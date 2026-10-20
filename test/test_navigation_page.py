"""统一导航的页面契约，防止跨页入口、选中状态和回收站层级漂移。"""
from html.parser import HTMLParser
import json
import re
import shutil
import subprocess
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class NavigationParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_navigation = False
        self.current = None
        self.links = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "nav" and "app-nav" in values.get("class", "").split():
            self.in_navigation = True
        if tag == "a" and self.in_navigation:
            self.current = {"href": values.get("href"), "current": values.get("aria-current"), "text": ""}

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self.current is not None:
            self.current["text"] = self.current["text"].strip()
            self.links.append(self.current)
            self.current = None
        if tag == "nav":
            self.in_navigation = False


class NavigationPageTests(unittest.TestCase):
    # 四个管理页入口相同，且仅当前页面高亮，不把回收站提升到全局导航。
    def test_management_navigation_matches_routes(self):
        expected = [
            ("/modbus-templates", "点表工作台"),
            ("/software", "软件管理"),
            ("/agents", "上位机管理"),
            ("/update", "平台维护"),
        ]
        for filename, route in (("modbus-templates.html", "/modbus-templates"), ("software.html", "/software"), ("update.html", "/update"), ("agents.html", "/agents")):
            with self.subTest(page=filename):
                parser = NavigationParser()
                page = (ROOT / "web" / filename).read_text(encoding="utf-8")
                parser.feed(page)
                self.assertEqual([(link["href"], link["text"]) for link in parser.links], expected)
                self.assertEqual([link["href"] for link in parser.links if link["current"] == "page"], [route])
                self.assertNotIn('class="app-sidebar"', page)
                self.assertNotIn('程序更新页', page)


AGENT_HARNESS = r"""
const assert = require('node:assert/strict'), vm = require('node:vm');
const context=vm.createContext({TextEncoder,AbortController,setTimeout,clearTimeout});
vm.runInContext(SCRIPT+'\nthis.createAgentClient=createAgentClient;',context);
const record=(fields={})=>({id:'agent-1',name:'电站上位机',site_id:'现场甲',notes:'',enabled:true,credential_active:true,credential_version:1,revision:1,created_at:100,updated_at:100,last_seen_at:null,agent_version:'',online:false,connection_id:null,...fields});
const response=(status,body)=>({status,ok:status>=200&&status<300,json:async()=>body});
function open(eventTimeoutMs=10000){
 const state={records:[],events:[],rejectAt:'',conflict:false,allow:true,hold:false,finish:null,logoutStatus:204,eventFail:false,holdEvents:false,finishEvents:null},calls=[],confirms=[],redirects=[],snapshots=[];
 const fetchImpl=async(path,options)=>{
  calls.push({path,options});
  if(path===state.rejectAt)return response(401,{error:'会话失效'});
  if(path==='/api/v1/auth/me')return response(200,{username:'admin',role:'admin'});
  if(path==='/api/v1/auth/logout')return response(state.logoutStatus,{error:'退出暂时失败'});
  if(path.endsWith('/events')){if(state.holdEvents)await new Promise(resolve=>{state.finishEvents=resolve;options.signal?.addEventListener('abort',()=>resolve());});return response(state.eventFail?500:200,state.eventFail?{error:'记录暂不可用'}:state.events);}
  const payload=options.body?JSON.parse(options.body):null;
  if(options.method==='GET')return response(200,path==='/api/v1/agents'?state.records:state.records.find(a=>a.id===path.split('/').at(-1)));
  if(state.hold)await new Promise(resolve=>{state.finish=resolve;});
  if(state.conflict)return response(409,{error:'冲突'});
  if(path==='/api/v1/agents'){
   const agent=record({...payload});state.records.push(agent);return response(201,{agent,token:'one-time-secret'});
  }
  let agent=state.records[0],action=path.split('/').at(-1);
  if(payload.revision!==agent.revision)return response(409,{error:'冲突'});
  if(options.method==='PUT')agent={...agent,...payload,revision:agent.revision+1};
  else if(action==='rotate')agent={...agent,credential_active:true,credential_version:agent.credential_version+1,revision:agent.revision+1};
  else if(action==='revoke')agent={...agent,credential_active:false,revision:agent.revision+1};
  else if(action==='unbind')agent={...agent,site_id:'',enabled:false,credential_active:false,revision:agent.revision+1};
  state.records[0]=agent;return response(200,action==='rotate'?{agent,token:'rotated-secret'}:agent);
 };
 const client=context.createAgentClient({fetchImpl,eventTimeoutMs,confirmAction:message=>{confirms.push(message);return state.allow;},onUnauthorized:()=>redirects.push('/login'),onChange:s=>snapshots.push(s)});
 return {client,state,calls,confirms,redirects,snapshots};
}
async function registered(){const p=open();await p.client.refresh();p.client.newAgent();p.client.edit({name:'  现场上位机  ',site_id:'  现场甲  ',notes:'  备注  '});await p.client.save();return p;}
"""


class AgentIdentityPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = (ROOT / 'web/agents.html').read_text(encoding='utf-8')
        cls.script = re.search(r'<script type="module">([\s\S]*?)</script>', cls.page).group(1)
        cls.client_script = cls.script.split('// 页面渲染只使用 textContent')[0]
        cls.node = shutil.which('node')
        if not cls.node:
            raise AssertionError('上位机页面行为验证需要 Node')
        result = subprocess.run([cls.node, '--check', '--input-type=module'], input=cls.script,
                                capture_output=True, text=True, encoding='utf-8', timeout=15)
        if result.returncode:
            raise AssertionError(result.stderr)

    def run_scenario(self, scenario):
        result = subprocess.run([self.node], input='const SCRIPT=' + json.dumps(self.client_script) + ';\n' + AGENT_HARNESS + '\n(async()=>{\n' + scenario + '\n})().catch(e=>{console.error(e);process.exitCode=1;});',
                                capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_registration_secret_lifetime_and_write_headers(self):
        self.run_scenario(r"""
const p=await registered();let s=p.client.snapshot();assert.equal(s.username,'admin');assert.equal(s.selected.name,'现场上位机');assert.equal(s.token,'one-time-secret');assert.equal(s.dirty,false);
assert.equal(p.calls[0].path,'/api/v1/auth/me');const create=p.calls.find(c=>c.options.method==='POST');assert.equal(create.options.headers['X-MskDSP-Action'],'agent-identity');assert.equal(create.options.headers['Content-Type'],'application/json');assert.equal(create.options.credentials,'same-origin');assert.equal(create.options.cache,'no-store');assert.deepEqual(JSON.parse(create.options.body),{name:'现场上位机',site_id:'现场甲',notes:'备注'});
p.client.closeToken();assert.equal(p.client.snapshot().token,'');assert.equal(p.client.snapshot().tokenAgent,'');
await p.client.credential('rotate');assert.equal(p.client.snapshot().token,'rotated-secret');assert.equal(p.client.snapshot().selected.credential_version,2);
await p.client.logout();assert.equal(p.client.snapshot().token,'');assert.equal(p.client.snapshot().selected,null);assert.equal(p.client.snapshot().stopped,true);assert.deepEqual(p.redirects,['/login']);const last=p.calls.at(-1);assert.equal(last.options.headers['X-MskDSP-Action'],'auth');
        """)

    def test_refresh_retains_draft_and_revision_conflict_requires_reload(self):
        self.run_scenario(r"""
const p=await registered();p.client.closeToken();p.client.edit({name:'未保存名称'});p.state.records[0]={...p.state.records[0],name:'其他管理员修改',revision:2,online:true};await p.client.refresh();let s=p.client.snapshot();assert.equal(s.draft.name,'未保存名称');assert.equal(s.draft.revision,1);assert.equal(s.selected.online,true);assert.equal(s.dirty,true);
await p.client.save();s=p.client.snapshot();assert.equal(s.conflict,true);assert(s.error.includes('重新读取'));const count=p.calls.length;await p.client.save();await p.client.credential('rotate');assert.equal(p.calls.length,count);
await p.client.refresh();assert.equal(p.client.snapshot().draft.name,'未保存名称');assert.equal(p.client.snapshot().conflict,true);await p.client.discard();assert.equal(p.client.snapshot().draft.name,'其他管理员修改');assert.equal(p.client.snapshot().draft.revision,2);assert.equal(p.client.snapshot().conflict,false);
        """)

    def test_confirmations_dirty_protection_and_single_pending_write(self):
        self.run_scenario(r"""
const p=await registered();p.client.closeToken();p.state.allow=false;const count=p.calls.length;await p.client.credential('revoke');await p.client.credential('unbind');assert.equal(p.calls.length,count);p.client.edit({enabled:false});await p.client.save();assert.equal(p.calls.length,count);assert(p.confirms.at(-1).includes('禁用'));p.client.newAgent();assert.equal(p.client.snapshot().creating,false);
p.state.allow=true;p.state.hold=true;const pending=p.client.save();assert.equal(p.client.snapshot().busy,true);const held=p.calls.length;await p.client.save();await p.client.refresh();p.client.newAgent();await p.client.credential('rotate');assert.equal(p.calls.length,held);p.state.finish();await pending;assert.equal(p.client.snapshot().selected.enabled,false);p.state.hold=false;
await p.client.credential('unbind');const s=p.client.snapshot();assert.equal(s.selected.site_id,'');assert.equal(s.selected.credential_active,false);assert.equal(s.selected.enabled,false);const unbind=p.calls.find(c=>c.path.endsWith('/unbind'));assert.deepEqual(JSON.parse(unbind.options.body),{revision:2});
        """)

    def test_unauthorized_and_failed_logout_clear_one_time_credentials(self):
        self.run_scenario(r"""
for(const rejectAt of ['/api/v1/agents','/api/v1/agents/agent-1/events']){
 const p=await registered();p.state.rejectAt=rejectAt;await p.client.refresh();assert.equal(p.client.snapshot().token,'');assert.equal(p.client.snapshot().username,'');assert.equal(p.client.snapshot().stopped,true);assert.deepEqual(p.redirects,['/login']);const count=p.calls.length;await p.client.refresh();await p.client.save();assert.equal(p.calls.length,count);
}
const p=await registered();p.state.logoutStatus=500;await p.client.logout();assert.equal(p.client.snapshot().token,'');assert.equal(p.client.snapshot().stopped,false);assert(p.client.snapshot().error.includes('退出暂时失败'));assert.equal(p.redirects.length,0);
        """)

    def test_event_failure_does_not_undo_success_or_lose_credential(self):
        self.run_scenario(r"""
const p=open();await p.client.refresh();p.client.newAgent();p.client.edit({name:'现场电脑'});p.state.eventFail=true;await p.client.save();await new Promise(r=>setImmediate(r));const s=p.client.snapshot();assert.equal(s.selected.name,'现场电脑');assert.equal(s.token,'one-time-secret');assert.equal(s.error,'');assert(s.eventError.includes('记录读取失败'));
        """)

    def test_hanging_event_read_never_delays_credential_or_keeps_form_busy(self):
        self.run_scenario(r"""
const p=open(25);await p.client.refresh();p.client.newAgent();p.client.edit({name:'现场电脑'});p.state.holdEvents=true;await p.client.save();let s=p.client.snapshot();assert.equal(s.busy,false);assert.equal(s.token,'one-time-secret');assert.equal(s.eventsBusy,true);assert(p.snapshots.some(s=>s.token==='one-time-secret'));p.client.closeToken();assert.equal(p.client.snapshot().token,'');await new Promise(r=>setTimeout(r,50));s=p.client.snapshot();assert.equal(s.eventsBusy,false);assert(s.eventError.includes('超时'));assert.equal(s.error,'');
await p.client.credential('rotate');s=p.client.snapshot();assert.equal(s.busy,false);assert.equal(s.token,'rotated-secret');assert.equal(s.eventsBusy,true);p.client.closeToken();p.state.finishEvents();await new Promise(r=>setImmediate(r));assert.equal(p.client.snapshot().eventsBusy,false);
        """)

    def test_multibyte_fields_checked_before_registration(self):
        self.run_scenario(r"""
const p=open();await p.client.refresh();p.client.newAgent();p.client.edit({name:'电'.repeat(43)});const count=p.calls.length;await p.client.save();assert.equal(p.calls.length,count);assert(p.client.snapshot().error.includes('上位机名称'));p.client.edit({name:'现场',notes:'备'.repeat(683)});await p.client.save();assert.equal(p.calls.length,count);assert(p.client.snapshot().error.includes('备注'));p.client.edit({notes:'备'.repeat(682)});await p.client.save();assert.equal(p.client.snapshot().selected.name,'现场');
        """)

    def test_page_has_safe_secret_and_accessibility_contract(self):
        self.assertNotIn('innerHTML', self.script)
        self.assertNotIn('localStorage', self.script)
        self.assertIn("$('credential-value').value = ''", self.script)
        self.assertIn("$('credential-value').textContent = ''", self.script)
        self.assertIn('aria-labelledby="credential-title"', self.page)
        self.assertIn('aria-describedby="credential-description"', self.page)
        self.assertIn("addEventListener('cancel'", self.script)
        self.assertIn("addEventListener('pagehide', () => client.closeToken())", self.script)
        self.assertIn('平台仅保存摘要', self.page)
        self.assertNotIn('远程控制</button>', self.page)


if __name__ == '__main__':
    unittest.main()
