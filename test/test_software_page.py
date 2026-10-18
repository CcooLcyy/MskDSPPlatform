"""执行实际软件版本页面脚本，验证会话、发布流程和安全文本渲染。"""
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

PAGE = Path(__file__).resolve().parents[1] / "web/software.html"


class SoftwarePageTests(unittest.TestCase):
    def test_browser_script(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "软件版本页面行为测试需要 Node")
        page = PAGE.read_text(encoding="utf-8")
        script = re.search(r'<script type="module">([\s\S]*?)</script>', page).group(1)
        syntax = subprocess.run([node, "--check", "--input-type=module"], input=script,
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        scenario = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
class Element {
  constructor(tag='div') { this.tag=tag; this.children=[]; this.events={}; this.value=''; this.disabled=false; this.attributes={}; this.textContent=''; this.className=''; this.classes=new Set(); this.classList={toggle:(name,value)=>value?this.classes.add(name):this.classes.delete(name)}; }
  set innerHTML(_) { throw new Error('动态内容不能使用 innerHTML'); }
  append(...children) { this.children.push(...children); if(this.tag==='select'&&!this.value&&children.length) this.value=children[0].value; }
  replaceChildren(...children) { this.children=[...children]; }
  addEventListener(name, fn) { this.events[name]=fn; }
  setAttribute(name, value) { this.attributes[name]=value; }
}
function record(fields={}) {
  return {id:'package-id',metadata:{product:'mskdsp-upper',version:'1.2.3',platform:'windows-x86_64-nsis',file_name:'installer.exe',size:2048,sha256:'a'.repeat(64),notes:'Release notes',...fields.metadata},channels:[],ever_published:false,created_at:'2026-10-15T00:00:00Z',...fields};
}
const response = (status, body) => ({ok:status>=200&&status<300,status,json:async()=>body});
async function open({records=[record()],user={username:'operator',role:'admin'},rejectAt='',failList=false}={}) {
  const ids={}; for(const name of ['message','refresh','logout','product','channel','packages','count','empty','username']) ids[name]=new Element();
  const calls=[],redirects=[],confirms=[];
  const state={records,failList,rejectAt,allowConfirm:true,publishFailure:false,hold:null,finish:null};
  const flatten=nodes=>nodes.flatMap(node=>[node,...flatten(node.children)]);
  const context=vm.createContext({AbortController,setTimeout,clearTimeout,
    document:{getElementById:name=>ids[name],createElement:tag=>new Element(tag),querySelectorAll:()=>flatten(ids.packages.children).filter(node=>['button','select'].includes(node.tag))},
    window:{confirm:value=>{confirms.push(value);return state.allowConfirm;}},location:{assign:value=>redirects.push(value)},
    fetch:async(path,options)=>{
      calls.push({path,options});
      if(path===state.rejectAt) return response(401,{error:'请重新登录'});
      if(path==='/api/v1/auth/me') return response(200,user);
      if(path==='/api/v1/auth/logout') return response(204,null);
      if(options.method==='POST') {
        if(state.publishFailure) return response(412,{error:'尚未配置公开地址'});
        if(state.hold) await new Promise(resolve=>{state.finish=resolve;});
        const channel=JSON.parse(options.body).channel;
        state.records=state.records.map(pkg=>({...pkg,channels:path.endsWith('/publish')?[channel]:[],ever_published:true}));
        return response(200,state.records[0]);
      }
      if(state.failList) throw new Error('网络离线');
      return response(200,state.records);
    }});
  await vm.runInContext('(async()=>{'+SCRIPT+'\nthis.api={refresh,mutate,render,snapshot:()=>({busy,authenticated,packages})};})()',context);
  return {ids,calls,redirects,confirms,state,api:context.api,flatten};
}
(async()=>{
  // 读取身份后才展示数据；动态标题、说明和链接参数不会成为 HTML。
  const malicious=record({id:'id/<script>',metadata:{product:'mskdsp-upper',version:'<img src=x onerror=alert(1)>',platform:'windows-x86_64-nsis',file_name:'installer.exe',size:2048,sha256:'a'.repeat(64),notes:'<script>alert(1)</script>'},ever_published:true});
  const page=await open({records:[malicious,record({id:'lower',metadata:{product:'mskdsp-lower',version:'1.0.0',platform:'linux-x64',file_name:'lower.tar.gz',size:1024,sha256:'b'.repeat(64)}})]});
  assert.equal(page.calls[0].path,'/api/v1/auth/me');
  assert.equal(page.calls[1].path,'/api/v1/software/packages');
  assert.equal(page.calls.length,2); assert.equal(page.ids.username.textContent,'operator');
  const nodes=page.flatten(page.ids.packages.children);
  assert(nodes.some(node=>node.tag==='h2'&&node.textContent===malicious.metadata.version));
  assert(nodes.some(node=>node.tag==='p'&&node.textContent===malicious.metadata.notes));
  assert(nodes.some(node=>node.tag==='a'&&node.href==='/software/packages/id%2F%3Cscript%3E/installer.exe'));
  page.ids.product.value='mskdsp-lower'; page.ids.product.events.change();
  assert.equal(page.ids.packages.children.length,1);
  const lowerSelect=page.flatten(page.ids.packages.children).find(node=>node.tag==='select');
  assert.equal(lowerSelect.children.length,1); assert.equal(lowerSelect.children[0].value,'ci');
  page.ids.product.value=''; page.ids.product.events.change();
  // 平台包显示完整构建身份和候选状态，只允许跳到程序更新页，不提供发布或下载。
  const platformRecord=record({id:'platform-candidate',metadata:{product:'mskdsp-platform',version:'0.0.1-ci.20261018t094236.6.98+sha.aaaaaaa',platform:'linux-amd64',file_name:'platform-binary.tar.gz',size:1024,sha256:'a'.repeat(64),channel:'ci',source_ref:'main',source_sha:'a'.repeat(40),build_time:'2026-10-18T09:42:36Z'}});
  const candidate=await open({records:[platformRecord,record()]});
  candidate.ids.product.value='mskdsp-platform'; candidate.ids.product.events.change();
  assert.equal(candidate.ids.packages.children.length,1);
  const candidateNodes=candidate.flatten(candidate.ids.packages.children);
  assert(candidateNodes.some(node=>node.textContent==='CI 候选包 · 待手动升级'));
  assert(candidateNodes.some(node=>node.textContent===platformRecord.metadata.build_time));
  assert(candidateNodes.some(node=>node.textContent===platformRecord.metadata.source_sha));
  assert(candidateNodes.some(node=>node.tag==='a'&&node.href==='/update'));
  assert(!candidateNodes.some(node=>node.tag==='button'||node.tag==='select'||node.href?.startsWith('/software/packages/')));
  const candidateCalls=candidate.calls.length;
  await candidate.api.mutate(platformRecord,'ci','publish');
  assert.equal(candidate.calls.length,candidateCalls); assert.equal(candidate.confirms.length,0);
  candidate.ids.product.value=''; candidate.ids.channel.value='ci'; candidate.ids.channel.events.change();
  assert.equal(candidate.ids.packages.children.length,1);
  candidate.ids.channel.value='candidate'; candidate.ids.channel.events.change();
  assert.equal(candidate.ids.packages.children.length,1);
  candidate.ids.channel.value='unpublished'; candidate.ids.channel.events.change();
  assert.equal(candidate.ids.packages.children.length,1);
  assert(candidate.flatten(candidate.ids.packages.children).some(node=>node.textContent==='上位机'));
  // 取消确认不会提交；提交期间立即禁用，拒绝重复操作；仅显式操作才发布。
  page.state.allowConfirm=false;
  await page.api.mutate(malicious,'stable','publish'); assert.equal(page.calls.length,2);
  page.state.allowConfirm=true; page.state.hold=true;
  const publishing=page.api.mutate(malicious,'stable','publish');
  assert.equal(page.api.snapshot().busy,true); assert.equal(page.ids.refresh.disabled,true);
  const count=page.calls.length; await page.api.mutate(malicious,'stable','publish'); assert.equal(page.calls.length,count);
  page.state.finish(); await publishing; page.state.hold=false;
  const post=page.calls.find(call=>call.options.method==='POST');
  assert.equal(post.path,'/api/v1/software/packages/id%2F%3Cscript%3E/publish');
  assert.equal(post.options.headers['X-MskDSP-Action'],'software');
  assert.equal(post.options.credentials,'same-origin');
  assert.deepEqual(JSON.parse(post.options.body),{channel:'stable'});
  assert.equal(page.api.snapshot().busy,false); assert.equal(page.ids.refresh.disabled,false);
  assert(page.ids.message.textContent.includes('已发布'));
  await page.api.mutate(page.state.records[0],'stable','withdraw');
  assert(page.calls.some(call=>call.path.endsWith('/withdraw')));
  assert.equal(page.state.records[0].channels.length,0); assert(page.ids.message.textContent.includes('撤回'));
  // 服务端错误、断网可见且允许刷新恢复，不会自动重复发布。
  page.state.publishFailure=true;
  await page.api.mutate(page.state.records[0],'beta','publish');
  assert.equal(page.ids.message.textContent,'尚未配置公开地址'); assert(page.ids.message.classes.has('error'));
  assert.equal(page.ids.refresh.disabled,false); page.state.publishFailure=false;
  page.state.failList=true; await page.api.refresh();
  assert.equal(page.ids.message.textContent,'网络离线'); assert.equal(page.ids.refresh.disabled,false);
  const posts=page.calls.filter(call=>call.options.method==='POST').length;
  page.state.failList=false; await page.api.refresh();
  assert.equal(page.calls.filter(call=>call.options.method==='POST').length,posts);
  // 会话失效后跳转登录并禁用变更；非管理员不能读取版本列表。
  const denied=await open({user:{username:'viewer',role:'viewer'}});
  assert.equal(denied.calls.length,1); assert.equal(denied.ids.refresh.disabled,true);
  for(const rejectAt of ['/api/v1/auth/me','/api/v1/software/packages']) {
    const expired=await open({rejectAt});
    assert.deepEqual(expired.redirects,['/login']); assert.equal(expired.ids.refresh.disabled,true);
  }
  await page.ids.logout.events.click();
  assert.equal(page.calls.at(-1).path,'/api/v1/auth/logout');
  assert.equal(page.calls.at(-1).options.headers['X-MskDSP-Action'],'auth');
  assert.deepEqual(page.redirects,['/login']);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        result = subprocess.run([node, "-e", "const SCRIPT=" + json.dumps(script) + ";\n" + scenario],
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
