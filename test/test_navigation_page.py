"""统一导航和账号管理页面的实际 JavaScript 行为契约。"""
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
        if tag == 'nav' and 'app-nav' in values.get('class', '').split():
            self.in_navigation = True
        if tag == 'a' and self.in_navigation:
            self.current = {'href': values.get('href'), 'current': values.get('aria-current'), 'text': ''}

    def handle_data(self, data):
        if self.current is not None:
            self.current['text'] += data

    def handle_endtag(self, tag):
        if tag == 'a' and self.current is not None:
            self.current['text'] = self.current['text'].strip()
            self.links.append(self.current)
            self.current = None
        if tag == 'nav':
            self.in_navigation = False


class NavigationPageTests(unittest.TestCase):
    def test_management_navigation_matches_routes(self):
        expected = [('/modbus-templates', '点表工作台'), ('/software', '软件管理'), ('/accounts', '账号管理'), ('/update', '平台维护')]
        for filename, route in [('modbus-templates.html', '/modbus-templates'), ('software.html', '/software'), ('update.html', '/update'), ('accounts.html', '/accounts')]:
            with self.subTest(page=filename):
                parser = NavigationParser()
                page = (ROOT / 'web' / filename).read_text(encoding='utf-8')
                parser.feed(page)
                self.assertEqual([(link['href'], link['text']) for link in parser.links], expected)
                self.assertEqual([link['href'] for link in parser.links if link['current'] == 'page'], [route])
                self.assertNotIn('class="app-sidebar"', page)


HARNESS = r"""
const assert=require('node:assert/strict'),vm=require('node:vm'),context=vm.createContext({TextEncoder});
vm.runInContext(SCRIPT+'\nthis.createAccountClient=createAccountClient;',context);
const record=(fields={})=>({id:'account-1',username:'station01',display_name:'现场账号',enabled:true,revision:1,created_at:100,updated_at:100,last_login_at:null,online_connections:0,...fields});
const response=(status,body)=>({status,ok:status>=200&&status<300,json:async()=>body});
function open(){
 const state={records:[],rejectAt:'',role:'admin',conflict:false,allow:true,hold:false,finish:null,logoutStatus:204,failStatus:0,failMessage:'请求失败',networkFail:false},calls=[],confirms=[],redirects=[];
 const fetchImpl=async(path,options)=>{
  calls.push({path,options});
  if(path===state.rejectAt)return response(401,{error:'会话失效'});
  if(path==='/api/v1/auth/me')return response(200,{username:'root',role:state.role});
  if(path==='/api/v1/auth/logout')return response(state.logoutStatus,{error:'退出暂时失败'});
  if(state.networkFail)throw new Error('断网');
  if(state.failStatus)return response(state.failStatus,{error:state.failMessage});
  const payload=options.body?JSON.parse(options.body):null;
  if(options.method==='GET')return response(200,path==='/api/v1/accounts'?state.records:state.records.find(a=>a.id===path.split('/').at(-1)));
  if(state.hold)await new Promise(resolve=>{state.finish=resolve;});
  if(state.conflict)return response(409,{error:'冲突'});
  if(path==='/api/v1/accounts'){const account=record({username:payload.username,display_name:payload.display_name});state.records.push(account);return response(201,account);}
  const id=path.split('/')[4],index=state.records.findIndex(a=>a.id===id);let account=state.records[index];
  if(payload.revision!==account.revision)return response(409,{error:'冲突'});
  account=options.method==='PUT'?{...account,...payload,revision:account.revision+1}:{...account,revision:account.revision+1};
  state.records[index]=account;return response(200,account);
 };
 const client=context.createAccountClient({fetchImpl,confirmAction:message=>{confirms.push(message);return state.allow;},onUnauthorized:()=>redirects.push('/login')});
 return {client,state,calls,confirms,redirects};
}
async function registered(){const p=open();await p.client.refresh();p.client.newAccount();p.client.edit({username:'  station01  ',display_name:'  现场账号  ',password:'safe-pass-123',confirm:'safe-pass-123'});await p.client.save();return p;}
"""


class AccountPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = (ROOT / 'web/accounts.html').read_text(encoding='utf-8')
        cls.script = re.search(r'<script type="module">([\s\S]*?)</script>', cls.page).group(1)
        cls.client_script = cls.script.split('// 页面渲染只使用 textContent')[0]
        cls.node = shutil.which('node')
        if not cls.node:
            raise AssertionError('账号页面行为验证需要 Node')
        result = subprocess.run([cls.node, '--check', '--input-type=module'], input=cls.script, capture_output=True, text=True, encoding='utf-8', timeout=15)
        if result.returncode:
            raise AssertionError(result.stderr)

    def run_scenario(self, scenario):
        source = 'const SCRIPT=' + json.dumps(self.client_script) + ';\n' + HARNESS + '\n(async()=>{\n' + scenario + '\n})().catch(e=>{console.error(e);process.exitCode=1;});'
        result = subprocess.run([self.node], input=source, capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_create_cookie_action_headers_update_and_password_cleanup(self):
        self.run_scenario(r"""
const p=await registered(),s=p.client.snapshot();assert.equal(s.username,'root');assert.equal(s.selected.username,'station01');assert.equal(s.selected.display_name,'现场账号');assert.equal(s.dirty,false);assert.equal(s.draft.password,undefined);assert.equal(s.draft.confirm,undefined);assert.equal(s.passwordDraft.password,'');
assert.equal(p.calls[0].path,'/api/v1/auth/me');const c=p.calls.find(c=>c.options.method==='POST');assert.equal(c.path,'/api/v1/accounts');assert.equal(c.options.headers['X-MskDSP-Action'],'account');assert.equal(c.options.headers['Content-Type'],'application/json');assert.equal(c.options.credentials,'same-origin');assert.equal(c.options.cache,'no-store');assert.deepEqual(JSON.parse(c.options.body),{username:'station01',password:'safe-pass-123',display_name:'现场账号'});
p.client.edit({username:'cannot-change',display_name:'新显示名'});await p.client.save();const u=p.calls.find(c=>c.options.method==='PUT');assert.deepEqual(JSON.parse(u.options.body),{revision:1,display_name:'新显示名',enabled:true});assert.equal(p.client.snapshot().selected.username,'station01');
        """)

    def test_refresh_preserves_drafts_conflicts_require_reload(self):
        self.run_scenario(r"""
const p=await registered();p.client.edit({display_name:'未保存'});p.state.records[0]={...p.state.records[0],display_name:'他人修改',revision:2,online_connections:1};await p.client.refresh();let s=p.client.snapshot();assert.equal(s.draft.display_name,'未保存');assert.equal(s.draft.revision,1);assert.equal(s.selected.online_connections,1);
await p.client.save();s=p.client.snapshot();assert.equal(s.conflict,true);assert(s.error.includes('重新读取'));assert.equal(s.draft.display_name,'未保存');const count=p.calls.length;await p.client.save();await p.client.resetPassword();assert.equal(p.calls.length,count);await p.client.refresh();assert.equal(p.client.snapshot().conflict,true);await p.client.discard();s=p.client.snapshot();assert.equal(s.draft.display_name,'他人修改');assert.equal(s.draft.revision,2);assert.equal(s.conflict,false);
p.client.editPassword({password:'new-pass-123',confirm:'new-pass-123'});await p.client.refresh();assert.equal(p.client.snapshot().passwordDraft.password,'new-pass-123');p.state.conflict=true;await p.client.resetPassword();s=p.client.snapshot();assert.equal(s.conflict,true);assert.equal(s.passwordDraft.password,'new-pass-123');
        """)

    def test_duplicate_username_keeps_creation_editable_for_retry(self):
        self.run_scenario(r"""
const p=open();await p.client.refresh();p.client.newAccount();p.client.edit({username:'existing',display_name:'新现场',password:'safe-pass-123',confirm:'safe-pass-123'});p.state.failStatus=409;p.state.failMessage='用户名已存在';await p.client.save();let s=p.client.snapshot();assert.equal(s.error,'用户名已存在');assert.equal(s.conflict,false);assert.equal(s.creating,true);assert.equal(s.draft.password,'safe-pass-123');assert.equal(s.draft.display_name,'新现场');p.state.failStatus=0;p.client.edit({username:'available'});await p.client.save();s=p.client.snapshot();assert.equal(s.selected.username,'available');assert.equal(s.creating,false);assert.equal(s.conflict,false);assert.equal(s.draft.password,undefined);
        """)

    def test_confirmations_and_pending_write_prevent_duplicate_operations(self):
        self.run_scenario(r"""
const p=await registered();p.state.allow=false;p.client.edit({enabled:false});let count=p.calls.length;await p.client.save();assert.equal(p.calls.length,count);assert(p.confirms.at(-1).includes('禁用'));p.client.newAccount();assert.equal(p.client.snapshot().creating,false);
p.state.allow=true;p.state.hold=true;const pending=p.client.save();assert.equal(p.client.snapshot().busy,true);count=p.calls.length;await p.client.save();await p.client.refresh();p.client.newAccount();await p.client.resetPassword();assert.equal(p.calls.length,count);p.state.finish();await pending;assert.equal(p.client.snapshot().selected.enabled,false);p.state.hold=false;
p.client.editPassword({password:'reset-pass-123',confirm:'reset-pass-123'});p.state.allow=false;count=p.calls.length;await p.client.resetPassword();assert.equal(p.calls.length,count);assert(p.confirms.at(-1).includes('重置'));p.state.allow=true;await p.client.resetPassword();assert.equal(p.client.snapshot().passwordDraft.password,'');assert.equal(p.client.snapshot().passwordDraft.confirm,'');assert.deepEqual(JSON.parse(p.calls.find(c=>c.path.endsWith('/password')).options.body),{revision:2,password:'reset-pass-123'});
        """)

    def test_passwords_clear_on_switch_unauthorized_logout_and_pagehide(self):
        self.run_scenario(r"""
const p=await registered();p.state.records.push(record({id:'account-2',username:'station02'}));p.client.editPassword({password:'new-pass-123',confirm:'new-pass-123'});await p.client.select('account-2');assert.equal(p.client.snapshot().passwordDraft.password,'');
p.client.newAccount();p.client.edit({username:'draft01',password:'draft-pass',confirm:'draft-pass'});p.client.clearPasswords();assert.equal(p.client.snapshot().draft.password,undefined);assert.equal(p.client.snapshot().draft.confirm,undefined);
p.client.edit({password:'secret-pass',confirm:'secret-pass'});p.state.rejectAt='/api/v1/accounts';await p.client.refresh();let s=p.client.snapshot();assert.equal(s.draft,null);assert.equal(s.passwordDraft.password,'');assert.equal(s.username,'');assert.equal(s.stopped,true);assert.deepEqual(p.redirects,['/login']);const count=p.calls.length;await p.client.refresh();await p.client.save();assert.equal(p.calls.length,count);
const q=await registered();q.client.editPassword({password:'secret-pass',confirm:'secret-pass'});q.state.logoutStatus=500;await q.client.logout();s=q.client.snapshot();assert.equal(s.passwordDraft.password,'');assert.equal(s.stopped,false);assert(s.error.includes('退出暂时失败'));assert.equal(q.redirects.length,0);q.state.logoutStatus=204;await q.client.logout();assert.equal(q.client.snapshot().selected,null);assert.deepEqual(q.redirects,['/login']);assert.equal(q.calls.at(-1).options.headers['X-MskDSP-Action'],'auth');
        """)

    def test_username_and_utf8_password_display_name_boundaries(self):
        self.run_scenario(r"""
const p=open();await p.client.refresh();p.client.newAccount();let count=p.calls.length;
for(const username of ['ab','用戶01','a'.repeat(65),'has space']){p.client.edit({username,password:'password1',confirm:'password1'});await p.client.save();assert.equal(p.calls.length,count);assert(p.client.snapshot().error.includes('用户名'));}
p.client.edit({username:'station01',display_name:'电'.repeat(43)});await p.client.save();assert.equal(p.calls.length,count);assert(p.client.snapshot().error.includes('显示名'));
p.client.edit({display_name:'电'.repeat(42),password:'1234567',confirm:'1234567'});await p.client.save();assert.equal(p.calls.length,count);assert(p.client.snapshot().error.includes('8～128'));
p.client.edit({password:'密'.repeat(43),confirm:'密'.repeat(43)});await p.client.save();assert.equal(p.calls.length,count);assert(p.client.snapshot().error.includes('8～128'));
p.client.edit({password:'password1',confirm:'different'});await p.client.save();assert.equal(p.calls.length,count);assert(p.client.snapshot().error.includes('不一致'));
for(const password of ['        ','　'.repeat(3),'\t'.repeat(8)]){p.client.edit({password,confirm:password});await p.client.save();assert.equal(p.calls.length,count);assert(p.client.snapshot().error.includes('空白'));}
const password='密'.repeat(42)+'ab';p.client.edit({password,confirm:password});await p.client.save();assert.equal(p.client.snapshot().selected.display_name,'电'.repeat(42));assert.equal(JSON.parse(p.calls.at(-1).options.body).password,password);
const q=open();await q.client.refresh();q.client.newAccount();q.client.edit({username:'a'.repeat(64),password:'12345678',confirm:'12345678',display_name:''});await q.client.save();assert.equal(q.client.snapshot().selected.display_name,'a'.repeat(64));
        """)

    def test_api_errors_preserve_draft_and_non_admin_cannot_load_accounts(self):
        self.run_scenario(r"""
for(const status of [400,403,404,429,500]){const p=await registered();p.client.edit({display_name:'保留草稿'});p.state.failStatus=status;p.state.failMessage=`错误${status}`;await p.client.save();assert.equal(p.client.snapshot().draft.display_name,'保留草稿');assert.equal(p.client.snapshot().error,`错误${status}`);assert.equal(p.client.snapshot().busy,false);}
const p=await registered();p.client.edit({display_name:'断网草稿'});p.state.networkFail=true;await p.client.save();assert(p.client.snapshot().error.includes('无法连接'));assert.equal(p.client.snapshot().draft.display_name,'断网草稿');
const q=open();q.state.role='account';await q.client.refresh();assert.deepEqual(q.redirects,['/login']);assert.equal(q.calls.length,1);assert.equal(q.client.snapshot().stopped,true);
        """)

    def test_page_password_accessibility_and_theme_contract(self):
        self.assertNotIn('innerHTML', self.script)
        self.assertNotIn('localStorage', self.script)
        for obsolete in ['credential-dialog', 'site_id', 'auth_version']:
            self.assertNotIn(obsolete, self.page)
        self.assertIn('type="password"', self.page)
        self.assertIn('autocomplete="new-password"', self.page)
        self.assertIn("addEventListener('pagehide', () => client.clearPasswords())", self.script)
        self.assertIn('没有 Web 管理权限', self.page)
        self.assertIn('aria-live="polite"', self.page)
        self.assertIn("$('account-username').readOnly = !state.creating", self.script)
        theme = re.search(r'<script id="theme-script">([\s\S]*?)</script>', self.page).group(1)
        for filename in ['login.html', 'modbus-templates.html', 'software.html', 'update.html']:
            page = (ROOT / 'web' / filename).read_text(encoding='utf-8')
            self.assertEqual(theme, re.search(r'<script id="theme-script">([\s\S]*?)</script>', page).group(1))


if __name__ == '__main__':
    unittest.main()
