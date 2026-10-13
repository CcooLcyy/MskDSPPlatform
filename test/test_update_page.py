"""更新页面契约与浏览器状态逻辑测试，仅使用 Python/Node 标准库。"""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "web" / "update.html"


class UpdatePageTests(unittest.TestCase):
    # 更新页共用平台导航，当前版本、可用版本、任务状态分别呈现。
    def test_shared_shell_and_separate_update_regions(self):
        page = PAGE.read_text(encoding="utf-8")
        self.assertIn('href="/assets/platform.css"', page)
        for css_class in ('app-shell', 'app-sidebar', 'app-topbar', 'page-heading'):
            self.assertIn(f'class="{css_class}"', page)
        self.assertIn('href="/modbus-templates#trash"', page)
        self.assertIn('aria-labelledby="current-heading"', page)
        self.assertIn('aria-labelledby="available-heading"', page)
        self.assertIn('id="task-status"', page)
        self.assertNotIn('技术详情', page)
        self.assertNotIn('更新来源', page)
        self.assertNotIn('source_url', page)
        self.assertNotIn('--page:', page)
        self.assertNotIn("document.querySelector('section')", page)

    # 所有动态内容走文本赋值，网页不自动提交检查或安装请求。
    def test_page_contract(self):
        page = PAGE.read_text(encoding="utf-8")
        self.assertIn('name="viewport"', page)
        for field in ("current", "latest", "channel", "state", "message", "check", "apply", "username", "logout"):
            self.assertIn(f'id="{field}"', page)
        self.assertNotIn("innerHTML", page)
        self.assertIn("textContent", page)
        self.assertIn("/api/update/status", page)
        self.assertIn("X-MskDSP-Action", page)
        self.assertIn("/readyz", page)
        self.assertIn("/api/v1/auth/me", page)
        self.assertIn("/api/v1/auth/logout", page)

    # 用隔离的 JavaScript 环境验证请求、断线恢复、按钮与刷新边界。
    def test_client_behavior(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("未找到 Node，页面静态契约检查仍执行")
        page = PAGE.read_text(encoding="utf-8")
        script = re.search(r'<script type="module">([\s\S]*?)</script>', page).group(1)
        syntax = subprocess.run([node, "--check", "--input-type=module"], input=script, capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        library = script.split("// 页面启动")[0]
        scenario = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const context = vm.createContext({AbortController, setTimeout, clearTimeout, URL});
vm.runInContext(LIBRARY + '\nthis.api = {getView, createUpdateClient};', context);
const {getView, createUpdateClient} = context.api;
const status = (fields = {}) => ({enabled:true,current_version:'1',latest_version:'2',channel:'ci',source_url:'https://example.com/ci/latest.json',state:'available',message:'',update_available:true,job_id:'',...fields});
const calls = [];
let current = status(), reloads = 0, fail = false, postStatus = null, ready = true;
const client = createUpdateClient({
  fetchImpl: async (url, options) => {
    calls.push({url,options});
    if(fail) throw new Error('离线');
    if(url === '/api/v1/auth/me') return {ok:true,status:200,json:async()=>({username:'root',role:'admin'})};
    if(url === '/readyz') return {ok:ready,status:ready?200:503,json:async()=>({status:ready?'ready':'starting'})};
    if(options.method === 'POST') {
      if(postStatus === 409) return {ok:false,status:409,json:async()=>({error:'已有任务'})};
      return {ok:true,status:202,json:async()=>postStatus || status({state:'updating',job_id:'own-job'})};
    }
    return {ok:true,status:200,json:async()=>current};
  }, onChange:()=>{}, reload:()=>reloads++
});
(async()=>{
  await client.refresh();
  assert.equal(calls.length,2);
  assert.equal(calls[0].url,'/api/v1/auth/me');
  assert.equal(calls[1].url,'/api/update/status');
  assert.equal(client.snapshot().username,'root');
  // 会话展示跟随配置的管理员用户名，严格校验管理员角色和非空用户名。
  for(const user of [{username:'operator',role:'admin'},{username:' ',role:'admin'},{username:7,role:'admin'},{username:'operator',role:'viewer'}]) {
    const customClient=createUpdateClient({
      fetchImpl:async(url)=>({ok:true,status:200,json:async()=>url==='/api/v1/auth/me'?user:status()}),
      onChange:()=>{},reload:()=>{}
    });
    await customClient.refresh();
    const valid=user.username==='operator' && user.role==='admin';
    assert.equal(customClient.snapshot().username,valid?'operator':'');
    assert.equal(customClient.snapshot().view.checkDisabled,!valid);
  }
  assert.equal(calls[0].options.method,'GET');
  assert.equal(getView(status({enabled:false})).checkDisabled,true);
  assert.equal(getView(status({enabled:false})).applyDisabled,true);
  assert.equal(getView(status({state:'checking'})).checkDisabled,true);
  assert.equal(getView(status({state:'updating'})).applyDisabled,true);
  assert.equal(getView(status({update_available:false})).applyDisabled,true);
  current=status({state:'succeeded',current_version:'2',job_id:'history'});
  await client.refresh(); assert.equal(reloads,0);
  current=status(); await client.refresh();
  await client.action('check');
  assert.equal(calls.at(-1).options.headers['X-MskDSP-Action'],'update');
  assert.equal(calls.at(-1).options.body,'{}');
  current=status({state:'succeeded',current_version:'2',job_id:'own-job'});
  await client.refresh(); assert.equal(reloads,0);
  current=status(); await client.refresh();
  postStatus=status({state:'current',current_version:'1',job_id:'noop'});
  await client.action('apply');
  current=status({state:'succeeded',current_version:'2',job_id:'noop'});
  await client.refresh(); assert.equal(reloads,0);
  current=status(); await client.refresh(); postStatus=409;
  await client.action('apply'); assert.match(client.snapshot().actionError,/已有任务/);
  current=status(); postStatus=null; await client.refresh();
  fail=true; await client.action('apply');
  const posts=calls.filter(c=>c.options.method==='POST').length;
  await client.refresh(); fail=false; current=status(); await client.refresh();
  assert.equal(calls.filter(c=>c.options.method==='POST').length,posts);
  await client.action('apply');
  current=status({state:'error',job_id:'own-job',message:'更新失败，已回滚'});
  await client.refresh(); assert.equal(reloads,0);
  current=status({state:'succeeded',current_version:'1',job_id:'own-job'});
  await client.refresh(); assert.equal(reloads,0);
  current=status({state:'succeeded',current_version:'2',job_id:'other-job'});
  await client.refresh(); assert.equal(reloads,0);
  current=status({state:'succeeded',current_version:'2',job_id:'own-job'}); ready=false;
  await client.refresh(); assert.equal(reloads,0);
  ready=true; await client.refresh(); assert.equal(reloads,1);
  await client.refresh(); assert.equal(reloads,1);
  assert.equal(calls.at(-2).url,'/readyz');
  // 慢状态读取期间按钮立即禁用，读取结束后恢复，避免用户点击被静默忽略。
  let slow=false, finishStatus, slowPosts=0;
  const slowClient=createUpdateClient({
    fetchImpl: async (_url,options) => {
      if(_url === '/api/v1/auth/me') return {ok:true,status:200,json:async()=>({username:'root',role:'admin'})};
      if(options.method==='POST') slowPosts++;
      if(slow && options.method==='GET') return await new Promise(resolve=>{finishStatus=resolve});
      return {ok:true,status:200,json:async()=>status()};
    }, onChange:()=>{}, reload:()=>{}
  });
  await slowClient.refresh();
  assert.equal(slowClient.snapshot().view.checkDisabled,false);
  assert.equal(slowClient.snapshot().view.applyDisabled,false);
  slow=true;
  const reading=slowClient.refresh();
  assert.equal(slowClient.snapshot().refreshing,true);
  assert.equal(slowClient.snapshot().view.checkDisabled,true);
  assert.equal(slowClient.snapshot().view.applyDisabled,true);
  await slowClient.action('apply'); assert.equal(slowPosts,0);
  finishStatus({ok:true,status:200,json:async()=>status()});
  await reading; slow=false;
  assert.equal(slowClient.snapshot().refreshing,false);
  assert.equal(slowClient.snapshot().view.checkDisabled,false);
  assert.equal(slowClient.snapshot().view.applyDisabled,false);
  await slowClient.action('check'); assert.equal(slowPosts,1);
  // 会话失效后禁用更新并终止请求，登录跳转只发生一次。
  for(const rejectedPath of ['/api/v1/auth/me','/api/update/status','/api/update/apply']) {
    let reject=false, redirects=0;
    const authCalls=[];
    const authClient=createUpdateClient({
      fetchImpl:async(url,options)=>{
        authCalls.push({url,options});
        if(url===rejectedPath && reject) return {ok:false,status:401,json:async()=>({error:'请登录'})};
        return {ok:true,status:200,json:async()=>url==='/api/v1/auth/me'?{username:'root',role:'admin'}:status()};
      },onChange:()=>{},reload:()=>{},onUnauthorized:()=>redirects++
    });
    assert.equal(authClient.snapshot().view.checkDisabled,true);
    if(rejectedPath!=='/api/v1/auth/me') await authClient.refresh();
    reject=true;
    if(rejectedPath==='/api/update/apply') await authClient.action('apply'); else await authClient.refresh();
    assert.equal(redirects,1);
    assert.equal(authClient.snapshot().stopped,true);
    assert.equal(authClient.snapshot().view.checkDisabled,true);
    const count=authCalls.length;
    await authClient.refresh(); await authClient.action('check'); await authClient.logout();
    assert.equal(authCalls.length,count);
  }
  // 退出等待期间不允许更新，成功无需解析 204 响应体。
  let finishLogout, logoutFails=false, logoutRedirects=0;
  const logoutCalls=[];
  const logoutClient=createUpdateClient({
    fetchImpl:async(url,options)=>{
      logoutCalls.push({url,options});
      if(url==='/api/v1/auth/logout') {
        if(logoutFails) throw new Error('离线');
        return await new Promise(resolve=>{finishLogout=resolve});
      }
      return {ok:true,status:200,json:async()=>url==='/api/v1/auth/me'?{username:'root',role:'admin'}:status()};
    },onChange:()=>{},reload:()=>{},onUnauthorized:()=>logoutRedirects++
  });
  await logoutClient.refresh(); logoutFails=true;
  await logoutClient.logout();
  assert.equal(logoutRedirects,0);
  assert.match(logoutClient.snapshot().actionError,/退出/);
  logoutFails=false;
  const exiting=logoutClient.logout();
  assert.equal(logoutClient.snapshot().loggingOut,true);
  assert.equal(logoutClient.snapshot().view.applyDisabled,true);
  const before=logoutCalls.length;
  await logoutClient.action('check'); await logoutClient.logout();
  assert.equal(logoutCalls.length,before);
  assert.equal(logoutCalls.at(-1).options.headers['X-MskDSP-Action'],'auth');
  assert.equal(logoutCalls.at(-1).options.credentials,'same-origin');
  assert.equal(logoutCalls.at(-1).options.body,'{}');
  finishLogout({ok:true,status:204,json:async()=>{throw new Error('无响应体')}});
  await exiting;
  assert.equal(logoutRedirects,1);
  assert.equal(logoutClient.snapshot().stopped,true);
  // 退出与更新就绪探测重叠时，不再自动刷新已退出的页面。
  let raceStatus=status(), finishReady, raceReloads=0;
  const raceClient=createUpdateClient({
    fetchImpl:async(url)=>{
      if(url==='/api/v1/auth/me') return {ok:true,status:200,json:async()=>({username:'root',role:'admin'})};
      if(url==='/api/v1/auth/logout') return {ok:true,status:204};
      if(url==='/readyz') return await new Promise(resolve=>{finishReady=resolve});
      if(url==='/api/update/apply') return {ok:true,status:202,json:async()=>status({state:'updating',job_id:'race'})};
      return {ok:true,status:200,json:async()=>raceStatus};
    },onChange:()=>{},reload:()=>raceReloads++,onUnauthorized:()=>{}
  });
  await raceClient.refresh(); await raceClient.action('apply');
  raceStatus=status({state:'succeeded',current_version:'2',job_id:'race'});
  const checkingReady=raceClient.refresh();
  while(!finishReady) await Promise.resolve();
  await raceClient.logout();
  finishReady({ok:true,status:200,json:async()=>({status:'ready'})});
  await checkingReady;
  assert.equal(raceReloads,0);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        # 将源码作为 JSON 字符串注入，避免 shell 引号与命令替换。
        import json
        command = "const LIBRARY = " + json.dumps(library) + ";\n" + scenario
        result = subprocess.run([node, "-e", command], capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
