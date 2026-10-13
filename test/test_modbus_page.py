"""点表页面契约和交互测试，仅依赖 Python / Node 标准库。"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "web" / "modbus-templates.html"


class ModbusPageTests(unittest.TestCase):
    def test_page_contract(self):
        page = PAGE.read_text(encoding="utf-8")
        for field in ("search", "template-list", "import-file", "new-template", "save", "copy-template", "download", "delete-template", "point-list", "point-editor", "add-point", "remove-point", "message", "username", "logout"):
            self.assertIn(f'id="{field}"', page)
        self.assertIn('name="viewport"', page)
        self.assertIn('href="/update"', page)
        self.assertIn("beforeunload", page)
        self.assertNotIn("innerHTML", page)
        self.assertNotIn("<script src=", page)
        self.assertIn("textContent", page)
        self.assertIn("If-Match", page)
        self.assertIn("X-MskDSP-Action", page)
        for field in ("scale", "offset", "deadband"):
            self.assertRegex(page, rf'id="point-{field}"[^>]*type="text"')

    def test_editor_and_client(self):
        node = shutil.which("node")
        if not node:
            self.fail("页面交互测试需要 Node")
        page = PAGE.read_text(encoding="utf-8")
        script = re.search(r'<script type="module">([\s\S]*?)</script>', page).group(1)
        syntax = subprocess.run([node, "--check", "--input-type=module"], input=script, capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        library = script.split("// 页面启动")[0]
        fixtures = [json.loads(path.read_text(encoding="utf-8")) for path in (ROOT.parent / "MskDSPUpper/src/features/modbus-templates/builtin").glob("*.json")]
        self.assertGreaterEqual(len(fixtures), 3)
        scenario = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const context = vm.createContext({TextEncoder, AbortController, setTimeout, clearTimeout, crypto:require('node:crypto').webcrypto});
vm.runInContext(LIBRARY + '\nthis.api = {parseTemplate, validateTemplate, createTemplate, createPoint, pointFromFields, createTemplateClient, filterTemplates};', context);
const {parseTemplate, validateTemplate, createTemplate, createPoint, pointFromFields, createTemplateClient, filterTemplates} = context.api;
const plain = value => JSON.parse(JSON.stringify(value));
for(const fixture of FIXTURES) assert.deepEqual(plain(parseTemplate(JSON.stringify(fixture))), fixture);
const template=plain(createTemplate());
assert.match(template.id,/^[A-Za-z0-9][A-Za-z0-9_.-]*$/);
template.name='<img src=x onerror=alert(1)>';
template.notes=['第一行\n第二行'];
template.sources=[{file:'来源.pdf',sha256:'a'.repeat(64)}];
Object.assign(template.points[0],{scale:'0.123456789012345678901234567890',offset:'1.000e-20',deadband:'-0.0000'});
const parsed=parseTemplate('\uFEFF'+JSON.stringify(template));
assert.equal(parsed.points[0].scale,template.points[0].scale);
assert.equal(parsed.points[0].offset,'1.000e-20');
assert.equal(parsed.points[0].deadband,'-0.0000');
const original={...parsed.points[0],data_type:'BOOL',default_selected:false,condition:'机型A',enum_value:0};
const values={...original,bit_index:'',enum_value:'0',default_selected:'false',condition_enabled:true,enum_enabled:true};
const edited=pointFromFields(original,values);
assert.equal(edited.default_selected,false); assert.equal(edited.enum_value,0); assert.equal(edited.bit_index,null);
assert.equal(edited.condition,'机型A'); assert.equal(edited.scale,original.scale);
values.enum_value=''; assert.equal(pointFromFields(original,values).enum_value,null);
values.enum_enabled=false; values.condition_enabled=false; values.default_selected='';
const omitted=pointFromFields(original,values);
for(const field of ['enum_value','condition','default_selected']) assert.equal(Object.hasOwn(omitted,field),false);
assert.throws(()=>parseTemplate('{'),/JSON/);
assert.throws(()=>parseTemplate(' '.repeat(2*1024*1024+1)),/2 MiB/);
assert.throws(()=>validateTemplate({...template,points:[]}),/至少/);
assert.throws(()=>validateTemplate({...template,points:Array(2001).fill(template.points[0])}),/2000/);
assert.throws(()=>validateTemplate({...template,extra:true}),/字段/);
assert.throws(()=>validateTemplate({...template,points:[{...template.points[0],scale:0.1}]}),/倍率/);
assert.throws(()=>validateTemplate({...template,points:[{...template.points[0],data_type:'UINT32',reg_count:1}]}),/不匹配/);
assert.equal(filterTemplates([{name:'名称',manufacturer:'华为',models:['SUN2000'],protocol_version:'V3'}],'sun2000').length,1);
const calls=[]; let reject=0, offline=false, delay=false, resolveRequest, allowDiscard=false;
const record={id:'platform-id',revision:1,created_at:1,updated_at:2,template};
const response=(body,status=200)=>({ok:status>=200&&status<300,status,json:async()=>body});
const client=createTemplateClient({fetchImpl:async(url,options)=>{
  calls.push({url,options});
  if(offline) throw new Error('offline');
  if(delay) return await new Promise(resolve=>resolveRequest=resolve);
  if(reject) return response({error:reject===409?'版本已变化':'中文校验错误'},reject);
  if(options.method==='DELETE') return {ok:true,status:204,json:async()=>{throw Error('无响应体')}};
  if(url === '/api/v1/auth/me') return response({username:'admin',role:'admin'});
  if(url.endsWith('modbus-templates') && options.method==='GET') return response([]);
  if(options.method==='PUT'||options.method==='POST') return response({...record,revision:2,template:JSON.parse(options.body)},options.method==='POST'?201:200);
  return response(record);
},onChange:()=>{},confirmDiscard:()=>allowDiscard});
(async()=>{
  await client.refresh(); assert.equal(calls[0].options.credentials,'same-origin');
  await client.load(record.id); assert.equal(client.snapshot().dirty,false);
  client.editMetadata('name','编辑后的名称'); assert.equal(client.snapshot().dirty,true);
  const draft=plain(client.snapshot().draft);
  await client.load('another'); assert.deepEqual(plain(client.snapshot().draft),draft);
  reject=409; await client.save();
  assert.deepEqual(plain(client.snapshot().draft),draft); assert.equal(client.snapshot().dirty,true);
  assert.equal(client.snapshot().conflict,true); assert.match(client.snapshot().error,/版本/);
  assert.equal(calls.at(-1).options.headers['If-Match'],'1');
  assert.equal(calls.at(-1).options.headers['X-MskDSP-Action'],'modbus-template');
  reject=422; await client.save(); assert.deepEqual(plain(client.snapshot().draft),draft);
  assert.match(client.snapshot().error,/中文校验错误/);
  reject=0; offline=true; await client.save(); assert.deepEqual(plain(client.snapshot().draft),draft);
  assert.match(client.snapshot().error,/连接/); offline=false;
  delay=true; const saving=client.save(); assert.equal(client.snapshot().pending,true);
  const before=calls.length; await client.save(); await client.remove(); client.editMetadata('name','应忽略');
  assert.equal(calls.length,before); assert.equal(client.snapshot().draft.name,draft.name);
  resolveRequest(response({...record,revision:2,template:draft})); await saving; delay=false;
  assert.equal(client.snapshot().pending,false); assert.equal(client.snapshot().dirty,false);
  assert.equal(client.snapshot().record.revision,2); assert.deepEqual(plain(client.snapshot().draft.notes),template.notes);
  assert.deepEqual(plain(client.snapshot().draft.sources),template.sources);
  allowDiscard=true;
  await client.importFile({size:2*1024*1024+1,text:async()=>JSON.stringify(template)});
  assert.match(client.snapshot().error,/2 MiB/); assert.equal(client.snapshot().record.revision,2);
  await client.importFile({size:100,text:async()=>JSON.stringify(template)});
  assert.equal(client.snapshot().record,null); assert.equal(client.snapshot().dirty,true);
  assert.equal(client.snapshot().draft.points[0].scale,template.points[0].scale);
  client.editPoint(0,{...client.snapshot().draft.points[0],default_selected:false,condition:'',enum_value:null});
  await client.save(); const post=calls.at(-1);
  assert.equal(post.options.method,'POST'); assert.equal(post.options.headers['If-Match'],undefined);
  const posted=JSON.parse(post.options.body); assert.equal(posted.points[0].default_selected,false);
  assert.equal(posted.points[0].condition,''); assert.equal(posted.points[0].enum_value,null);
  reject=409; await client.remove(); assert.equal(client.snapshot().record.revision,2);
  assert.equal(client.snapshot().conflict,true);
  reject=0; await client.remove(); assert.equal(client.snapshot().record,null); assert.equal(client.snapshot().draft,null);
  assert.equal(calls.at(-1).options.headers['If-Match'],'2');
  client.startNew(); const first=client.snapshot().draft.points[0].id;
  client.addPoint(); assert.equal(client.snapshot().draft.points.length,2);
  assert.notEqual(client.snapshot().draft.points[1].id,first);
  client.removePoint(0); assert.equal(client.snapshot().draft.points.length,1);
  const sourceId=client.snapshot().draft.id;
  const sourcePoints=plain(client.snapshot().draft.points);
  client.copyTemplate(); assert.notEqual(client.snapshot().draft.id,sourceId);
  assert.equal(client.snapshot().record,null); assert.equal(client.snapshot().dirty,true);
  assert.deepEqual(plain(client.snapshot().draft.points),sourcePoints);
  assert.match(client.snapshot().draft.name,/副本/);
  // 各接口会话失效都停止后续动作，登录跳转只发起一次。
  for(const rejectedPath of ['/api/v1/auth/me','/api/v1/modbus-templates','/api/v1/modbus-templates/platform-id']) {
    let rejects=false,redirects=0; const authCalls=[];
    const authClient=createTemplateClient({fetchImpl:async(url,options)=>{
      authCalls.push({url,options});
      if(rejects && url===rejectedPath) return response({error:'请先登录'},401);
      return response(url==='/api/v1/auth/me'?{username:'admin',role:'admin'}:url===BASE?[]:record);
    },onChange:()=>{},onUnauthorized:()=>redirects++});
    if(rejectedPath.endsWith('platform-id')) { await authClient.refresh(); await authClient.load(record.id); authClient.editMetadata('name','草稿'); }
    rejects=true;
    if(rejectedPath.endsWith('platform-id')) await authClient.save(); else await authClient.refresh();
    assert.equal(redirects,1); assert.equal(authClient.snapshot().stopped,true);
    const total=authCalls.length; await authClient.refresh(); await authClient.save(); authClient.startNew();
    assert.equal(authCalls.length,total);
    if(rejectedPath.endsWith('platform-id')) assert.equal(authClient.snapshot().draft.name,'草稿');
  }
  // 退出登录确认未保存内容，且使用 auth 防跨源标记。
  let discard=false,redirects=0; const exitCalls=[];
  const exitClient=createTemplateClient({fetchImpl:async(url,options)=>{
    exitCalls.push({url,options});
    if(url==='/api/v1/auth/logout') return response(null,204);
    return response(url==='/api/v1/auth/me'?{username:'admin'}:[]);
  },onChange:()=>{},confirmDiscard:()=>discard,onUnauthorized:()=>redirects++});
  await exitClient.refresh(); exitClient.startNew(); const total=exitCalls.length;
  await exitClient.logout(); assert.equal(exitCalls.length,total); discard=true;
  await exitClient.logout(); assert.equal(redirects,1); assert.equal(exitClient.snapshot().dirty,false);
  assert.equal(exitCalls.at(-1).options.headers['X-MskDSP-Action'],'auth');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        command = "const LIBRARY = " + json.dumps(library) + ";\nconst FIXTURES = " + json.dumps(fixtures) + ";\nconst BASE='/api/v1/modbus-templates';\n" + scenario
        result = subprocess.run([node], input=command, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_page_dom_events(self):
        """完整页面脚本跑在最小 DOM 中，检查实际表单事件及保存后的回填。"""
        node = shutil.which("node")
        self.assertIsNotNone(node, "页面交互测试需要 Node")
        page = PAGE.read_text(encoding="utf-8")
        script = re.search(r'<script type="module">([\s\S]*?)</script>', page).group(1)
        identifiers = re.findall(r'id="([^"]+)"', page)
        scenario = r'''
const assert=require('node:assert/strict'), vm=require('node:vm');
class Element {
  constructor(tag='div') {this.tag=tag;this.children=[];this.events={};this.value='';this.checked=false;this.disabled=false;this.hidden=false;this.attributes={};this.classList={toggle:()=>{}};}
  set textContent(value) {this.content=value;this.children=[];}
  get textContent() {return this.content||'';}
  get childElementCount() {return this.children.length;}
  append(...children) {this.children.push(...children);}
  replaceChildren(...children) {this.children=[...children];}
  addEventListener(event,listener) {(this.events[event]??=[]).push(listener);}
  setAttribute(key,value) {this.attributes[key]=value;}
  async dispatch(event) {for(const listener of this.events[event]||[]) await listener({target:this});}
  click() {return this.dispatch('click');}
  remove() {}
}
const nodes=Object.fromEntries(IDS.map(id=>[id,new Element()]));
const document={body:new Element('body'),getElementById:id=>nodes[id],createElement:tag=>new Element(tag),querySelectorAll:()=>[]};
let status=200, finishSave, slow=false; const calls=[], windowEvents={};
const response=(body,code=200)=>({ok:code>=200&&code<300,status:code,json:async()=>body});
const window={confirm:()=>true,location:{assign:()=>{}},addEventListener:(name,listener)=>windowEvents[name]=listener,fetch:async(url,options)=>{
  calls.push({url,options});
  if(url==='/api/v1/auth/me') return response({username:'admin'});
  if(options.method==='GET') return response([]);
  if(slow) return await new Promise(resolve=>finishSave=resolve);
  if(status!==200) return response({error:'版本已变化'},status);
  return response({id:'saved-id',revision:2,created_at:1,updated_at:2,template:JSON.parse(options.body)});
}};
const context=vm.createContext({document,window,crypto:require('node:crypto').webcrypto,TextEncoder,AbortController,setTimeout,clearTimeout});
vm.runInContext(SCRIPT+'\nthis.api={client,createTemplate};',context);
const {client,createTemplate}=context.api;
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  await tick(); assert.equal(nodes['username'].textContent,'admin');
  const template=JSON.parse(JSON.stringify(createTemplate()));
  template.name='<img src=x onerror=alert(1)>'; template.points[0].tag='<script>alert(1)</script>';
  template.notes=['多行说明\n保持换行'];template.sources=[{file:'来源.pdf',sha256:'a'.repeat(64)}];
  nodes['import-file'].files=[{size:100,text:async()=>JSON.stringify(template)}];
  await nodes['import-file'].dispatch('change');
  assert.equal(nodes['meta-name'].value,template.name);
  assert.equal(nodes['point-list'].children[0].children[0].textContent,template.points[0].tag);
  nodes['point-scale'].value='1.234567890123456789e-22'; await nodes['point-scale'].dispatch('input');
  nodes['point-data_type'].value='BOOL'; await nodes['point-data_type'].dispatch('input');
  nodes['point-default_selected'].value='false'; await nodes['point-default_selected'].dispatch('input');
  nodes['point-condition_enabled'].checked=true; await nodes['point-condition_enabled'].dispatch('input');
  nodes['point-condition'].value='机型条件'; await nodes['point-condition'].dispatch('input');
  nodes['point-enum_enabled'].checked=true; await nodes['point-enum_enabled'].dispatch('input');
  nodes['point-enum_value'].value='0'; await nodes['point-enum_value'].dispatch('input');
  const draft=client.snapshot().draft;
  assert.equal(draft.points[0].scale,'1.234567890123456789e-22');
  assert.equal(draft.points[0].enum_value,0); assert.equal(draft.points[0].default_selected,false);
  assert.equal(draft.points[0].condition,'机型条件');
  assert.deepEqual(JSON.parse(JSON.stringify(draft.notes)),template.notes);
  let prevented=false; const event={preventDefault:()=>prevented=true}; windowEvents.beforeunload(event);assert.equal(prevented,true);
  status=409; await nodes['save'].dispatch('click');
  assert.equal(client.snapshot().conflict,true); assert.equal(nodes['point-scale'].value,'1.234567890123456789e-22');
  assert.match(nodes['message'].textContent,/草稿已保留/);
  status=200; slow=true; const saving=nodes['save'].dispatch('click');
  assert.equal(nodes['save'].disabled,true);assert.equal(nodes['new-template'].disabled,true);assert.equal(nodes['point-editor'].disabled,true);
  const posted=JSON.parse(calls.at(-1).options.body);
  finishSave(response({id:'saved-id',revision:3,created_at:1,updated_at:3,template:posted}));await saving;slow=false;
  assert.equal(client.snapshot().dirty,false);assert.equal(nodes['save'].disabled,false);
  assert.equal(nodes['point-scale'].value,posted.points[0].scale);
  assert.equal(nodes['point-enum_value'].value,0);
  await nodes['add-point'].dispatch('click');assert.equal(client.snapshot().draft.points.length,2);
  assert.equal(nodes['point-id'].value,client.snapshot().draft.points[1].id);
  await nodes['remove-point'].dispatch('click');assert.equal(client.snapshot().draft.points.length,1);
  assert.equal(nodes['point-id'].value,client.snapshot().draft.points[0].id);
  await nodes['download'].dispatch('click');assert.equal(document.body.children.at(-1).href,'/api/v1/modbus-templates/saved-id/download');
  await nodes['copy-template'].dispatch('click');assert.equal(client.snapshot().record,null);
  assert.equal(nodes['download'].disabled,true);assert.notEqual(client.snapshot().draft.id,posted.id);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        command = "const SCRIPT=" + json.dumps(script) + ";\nconst IDS=" + json.dumps(identifiers) + ";\n" + scenario
        result = subprocess.run([node], input=command, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
