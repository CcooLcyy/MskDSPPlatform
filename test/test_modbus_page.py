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
        for field in ("search", "template-list", "import-file", "new-template", "save", "copy-template", "download", "delete-template", "point-list", "point-editor", "add-point", "remove-point", "message", "username", "logout", "library-view", "trash-view", "library-title"):
            self.assertIn(f'id="{field}"', page)
        self.assertIn('name="viewport"', page)
        self.assertIn('href="/update"', page)
        self.assertIn("beforeunload", page)
        self.assertNotIn("innerHTML", page)
        self.assertNotIn("<script src=", page)
        self.assertIn("textContent", page)
        self.assertIn("If-Match", page)
        self.assertIn("X-MskDSP-Action", page)
        self.assertIn("90 天", page)
        self.assertNotIn("此操作无法撤销", page)
        self.assertIn('href="/assets/platform.css"', page)
        for classname in ("app-shell", "app-content", "app-topbar", "page-content", "page-heading", "app-nav"):
            self.assertIn(classname, page)
        self.assertNotIn('class="app-sidebar"', page)
        for field in ("workbench", "library-panel", "library-total", "trash-total", "catalog-count", "library-controls", "point-advanced", "page-title"):
            self.assertIn(f'id="{field}"', page)
        self.assertRegex(page, r'<details[^>]*id="point-advanced"')
        self.assertNotIn(":root {", page)
        identifiers = re.findall(r'\sid="([^"]+)"', page)
        self.assertEqual(len(identifiers), len(set(identifiers)), "页面控件 ID 不能重复，否则表单会读写错误的字段")
        for field in ("scale", "offset", "deadband"):
            self.assertRegex(page, rf'id="point-{field}"[^>]*type="text"')

    def test_workbench_layout_contract(self):
        page = PAGE.read_text(encoding="utf-8")
        header = page.split('<header class="app-topbar">', 1)[1].split('</header>', 1)[0]
        self.assertIn('href="/modbus-templates" aria-current="page">点表工作台', header)
        self.assertIn('href="/software">软件管理', header)
        self.assertIn('href="/update">平台维护', header)
        self.assertNotIn('id="trash-view"', header)
        catalog = page.split('id="library-panel"', 1)[1].split('id="editor-panel"', 1)[0]
        self.assertIn('id="trash-view"', catalog)
        heading = page.split('class="panel-heading editor-heading"', 1)[1].split('id="metadata-details"', 1)[0]
        for field in ('save', 'draft-status', 'template-actions', 'copy-template', 'download', 'export-draft'):
            self.assertIn(f'id="{field}"', heading)
        for field in ('property-panel', 'point-basic', 'point-acquisition', 'point-advanced', 'open-properties', 'close-properties', 'property-backdrop', 'show-function-code', 'show-group'):
            self.assertIn(f'id="{field}"', page)
        self.assertIn('@media (max-width: 1100px)', page)
        self.assertIn('.properties-open .property-panel', page)

    def test_trash_layout_contract(self):
        page = PAGE.read_text(encoding="utf-8")
        self.assertIn('class="catalog-heading"', page)
        for field in ('catalog-heading-info', 'catalog-search', 'workspace-footer'):
            self.assertIn(f'id="{field}"', page)
        self.assertNotIn('id="trash-intro"', page)

    def test_editor_and_client(self):
        node = shutil.which("node")
        if not node:
            self.fail("页面交互测试需要 Node")
        page = PAGE.read_text(encoding="utf-8")
        script = re.search(r'<script type="module">([\s\S]*?)</script>', page).group(1)
        syntax = subprocess.run([node, "--check", "--input-type=module"], input=script, capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        library = script.split("// 页面启动")[0]
        fixtures = [json.loads(path.read_text(encoding="utf-8-sig")) for path in sorted((ROOT / "test/fixtures/modbus-templates").glob("*.json"))]
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
assert.throws(()=>parseTemplate(' '.repeat(10*1024*1024+1)),/10 MiB/);
assert.throws(()=>validateTemplate({...template,points:[]}),/至少/);
const expanded={...template,points:Array.from({length:10000},(_,index)=>({...template.points[0],id:`point-${index}`,tag:`点位${index}`}))};
assert.equal(validateTemplate(expanded).points.length,10000);
assert.throws(()=>validateTemplate({...expanded,points:[...expanded.points,{...template.points[0],id:'overflow',tag:'超限'}]}),/10000/);
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
  await client.importFile({size:10*1024*1024+1,text:async()=>JSON.stringify(template)});
  assert.match(client.snapshot().error,/10 MiB/); assert.equal(client.snapshot().record.revision,2);
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

    def test_trash_client(self):
        """回收站切换、版本恢复、失败与会话失效均保留未保存草稿。"""
        node = shutil.which("node")
        self.assertIsNotNone(node, "页面交互测试需要 Node")
        script = re.search(r'<script type="module">([\s\S]*?)</script>', PAGE.read_text(encoding="utf-8")).group(1)
        library = script.split("// 页面启动")[0]
        scenario = r'''
const assert=require('node:assert/strict'),vm=require('node:vm');
const context=vm.createContext({TextEncoder,AbortController,setTimeout,clearTimeout,crypto:require('node:crypto').webcrypto});
vm.runInContext(LIBRARY+'\nthis.api={createTemplateClient,createTemplate,summary};',context);
const {createTemplateClient,createTemplate,summary}=context.api;
const BASE='/api/v1/modbus-templates',plain=value=>JSON.parse(JSON.stringify(value));
const record={id:'original-id',revision:1,created_at:100,updated_at:200,template:createTemplate()};
let active=[summary(record)],trash=[],failure=0,denyDiscard=true,redirects=0,roundedRestore=false;const calls=[];
const response=(body,status=200)=>({ok:status>=200&&status<300,status,json:async()=>body});
const client=createTemplateClient({onChange:()=>{},confirmDiscard:()=>!denyDiscard,onUnauthorized:()=>redirects++,fetchImpl:async(url,options)=>{
  calls.push({url,options});
  if(failure) return response({error:failure===422?'点表库已满':failure===404?'回收站记录不存在或已过期':'版本或会话已变化'},failure);
  if(url==='/api/v1/auth/me')return response({username:'admin'});
  if(url===BASE)return response(active);
  if(url===BASE+'/trash')return response(trash);
  if(options.method==='DELETE'){
    active=[];trash=[{...summary(record),revision:2,deleted_at:300,expires_at:300+90*24*60*60*1000}];
    return response(null,204);
  }
  if(url.endsWith('/restore')){
    if(roundedRestore){
      const restored=JSON.parse('{"id":"rounded-id","revision":9007199254740993,"created_at":100,"updated_at":400,"template":'+JSON.stringify(record.template)+'}');
      active=[summary(restored)];trash=[];return response(restored);
    }
    const restored={...record,id:url.split('/').at(-2),revision:6,updated_at:400};
    if(restored.id===record.id)restored.revision=3;
    active=[summary(restored)];trash=[];return response(restored);
  }
  return response(record);
}});
(async()=>{
  await client.refresh();await client.loadTrash();assert.equal(client.snapshot().trashLoaded,true);
  assert.equal(client.snapshot().trash.length,0);client.showLibrary();
  await client.load(record.id);await client.remove();
  assert.equal(client.snapshot().trashLoaded,false);
  assert.equal(client.snapshot().list.length,0);assert.equal(client.snapshot().draft,null);
  assert.match(client.snapshot().notice,/90 天/);
  await client.loadTrash();assert.equal(client.snapshot().view,'trash');
  assert.equal(client.snapshot().trash[0].revision,2);assert.equal(client.snapshot().trash[0].deleted_at,300);
  assert.equal(client.snapshot().trash[0].expires_at,300+90*24*60*60*1000);
  await client.restore(record.id);assert.equal(client.snapshot().record.revision,3);
  assert.equal(client.snapshot().trash.length,0);assert.equal(client.snapshot().list[0].revision,3);
  assert.equal(calls.at(-1).url,BASE+'/trash/original-id/restore');
  assert.equal(calls.at(-1).options.method,'POST');assert.equal(calls.at(-1).options.headers['If-Match'],'2');
  assert.equal(calls.at(-1).options.headers['X-MskDSP-Action'],'modbus-template');
  assert.equal(calls.at(-1).options.body,undefined);
  // 切换视图和刷新不放弃草稿；恢复失败不能清除草稿或回收站条目。
  client.editMetadata('name','必须保留的草稿');const draft=plain(client.snapshot().draft);
  trash=[{...summary(record),id:'other-id',revision:5,deleted_at:300,expires_at:999}];
  await client.loadTrash();client.showLibrary();assert.equal(client.snapshot().view,'library');
  assert.deepEqual(plain(client.snapshot().draft),draft);
  await client.loadTrash();await client.refresh();assert.deepEqual(plain(client.snapshot().draft),draft);
  for(const status of [422,409,404]){
    failure=status;await client.restore('other-id');assert.deepEqual(plain(client.snapshot().draft),draft);
    assert.equal(client.snapshot().trash.length,1);assert.equal(client.snapshot().view,'trash');
    assert.match(client.snapshot().error,status===422?/已满/:status===404?/过期/:/版本/);
  }
  failure=0;await client.restore('other-id');assert.deepEqual(plain(client.snapshot().draft),draft);
  assert.equal(client.snapshot().dirty,true);assert.equal(client.snapshot().trash.length,0);
  // 服务端 u64 递增版本经 JSON 解码可能舍入相同；HTTP 成功仍必须移除回收站卡片。
  roundedRestore=true;
  trash=[JSON.parse('{"id":"rounded-id","revision":9007199254740992,"name":"大版本点表","models":[]}')];
  await client.loadTrash();await client.restore('rounded-id');
  assert.equal(client.snapshot().error,'');assert.equal(client.snapshot().trash.length,0);
  assert.equal(client.snapshot().list[0].id,'rounded-id');
  assert.equal(client.snapshot().list[0].revision,9007199254740992);
  assert.deepEqual(plain(client.snapshot().draft),draft);
  // 回收站列表加载和恢复各自的 401 都停止请求，且只跳转一次登录页。
  for(const operation of ['loadTrash','restore']){
    let expired=false,count=0;const authCalls=[];
    const authClient=createTemplateClient({onChange:()=>{},onUnauthorized:()=>count++,fetchImpl:async(url,options)=>{
      authCalls.push({url,options});if(expired)return response({error:'请先登录'},401);
      return response(url==='/api/v1/auth/me'?{username:'admin'}:url===BASE+'/trash'?[{...summary(record),revision:2}]:[]);
    }});
    await authClient.refresh();authClient.startNew();const original=plain(authClient.snapshot().draft);
    if(operation==='restore')await authClient.loadTrash();expired=true;
    await authClient[operation](record.id);assert.equal(count,1);assert.equal(authClient.snapshot().stopped,true);
    assert.deepEqual(plain(authClient.snapshot().draft),original);const total=authCalls.length;
    await authClient.loadTrash();await authClient.restore(record.id);await authClient.refresh();authClient.showLibrary();
    assert.equal(authCalls.length,total);
  }
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        result = subprocess.run([node], input="const LIBRARY=" + json.dumps(library) + ";\n" + scenario, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_page_dom_events(self):
        """完整页面脚本跑在最小 DOM 中，检查实际表单事件及保存后的回填。"""
        node = shutil.which("node")
        self.assertIsNotNone(node, "页面交互测试需要 Node")
        page = PAGE.read_text(encoding="utf-8")
        script = re.search(r'<script type="module">([\s\S]*?)</script>', page).group(1)
        identifiers = re.findall(r'\sid="([^"]+)"', page)
        scenario = r'''
const assert=require('node:assert/strict'), vm=require('node:vm');
class Element {
  constructor(tag='div') {this.tag=tag;this.children=[];this.events={};this.value='';this.checked=false;this.disabled=false;this.hidden=false;this.attributes={};this.dataset={};this.open=false;const classes=new Set();this.classList={toggle:(name,enabled)=>enabled?classes.add(name):classes.delete(name),contains:name=>classes.has(name)};}
  set textContent(value) {this.content=value;this.children=[];}
  get textContent() {return this.content||'';}
  get childElementCount() {return this.children.length;}
  append(...children) {this.children.push(...children);}
  replaceChildren(...children) {this.children=[...children];}
  addEventListener(event,listener) {(this.events[event]??=[]).push(listener);}
  setAttribute(key,value) {this.attributes[key]=value;}
  getAttribute(key) {return this.attributes[key];}
  async dispatch(event,extra={}) {for(const listener of this.events[event]||[]) await listener({target:this,preventDefault:()=>{},stopPropagation:()=>{},...extra});}
  click() {return this.dispatch('click');}
  removeAttribute(key) {delete this.attributes[key];}
  focus() {this.focused=true;document.activeElement=this;}
  getClientRects() {return [0];}
  querySelectorAll() {return [];}
  scrollIntoView() {this.scrolled=true;}
  remove() {}
}
const nodes=Object.fromEntries(IDS.map(id=>[id,new Element()]));
const document={body:new Element('body'),getElementById:id=>nodes[id],createElement:tag=>new Element(tag),querySelectorAll:()=>[]};
let status=200, finishSave, finishTrash, slowTrash=false, slow=false,trash=[],storedTemplate,exportedBlob,narrow=false; const calls=[], windowEvents={};
const response=(body,code=200)=>({ok:code>=200&&code<300,status:code,json:async()=>body});
const window={matchMedia:()=>({matches:narrow}),confirm:()=>true,location:{assign:()=>{},hash:'#trash'},history:{pushState:(_s,_t,hash)=>window.location.hash=hash},addEventListener:(name,listener)=>windowEvents[name]=listener,fetch:async(url,options)=>{
  calls.push({url,options});
  if(url==='/api/v1/auth/me') return response({username:'admin'});
  if(url==='/api/v1/modbus-templates/trash') return slowTrash ? await new Promise(resolve=>finishTrash=resolve) : response(trash);
  if(url.endsWith('/restore')) {
    if(status!==200)return response({error:'点表库已满'},status);
    trash=[];return response({id:'trash-id',revision:6,created_at:1,updated_at:3,template:storedTemplate});
  }
  if(options.method==='GET') return response([]);
  if(options.method==='DELETE'){
    trash=[{id:'saved-id',revision:4,name:storedTemplate.name,models:[],point_count:storedTemplate.points.length,deleted_at:1000,expires_at:2000}];
    return response(null,204);
  }
  if(slow) return await new Promise(resolve=>finishSave=resolve);
  if(status!==200) return response({error:'版本已变化'},status);
  storedTemplate=JSON.parse(options.body);
  return response({id:'saved-id',revision:2,created_at:1,updated_at:2,template:storedTemplate});
}};
const context=vm.createContext({document,window,Blob,URL:{createObjectURL:blob=>{exportedBlob=blob;return 'blob:current-draft';},revokeObjectURL:()=>{}},crypto:require('node:crypto').webcrypto,TextEncoder,AbortController,setTimeout,clearTimeout});
vm.runInContext(SCRIPT+'\nthis.api={client,createTemplate,parseTemplate,MAX_BYTES};',context);
const {client,createTemplate,parseTemplate,MAX_BYTES}=context.api;
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  await tick(); assert.equal(nodes['username'].textContent,'admin');
  assert.equal(client.snapshot().view,'trash','initial trash deep link');
  assert.equal(nodes['page-title'].textContent,'回收站');
  assert.equal(nodes['catalog-heading-info'].hidden,true,'trash omits duplicate title and count');
  assert.equal(nodes['catalog-search'].hidden,true,'loaded empty trash has no search controls');
  assert.equal(nodes['template-list'].hidden,true,'empty list cannot reserve blank space');
  assert.equal(nodes['list-empty'].hidden,false);assert.equal(nodes['list-empty'].textContent,'回收站为空');
  assert.equal(nodes['workspace-footer'].hidden,true,'trash omits point-editor footer');
  assert.equal(nodes['message'].hidden,true,'empty trash has no repeated read-success message');
  assert.match(nodes['page-summary'].textContent,/90 天/);
  await nodes['library-view'].dispatch('click');
  assert.equal(window.location.hash,'#library');
  assert.equal(nodes['catalog-heading-info'].hidden,false);
  assert.equal(nodes['catalog-search'].hidden,false);
  assert.equal(nodes['workspace-footer'].hidden,false);
  assert.equal(nodes['library-total'].textContent,'0');
  assert.equal(nodes['trash-total'].textContent,'0');
  // 回收站导航自身未完成时后退；旧请求不能覆盖新的地址和历史意图。
  for (const targetHash of ['', '#library']) {
    window.location.hash='#library';slowTrash=true;
    const loadingTrash=nodes['trash-view'].dispatch('click');await tick();
    assert.equal(client.snapshot().pending,true);
    assert.equal(nodes['message'].hidden,false,'pending feedback stays visible');
    window.location.hash='';const wentBack=windowEvents.hashchange();
    if (targetHash) { window.location.hash=targetHash;await windowEvents.hashchange(); }
    slowTrash=false;finishTrash(response(trash));await loadingTrash;await wentBack;
    assert.equal(window.location.hash,targetHash,'old navigation never overwrites browser history');
    assert.equal(client.snapshot().view,'library','latest browser destination wins');
  }
  // 大草稿紧凑 JSON 合法，但缩进可能使导出的文件超过回导上限。
  const wide=JSON.parse(JSON.stringify(createTemplate()));
  wide.points=Array.from({length:10000},(_,i)=>({...wide.points[0],id:`export-point-${i}`,tag:`标签${i}`,description:'x'.repeat(670)}));
  const compact=JSON.stringify(wide), compactBytes=new TextEncoder().encode(compact).length;
  assert.ok(compactBytes<=MAX_BYTES);
  assert.ok(new TextEncoder().encode(JSON.stringify(wide,null,2)).length>MAX_BYTES,'fixture exercises pretty-print overflow');
  await client.importFile({size:compactBytes,text:async()=>compact});
  await nodes['export-draft'].dispatch('click');
  assert.equal(exportedBlob.size,compactBytes,'export uses exactly the bytes accepted by validation');
  assert.ok(exportedBlob.size<=MAX_BYTES);
  assert.equal(parseTemplate(await exportedBlob.text()).points.length,10000);
  await client.importFile(exportedBlob);
  assert.equal(client.snapshot().error,'');assert.equal(client.snapshot().draft.points.length,10000);
  const large=JSON.parse(JSON.stringify(createTemplate()));
  large.points=Array.from({length:121},(_,i)=>({...large.points[0],id:`large-${i}`,tag:`标签${i}`,group:i%2?'B':'A',function_code:i%3===0?6:3,address:i}));
  await client.importFile({size:100,text:async()=>JSON.stringify(large)});
  assert.equal(nodes['point-list'].children.length,50);
  // 重建五十行时只读取固定次数的页面状态，不能逐格序列化整个草稿。
  const readSnapshot=client.snapshot;let snapshotReads=0;
  client.snapshot=()=>{snapshotReads++;return readSnapshot();};
  for (const visible of [true,false]) {
    snapshotReads=0;nodes['show-function-code'].checked=visible;
    await nodes['show-function-code'].dispatch('change');
    assert.equal(nodes['point-list'].children.length,50);
    assert.equal(nodes['column-function-code'].hidden,!visible);
    assert.ok(snapshotReads<=2,`列切换重建五十行只能读取固定次数的快照，实际 ${snapshotReads} 次`);
  }
  client.snapshot=readSnapshot;
  // 单元格渲染后状态可能变化，点击仍必须检查最新请求状态和授权。
  const guardedCell=nodes['point-list'].children[1].children[1];
  const guardedSelection=nodes['point-id'].value,guardedDraft=JSON.stringify(readSnapshot().draft);
  for (const flags of [{pending:true},{pending:false,authorized:false}]) {
    snapshotReads=0;
    client.snapshot=()=>{snapshotReads++;return {...readSnapshot(),...flags};};
    await guardedCell.dispatch('click');
    assert.equal(snapshotReads,1,'拒绝单元格点击只需读取一次最新快照');
    assert.equal(nodes['point-id'].value,guardedSelection,'请求中或授权失效时不能切换点位');
    assert.equal(JSON.stringify(readSnapshot().draft),guardedDraft,'拒绝点击时保留草稿');
    client.snapshot=readSnapshot;
  }
  // 真实单元格事件写入同一草稿，切点和属性栏始终显示该草稿；显式保存前不发请求。
  const beforeInline=calls.length;
  const firstRow=nodes['point-list'].children[0];
  await firstRow.dispatch('click');
  assert.equal(nodes['point-list'].children[0],firstRow,'selecting a row keeps its cells mounted so double-click remains possible');
  narrow=true;await nodes['close-properties'].dispatch('click');
  await firstRow.children[1].dispatch('click');
  assert.equal(nodes['open-properties'].attributes['aria-expanded'],'false','first click in editable cell never opens drawer before double-click');
  assert.equal(nodes['point-list'].children[0],firstRow);
  await firstRow.children[1].dispatch('dblclick');
  let inline=firstRow.children[1].children[0];assert.equal(inline.focused,true);
  inline.value='0012';await inline.dispatch('input');
  assert.equal(client.snapshot().draft.points[0].address,12);
  assert.equal(nodes['point-address'].value,12,'property form follows inline changes');
  assert.equal(firstRow.children[1].children[0],inline,'typing does not remount input or lose focus');
  await inline.dispatch('keydown',{key:'Enter'});
  let row=nodes['point-list'].children[0];
  await row.children[3].dispatch('keydown',{key:'Enter'});
  inline=row.children[3].children[0];inline.value='1.234567890123456789e-22';await inline.dispatch('input');
  await inline.dispatch('keydown',{key:'Enter'});
  assert.equal(client.snapshot().draft.points[0].scale,'1.234567890123456789e-22');
  row=nodes['point-list'].children[0];await row.children[4].dispatch('dblclick');
  inline=row.children[4].children[0];inline.value='kW';await inline.dispatch('input');await inline.dispatch('blur');
  // A 尚在编辑时直接转到 B：真实顺序为 A blur、B click、B click、B dblclick。
  row=nodes['point-list'].children[0];await row.children[3].dispatch('dblclick');
  inline=row.children[3].children[0];inline.value='9.876543210987654321e-23';await inline.dispatch('input');
  const nextCell=nodes['point-list'].children[1].children[4];
  await inline.dispatch('blur');await nextCell.dispatch('click');await nextCell.dispatch('click');
  assert.equal(nodes['point-list'].children[1].children[4],nextCell,'switching cells preserves the second double-click target');
  await nextCell.dispatch('dblclick');inline=nextCell.children[0];
  assert.equal(inline.focused,true);assert.equal(inline.attributes['aria-label'],'编辑单位');
  inline.value='V';await inline.dispatch('input');await inline.dispatch('keydown',{key:'Enter'});
  assert.equal(client.snapshot().draft.points[1].unit,'V');
  assert.equal(client.snapshot().draft.points[0].scale,'9.876543210987654321e-23','switch retains the first cell precision');
  await nodes['point-list'].children[1].dispatch('click');
  assert.equal(nodes['point-tag'].value,'标签1');
  await nodes['point-list'].children[0].dispatch('click');assert.equal(nodes['point-unit'].value,'kW');
  row=nodes['point-list'].children[0];await row.children[4].dispatch('keydown',{key:'Enter'});
  inline=row.children[4].children[0];inline.value='临时单位';await inline.dispatch('input');await inline.dispatch('keydown',{key:'Escape'});
  assert.equal(client.snapshot().draft.points[0].unit,'kW','Escape reverts only the current cell');
  assert.equal(calls.length,beforeInline,'inline edits never submit automatically');
  // 错误同时标记行、单元格和右侧字段，修复后清除标记；不合法输入不提交。
  row=nodes['point-list'].children[0];await row.children[1].dispatch('dblclick');
  inline=row.children[1].children[0];inline.value='bad';await inline.dispatch('input');await inline.dispatch('keydown',{key:'Enter'});
  await nodes['save'].dispatch('click');assert.equal(calls.length,beforeInline);
  row=nodes['point-list'].children[0];assert.equal(row.attributes['aria-invalid'],'true');
  assert.equal(row.children[1].attributes['aria-invalid'],'true');assert.equal(nodes['point-address'].attributes['aria-invalid'],'true');
  assert.equal(nodes['point-acquisition'].open,true);
  await row.children[1].dispatch('keydown',{key:'Enter'});inline=row.children[1].children[0];
  inline.value='0';await inline.dispatch('input');await inline.dispatch('keydown',{key:'Enter'});
  assert.equal(nodes['point-list'].children[0].attributes['aria-invalid'],undefined);
  assert.equal(nodes['point-address'].attributes['aria-invalid'],undefined);
  // 功能码/分组按需显示，列选择不会丢失草稿。
  assert.equal(nodes['point-list'].children[0].children[5].hidden,true);
  nodes['show-function-code'].checked=true;await nodes['show-function-code'].dispatch('change');
  nodes['show-group'].checked=true;await nodes['show-group'].dispatch('change');
  assert.equal(nodes['column-function-code'].hidden,false);assert.equal(nodes['column-group'].hidden,false);
  assert.equal(nodes['point-list'].children[0].children[5].hidden,false);
  assert.equal(nodes['point-list'].children[0].children[5].textContent,'06');
  assert.equal(nodes['point-list'].children[0].children[6].textContent,'A');
  // 窄屏共用同一个属性表单，关闭抽屉、遮罩或 Escape 都保留草稿。
  await nodes['close-properties'].dispatch('click');
  nodes['open-properties'].focus();await nodes['open-properties'].dispatch('click');assert.equal(nodes['open-properties'].attributes['aria-expanded'],'true');
  assert.equal(nodes['editor-panel'].classList.contains('properties-open'),true);
  assert.equal(nodes['property-panel'].attributes.role,'dialog');assert.equal(nodes['property-panel'].attributes['aria-modal'],'true');
  assert.equal(document.activeElement,nodes['close-properties']);
  nodes['property-panel'].querySelectorAll=()=>[nodes['close-properties'],nodes['point-tag']];
  await nodes['property-panel'].dispatch('keydown',{key:'Tab',shiftKey:true});
  assert.equal(document.activeElement,nodes['point-tag'],'Shift+Tab stays in drawer');
  await nodes['property-panel'].dispatch('keydown',{key:'Tab'});assert.equal(document.activeElement,nodes['close-properties']);
  await nodes['close-properties'].dispatch('click');assert.equal(nodes['open-properties'].attributes['aria-expanded'],'false');
  assert.equal(document.activeElement,nodes['open-properties'],'closing drawer returns focus to trigger');
  assert.equal(nodes['property-panel'].attributes['aria-modal'],undefined);
  await nodes['open-properties'].dispatch('click');await nodes['property-backdrop'].dispatch('click');
  assert.equal(nodes['editor-panel'].classList.contains('properties-open'),false);
  await nodes['open-properties'].dispatch('click');await nodes['property-panel'].dispatch('keydown',{key:'Escape'});
  assert.equal(nodes['open-properties'].attributes['aria-expanded'],'false');
  assert.equal(client.snapshot().draft.points[0].unit,'kW');
  narrow=false;
  // 桌面属性已展开、焦点在表格时缩窄，必须迁焦到新出现的 modal 中。
  const resizeRow=nodes['point-list'].children[0];resizeRow.focus();await resizeRow.dispatch('click');
  assert.equal(nodes['property-panel'].attributes['aria-modal'],undefined);
  assert.equal(document.activeElement,resizeRow);
  narrow=true;await windowEvents.resize();
  assert.equal(nodes['property-panel'].attributes['aria-modal'],'true');
  assert.equal(document.activeElement,nodes['close-properties'],'crossing into narrow viewport moves background focus into drawer');
  await nodes['property-panel'].dispatch('keydown',{key:'Tab',shiftKey:true});assert.equal(document.activeElement,nodes['point-tag']);
  await nodes['property-panel'].dispatch('keydown',{key:'Tab'});assert.equal(document.activeElement,nodes['close-properties']);
  await nodes['close-properties'].dispatch('click');assert.equal(document.activeElement,resizeRow,'resize drawer close restores the previously focused row');
  // 原触发节点已重绘卸载时安全回到稳定的属性按钮。
  resizeRow.focus();await resizeRow.dispatch('click');resizeRow.isConnected=false;
  await nodes['close-properties'].dispatch('click');assert.equal(document.activeElement,nodes['open-properties']);
  narrow=false;
  await nodes['toggle-catalog'].dispatch('click');assert.equal(nodes['workbench'].classList.contains('catalog-collapsed'),true);
  await nodes['toggle-catalog'].dispatch('click');assert.equal(nodes['workbench'].classList.contains('catalog-collapsed'),false);
  await nodes['point-next'].dispatch('click');assert.match(nodes['point-page-status'].textContent,/第 2/);
  assert.equal(nodes['point-list'].children[0].children[0].children[0].textContent,'标签50');
  await nodes['point-list'].children[3].dispatch('keydown',{key:'Enter'});
  assert.equal(nodes['point-tag'].value,'标签53');
  nodes['point-group-filter'].value='group:B';await nodes['point-group-filter'].dispatch('change');
  assert.match(nodes['point-page-status'].textContent,/60 个匹配/);
  nodes['point-access-filter'].value='write';await nodes['point-access-filter'].dispatch('change');
  assert.match(nodes['point-page-status'].textContent,/20 个匹配/);
  assert.equal(nodes['point-tag'].value,'标签53','filters retain selected detail');
  nodes['point-search'].value='no-result';await nodes['point-search'].dispatch('input');
  assert.match(nodes['point-page-status'].textContent,/0 个匹配/);
  nodes['point-search'].value='';await nodes['point-search'].dispatch('input');
  // Duplicate tag validation locates the offending point outside the current page.
  client.editPoint(120,{...large.points[120],tag:large.points[0].tag});
  const invalidCalls=calls.length;await nodes['save'].dispatch('click');
  assert.equal(calls.length,invalidCalls);assert.equal(nodes['point-tag'].attributes['aria-invalid'],'true');
  assert.equal(nodes['point-id'].value,'large-120');assert.match(nodes['point-page-status'].textContent,/第 3/);
  assert.match(nodes['error-point-tag'].textContent,/重复/);
  nodes['point-tag'].value='独立标签';await nodes['point-tag'].dispatch('input');
  assert.equal(nodes['point-tag'].attributes['aria-invalid'],undefined);
  client.editPoint(120,{...client.snapshot().draft.points[120],enum_value:0});
  await nodes['save'].dispatch('click');assert.equal(nodes['point-advanced'].open,true);
  assert.equal(nodes['point-enum_value'].attributes['aria-invalid'],'true');
  await nodes['point-list'].children[0].dispatch('click');
  assert.equal(nodes['point-enum_value'].attributes['aria-invalid'],undefined,'switch clears stale field errors');
  assert.equal(nodes['point-id'].value,'large-100','switch does not relocate to the previous invalid point');
  // 修正另一点的错误后，保存当前模板仍停在选中的点位。
  client.editPoint(120,{...large.points[120],tag:'独立标签'});
  await nodes['point-list'].children[3].dispatch('click');
  assert.equal(nodes['point-id'].value,'large-103');
  await nodes['save'].dispatch('click');
  assert.equal(client.snapshot().dirty,false);assert.equal(nodes['point-id'].value,'large-103');
  const template=JSON.parse(JSON.stringify(createTemplate()));
  template.name='<img src=x onerror=alert(1)>'; template.points[0].tag='<script>alert(1)</script>';
  template.notes=['多行说明\n保持换行'];template.sources=[{file:'来源.pdf',sha256:'a'.repeat(64)}];
  nodes['import-file'].files=[{size:100,text:async()=>JSON.stringify(template)}];
  await nodes['import-file'].dispatch('change');
  assert.equal(nodes['meta-name'].value,template.name);
  assert.equal(nodes['point-list'].children[0].children[0].children[0].textContent,template.points[0].tag);
  const importedSnapshot=JSON.stringify(client.snapshot().draft);
  const badImport={...template,points:[...template.points,{...template.points[0],id:'new-id',address:'bad'}]};
  await client.importFile({size:100,text:async()=>JSON.stringify(badImport)});
  assert.equal(JSON.stringify(client.snapshot().draft),importedSnapshot,'invalid import preserves current draft');
  assert.equal(client.snapshot().validationError,null,'import errors do not mark existing draft fields');
  assert.equal(nodes['metadata-details'].open,false);
  assert.match(nodes['metadata-summary'].textContent,/0 基/);
  assert.equal(nodes['export-draft'].disabled,false);
  nodes['point-address'].value='bad';await nodes['point-address'].dispatch('input');
  const beforeInvalid=calls.length;await nodes['save'].dispatch('click');
  assert.equal(calls.length,beforeInvalid,'invalid draft never submitted');
  assert.equal(nodes['point-address'].attributes['aria-invalid'],'true');
  assert.match(nodes['error-point-address'].textContent,/地址/);
  assert.equal(nodes['point-address'].focused,true);
  nodes['point-address'].value='0';await nodes['point-address'].dispatch('input');
  assert.equal(nodes['point-address'].attributes['aria-invalid'],undefined);
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
  const beforeExport=calls.length;await nodes['export-draft'].dispatch('click');
  assert.equal(calls.length,beforeExport,'draft export is local');
  const exported=JSON.parse(await exportedBlob.text());
  assert.equal(exported.points[0].scale,'1.234567890123456789e-22');
  assert.equal(exported.points[0].enum_value,0);
  assert.equal(document.body.children.at(-1).href,'blob:current-draft');
  let prevented=false; const event={preventDefault:()=>prevented=true}; windowEvents.beforeunload(event);assert.equal(prevented,true);
  status=409; await nodes['save'].dispatch('click');
  assert.equal(client.snapshot().conflict,true);assert.equal(client.snapshot().validationError,null);assert.equal(nodes['point-address'].attributes['aria-invalid'],undefined); assert.equal(nodes['point-scale'].value,'1.234567890123456789e-22');
  assert.match(nodes['message'].textContent,/草稿已保留/);
  status=200; slow=true; const saving=nodes['save'].dispatch('click');
  assert.equal(nodes['save'].disabled,true);assert.equal(nodes['new-template'].disabled,true);assert.equal(nodes['point-editor'].disabled,true);
  window.location.hash='#trash';const pendingHash=windowEvents.hashchange();
  assert.equal(client.snapshot().pending,true);
  const posted=JSON.parse(calls.at(-1).options.body);
  finishSave(response({id:'saved-id',revision:3,created_at:1,updated_at:3,template:posted}));await saving;slow=false;
  await pendingHash;
  assert.equal(client.snapshot().view,'trash','pending history navigation is reconciled after save');
  assert.equal(window.location.hash,'#trash');
  await nodes['library-view'].dispatch('click');
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
  assert.equal(nodes['export-draft'].disabled,false,'new template can export current draft');
  await nodes['export-draft'].dispatch('click');
  assert.equal(JSON.parse(await exportedBlob.text()).id,client.snapshot().draft.id);
  // 回收站卡片只以文本展示导入名称，显示服务端时间，切换及恢复失败保留右侧草稿。
  storedTemplate=posted;
  const copied=JSON.parse(JSON.stringify(client.snapshot().draft));
  trash=[{id:'trash-id',revision:5,name:posted.name,manufacturer:'厂家',models:[],point_count:1,deleted_at:1000,expires_at:2000}];
  await nodes['trash-view'].dispatch('click');assert.equal(window.location.hash,'#trash');assert.equal(nodes['library-title'].textContent,'回收站');
  assert.equal(nodes['editor-panel'].hidden,true);assert.equal(nodes['library-controls'].hidden,true);
  assert.equal(nodes['workbench'].attributes['data-view'],'trash');
  assert.equal(nodes['trash-total'].textContent,'1');
  assert.equal(nodes['trash-view'].attributes['aria-pressed'],'true');
  assert.equal(nodes['catalog-heading-info'].hidden,true);
  assert.equal(nodes['catalog-search'].hidden,false,'nonempty trash remains searchable');
  assert.equal(nodes['template-list'].hidden,false);
  assert.equal(nodes['workspace-footer'].hidden,true);
  assert.equal(nodes['message'].hidden,true,'successful trash reads do not repeat count');
  nodes['search'].value='找不到的点表';await nodes['search'].dispatch('input');
  assert.equal(nodes['template-list'].hidden,true);
  assert.equal(nodes['list-empty'].hidden,false);assert.match(nodes['list-empty'].textContent,/没有匹配/);
  assert.equal(nodes['catalog-search'].hidden,false,'search with no matches must remain usable');
  const unfilteredTrash=trash;trash=[];await nodes['refresh'].dispatch('click');
  assert.equal(nodes['catalog-search'].hidden,true);
  assert.equal(nodes['list-empty'].textContent,'回收站为空','refresh replaces unmatched-search text when the collection becomes empty');
  trash=unfilteredTrash;await nodes['refresh'].dispatch('click');
  assert.equal(nodes['catalog-search'].hidden,false);
  assert.equal(nodes['search'].value,'找不到的点表','refresh preserves the current search');
  assert.match(nodes['list-empty'].textContent,/没有匹配/);
  nodes['search'].value='';await nodes['search'].dispatch('input');
  assert.equal(nodes['template-list'].hidden,false);assert.equal(nodes['list-empty'].hidden,true);
  assert.equal(nodes['template-list'].children.length,1);
  window.location.hash='#library';await windowEvents.hashchange();
  assert.equal(client.snapshot().view,'library');assert.deepEqual(JSON.parse(JSON.stringify(client.snapshot().draft)),copied);
  window.location.hash='#trash';await windowEvents.hashchange();
  assert.equal(client.snapshot().view,'trash');assert.deepEqual(JSON.parse(JSON.stringify(client.snapshot().draft)),copied);
  const card=nodes['template-list'].children[0].children[0];
  assert.equal(card.children[0].textContent,posted.name);
  assert.match(card.children[3].textContent,/删除：/);assert.match(card.children[4].textContent,/到期：/);
  assert.equal(card.children[3].textContent,'删除：'+new Date(1000).toLocaleString('zh-CN'));
  assert.equal(card.children[4].textContent,'到期：'+new Date(2000).toLocaleString('zh-CN'));
  status=422;await card.children[5].dispatch('click');assert.match(nodes['message'].textContent,/已满/);
  assert.equal(nodes['message'].hidden,false,'restore failure stays visible');
  assert.equal(nodes['message'].classList.contains('error'),true);
  assert.equal(nodes['template-list'].hidden,false);
  assert.equal(nodes['catalog-search'].hidden,false);
  assert.deepEqual(JSON.parse(JSON.stringify(client.snapshot().draft)),copied);
  assert.equal(nodes['template-list'].children.length,1);
  status=200;await nodes['template-list'].children[0].children[0].children[5].dispatch('click');
  assert.equal(nodes['list-empty'].hidden,false);assert.match(nodes['list-empty'].textContent,/回收站为空/);
  assert.equal(nodes['list-empty'].textContent,'回收站为空');
  assert.equal(nodes['template-list'].hidden,true);
  assert.equal(nodes['catalog-search'].hidden,true,'restoring the last record returns to empty state');
  assert.equal(nodes['message'].hidden,false,'restore success feedback stays visible');
  assert.equal(nodes['message'].classList.contains('error'),false);
  assert.match(nodes['message'].textContent,/恢复/);
  assert.equal(nodes['trash-total'].textContent,'0');
  assert.deepEqual(JSON.parse(JSON.stringify(client.snapshot().draft)),copied);
  await nodes['library-view'].dispatch('click');assert.equal(nodes['library-title'].textContent,'全部点表');
  assert.doesNotMatch(nodes['message'].textContent,/回收站有/);
  assert.equal(nodes['editor-panel'].hidden,false);assert.equal(nodes['library-controls'].hidden,false);
  assert.equal(nodes['catalog-heading-info'].hidden,false);
  assert.equal(nodes['catalog-search'].hidden,false);
  assert.equal(nodes['workspace-footer'].hidden,false);
  assert.equal(nodes['meta-name'].value,copied.name);
  assert.deepEqual(JSON.parse(JSON.stringify(client.snapshot().draft)),copied);
  // 已读取空回收站后删除正常点表，旧零计数应失效，重新访问才显示真实数量。
  await nodes['save'].dispatch('click');
  assert.equal(nodes['trash-total'].textContent,'0');
  await nodes['delete-template'].dispatch('click');await tick();assert.equal(nodes['trash-total'].textContent,'—');
  await nodes['trash-view'].dispatch('click');assert.equal(nodes['trash-total'].textContent,'1');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        command = "const SCRIPT=" + json.dumps(script) + ";\nconst IDS=" + json.dumps(identifiers) + ";\n" + scenario
        result = subprocess.run([node], input=command, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
