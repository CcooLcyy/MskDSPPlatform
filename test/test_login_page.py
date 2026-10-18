"""管理员登录页面契约与请求行为测试，仅使用 Python/Node 标准库。"""
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

PAGE = Path(__file__).resolve().parents[1] / "web" / "login.html"


class LoginPageTests(unittest.TestCase):
    # 登录入口使用平台共用样式，保留主题与输入辅助信息，品牌说明与表单分开。
    def test_shared_login_layout_and_accessibility(self):
        page = PAGE.read_text(encoding="utf-8")
        self.assertIn('href="/assets/platform.css"', page)
        self.assertIn('class="login-layout"', page)
        self.assertIn('class="login-brand"', page)
        self.assertIn('class="login-card panel"', page)
        self.assertIn('aria-describedby="message"', page)
        self.assertIn('for="username"', page)
        self.assertIn('for="password"', page)
        self.assertNotIn('--page:', page)
        self.assertNotIn('http://', page)
        self.assertNotIn('https://', page)

    # 登录表单采用密码输入与浏览器自动填充，不包含示例或默认密码。
    def test_page_contract(self):
        page = PAGE.read_text(encoding="utf-8")
        for field in ("login-form", "username", "password", "submit", "message"):
            self.assertIn(f'id="{field}"', page)
        self.assertIn('type="password"', page)
        self.assertIn('autocomplete="username"', page)
        self.assertIn('autocomplete="current-password"', page)
        self.assertIn('name="viewport"', page)
        self.assertNotIn("innerHTML", page)
        self.assertIn("textContent", page)
        self.assertIn("/api/v1/auth/login", page)
        self.assertIn("window.location.replace('/modbus-templates')", page)
        self.assertNotRegex(page, r'type="password"[^>]*value=')

    def _run_remember_scenario(self, scenario, *, whole_page=False):
        node = shutil.which("node")
        if not node:
            self.skipTest("未找到 Node，页面静态契约检查仍执行")
        page = PAGE.read_text(encoding="utf-8")
        script = re.search(r'<script type="module">([\s\S]*?)</script>', page).group(1)
        source = script if whole_page else script.split("// 页面启动")[0]
        harness = r'''
const assert=require('node:assert/strict');
const vm=require('node:vm');
const key='mskdsp-platform-remembered-login';
const themeKey='mskdsp-platform-theme';
const data=new Map([[themeKey,'dark']]);
const storage={getItem:k=>data.has(k)?data.get(k):null,setItem:(k,v)=>data.set(k,String(v)),removeItem:k=>data.delete(k)};
const context=vm.createContext({AbortController,TextEncoder,setTimeout,clearTimeout});
'''
        if not whole_page:
            harness += "vm.runInContext(SOURCE+'\\nthis.api={createLoginMemory,restoreRememberedLogin,createLoginClient};',context);\nconst {createLoginMemory,restoreRememberedLogin,createLoginClient}=context.api;\n"
        result = subprocess.run(
            [node, "-e", "const SOURCE=" + json.dumps(source) + ";\n" + harness + scenario],
            capture_output=True, text=True, encoding="utf-8", timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    # 记住选项默认关闭，保留密码管理器提示，控件关联清晰。
    def test_remember_password_contract(self):
        page = PAGE.read_text(encoding="utf-8")
        checkbox = re.search(r'<input\b[^>]*\bid="remember-password"[^>]*>', page)
        self.assertIsNotNone(checkbox)
        self.assertIn('type="checkbox"', checkbox.group())
        self.assertNotRegex(checkbox.group(), r'\bchecked\b')
        self.assertIn('for="remember-password"', page)
        self.assertIn("记住密码", page)

    # 只读写自己的键；损坏或超限记录清理；存储拒绝访问时返回提示。
    def test_remember_storage_boundaries(self):
        self._run_remember_scenario(r'''
const memory=createLoginMemory(storage);
assert.equal(memory.load(),null);
assert.equal(memory.save('operator','test-password'),true);
assert.deepEqual(JSON.parse(data.get(key)),{version:1,username:'operator',password:'test-password'});
assert.equal(JSON.stringify(memory.load()),JSON.stringify({username:'operator',password:'test-password'}));
assert.equal(createLoginMemory(storage).load().password,'test-password');
assert.equal(data.get(themeKey),'dark');
assert.equal(memory.clear(),true); assert.equal(memory.load(),null);
for(const invalid of ['{','null','[]',JSON.stringify({version:2,username:'operator',password:'p'}),
  JSON.stringify({version:1,username:7,password:'p'}),JSON.stringify({version:1,username:' ',password:'p'}),
  JSON.stringify({version:1,username:'operator',password:''}),JSON.stringify({version:1,username:'operator',password:7}),
  JSON.stringify({version:1,username:'u'.repeat(129),password:'p'}),JSON.stringify({version:1,username:'u',password:'p'.repeat(1025)}),
  JSON.stringify({version:1,username:'u',password:'密'.repeat(342)})]) {
  data.set(key,invalid); assert.equal(memory.load(),null); assert.equal(data.has(key),false);
}
const blocked=createLoginMemory({getItem(){throw Error('blocked')},setItem(){throw Error('blocked')},removeItem(){throw Error('blocked')}});
assert.equal(blocked.load(),null); assert.equal(blocked.save('operator','test-password'),false);
assert.equal(blocked.clear(),false); assert.match(blocked.notice(),/清除/);
assert.equal(data.get(themeKey),'dark');
''')

    # 恢复只在两个输入均为空时填入，保护浏览器自动填充和用户输入。
    def test_restore_remembered_credentials(self):
        self._run_remember_scenario(r'''
const fields=(username='',password='')=>({username:{value:username},password:{value:password},remember:{checked:false}});
const saved={username:'operator',password:'test-password'};
const empty=fields(); restoreRememberedLogin(empty,null);
assert.equal(empty.remember.checked,false); assert.equal(empty.username.value,'');
restoreRememberedLogin(empty,saved);
assert.equal(empty.remember.checked,true); assert.equal(empty.username.value,'operator'); assert.equal(empty.password.value,'test-password');
for(const [username,password] of [['browser-user',''],['','browser-password'],['typed-user','typed-password']]) {
  const existing=fields(username,password); restoreRememberedLogin(existing,saved);
  assert.equal(existing.remember.checked,true); assert.equal(existing.username.value,username); assert.equal(existing.password.value,password);
}
''')

    # 成功才保存；失败不覆盖；未勾选先清旧记录；存储异常不影响认证。
    def test_remember_login_lifecycle(self):
        self._run_remember_scenario(r'''
(async()=>{
  const memory=createLoginMemory(storage);
  const old=JSON.stringify({version:1,username:'saved-user',password:'saved-password'});
  const response=(status,user={username:'operator',role:'admin'})=>({ok:status===200,status,json:async()=>status===200?user:{error:'登录失败'}});
  const make=(fetchImpl,memoryImpl=memory)=>{
    let redirects=0;
    const client=createLoginClient({fetchImpl,memory:memoryImpl,onChange:()=>{},onSuccess:()=>redirects++});
    return {client,redirects:()=>redirects};
  };
  for(const fetchImpl of [async()=>response(401),async()=>response(429),async()=>{throw Error('offline')},
    async()=>response(200,{username:'operator',role:'viewer'}),async()=>response(200,{username:' ',role:'admin'}),
    async()=>({ok:true,status:200,json:async()=>{throw Error('invalid JSON')}})]) {
    data.set(key,old);
    const {client,redirects}=make(fetchImpl); await client.login('operator','new-password',true);
    assert.equal(data.get(key),old); assert.equal(redirects(),0); assert.equal(client.snapshot().pending,false);
    assert.equal(JSON.stringify(client.snapshot()).includes('new-password'),false);
  }
  data.set(key,old);
  const unchecked=make(async()=>{assert.equal(data.has(key),false);return response(401)});
  await unchecked.client.login('operator','new-password',false); assert.equal(data.has(key),false);
  const success=make(async(url,options)=>{
    assert.deepEqual(JSON.parse(options.body),{username:'operator',password:'new-password'});
    return response(200);
  });
  await success.client.login(' operator ','new-password',true);
  assert.equal(success.redirects(),1); assert.equal(memory.load().username,'operator'); assert.equal(memory.load().password,'new-password');
  let resolve,calls=0;
  const duplicate=make(async()=>{calls++;return new Promise(done=>{resolve=done})});
  data.set(key,old);
  const pending=duplicate.client.login('operator','pending-password',true);
  await duplicate.client.login('operator','ignored-password',false);
  assert.equal(calls,1); assert.equal(data.get(key),old);
  resolve(response(200)); await pending;
  assert.equal(memory.load().password,'pending-password');
  await duplicate.client.login('operator','ignored-password',false);
  assert.equal(calls,1); assert.equal(memory.load().password,'pending-password');
  for(const remember of [true,false]) {
    const blocked=createLoginMemory({getItem(){throw Error('blocked')},setItem(){throw Error('blocked')},removeItem(){throw Error('blocked')}});
    const unavailable=make(async()=>response(200),blocked);
    await unavailable.client.login('operator','new-password',remember);
    assert.equal(unavailable.redirects(),1); assert.equal(unavailable.client.snapshot().finished,true);
  }
})().catch(error=>{console.error(error);process.exitCode=1});
''')

    # 执行真实页面启动代码，确认取消即清理、提交期间禁用、成功跳转。
    def test_remember_page_wiring(self):
        self._run_remember_scenario(r'''
const elements=new Map();
const element=id=>{
  if(!elements.has(id)) elements.set(id,{value:'',checked:false,disabled:false,textContent:'',listeners:{},
    addEventListener(type,listener){this.listeners[type]=listener},setAttribute(){}});
  return elements.get(id);
};
data.set(key,JSON.stringify({version:1,username:'operator',password:'saved-password'}));
let finish,redirect='';
context.document={getElementById:element};
context.window={localStorage:storage,fetch:async()=>new Promise(resolve=>{finish=resolve}),location:{replace:path=>redirect=path}};
vm.runInContext(SOURCE,context);
(async()=>{
  assert.equal(element('username').value,'operator'); assert.equal(element('password').value,'saved-password');
  const remember=element('remember-password'); assert.equal(remember.checked,true);
  remember.checked=false; remember.listeners.change({target:remember}); assert.equal(data.has(key),false);
  remember.checked=true; remember.listeners.change({target:remember});
  const submitting=element('login-form').listeners.submit({preventDefault(){}});
  assert.equal(remember.disabled,true); assert.equal(element('submit').disabled,true);
  assert.equal(data.has(key),false);
  finish({ok:true,status:200,json:async()=>({username:'operator',role:'admin'})});
  await submitting;
  assert.equal(redirect,'/modbus-templates'); assert.equal(JSON.parse(data.get(key)).password,'saved-password');
})().catch(error=>{console.error(error);process.exitCode=1});
''', whole_page=True)

    # 验证重复提交、登录失败、限流、断网与成功跳转的请求边界。
    def test_login_behavior(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("未找到 Node，页面静态契约检查仍执行")
        page = PAGE.read_text(encoding="utf-8")
        script = re.search(r'<script type="module">([\s\S]*?)</script>', page).group(1)
        syntax = subprocess.run([node, "--check", "--input-type=module"], input=script,
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        library = script.split("// 页面启动")[0]
        scenario = r'''
const assert=require('node:assert/strict');
const vm=require('node:vm');
const context=vm.createContext({AbortController,setTimeout,clearTimeout});
vm.runInContext(LIBRARY+'\nthis.api={createLoginClient};',context);
const {createLoginClient}=context.api;
let result=401, fail=false, finish, redirects=0;
const calls=[];
const client=createLoginClient({
  fetchImpl:async(url,options)=>{
    calls.push({url,options});
    if(fail) throw new Error('网络错误');
    if(result==='slow') return await new Promise(resolve=>{finish=resolve});
    return {ok:result===200,status:result,json:async()=>result===200?{username:'root',role:'admin'}:{error:result===429?'登录尝试过多，请稍后重试':'用户名或密码错误'}};
  },onChange:()=>{},onSuccess:()=>redirects++
});
(async()=>{
  await client.login('',''); assert.equal(calls.length,0);
  await client.login('root','test-password');
  assert.equal(calls[0].url,'/api/v1/auth/login');
  assert.equal(calls[0].options.method,'POST');
  assert.equal(calls[0].options.credentials,'same-origin');
  assert.equal(calls[0].options.headers['X-MskDSP-Action'],'auth');
  assert.deepEqual(JSON.parse(calls[0].options.body),{username:'root',password:'test-password'});
  assert.match(client.snapshot().error,/用户名或密码错误/);
  assert.equal(redirects,0); assert.equal(client.snapshot().pending,false);
  result=429; await client.login('root','test-password');
  assert.match(client.snapshot().error,/稍后重试/);
  fail=true; await client.login('root','test-password');
  assert.match(client.snapshot().error,/连接/); assert.equal(client.snapshot().pending,false);
  fail=false; result='slow';
  const loggingIn=client.login('root','test-password');
  assert.equal(client.snapshot().pending,true);
  const count=calls.length;
  await client.login('root','test-password'); assert.equal(calls.length,count);
  finish({ok:true,status:200,json:async()=>({username:'root',role:'admin'})});
  await loggingIn; assert.equal(redirects,1);
  await client.login('root','test-password'); assert.equal(calls.length,count);
  // 自定义管理员用户名也能登录，空用户名和非管理员角色仍被拒绝。
  for(const user of [{username:'operator',role:'admin'},{username:' ',role:'admin'},{username:7,role:'admin'},{username:'operator',role:'viewer'}]) {
    let customRedirects=0;
    const customClient=createLoginClient({
      fetchImpl:async()=>({ok:true,status:200,json:async()=>user}),
      onChange:()=>{},onSuccess:()=>customRedirects++
    });
    await customClient.login('operator','test-password');
    assert.equal(customRedirects,user.username==='operator' && user.role==='admin'?1:0);
  }
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        result = subprocess.run([node, "-e", "const LIBRARY=" + json.dumps(library) + ";\n" + scenario],
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
