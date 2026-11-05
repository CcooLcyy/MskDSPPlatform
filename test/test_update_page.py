"""更新页面契约与浏览器状态逻辑测试，仅使用 Python/Node 标准库。"""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "web" / "update.html"


class UpdatePageTests(unittest.TestCase):
    # 后台声明自动策略后页面才显示自动安装；旧管理器与无更新源不能误报。
    def test_update_policy_matches_manager_capability(self):
        page = PAGE.read_text(encoding="utf-8")
        for field in ("update-mode", "update-policy", "update-footer"):
            self.assertIn(f'id="{field}"', page)
        node = shutil.which("node")
        if not node:
            self.skipTest("未找到 Node，页面静态契约检查仍执行")
        script = re.search(r'<script type="module">([\s\S]*?)</script>', page).group(1)
        library = script.split("// 页面启动")[0]
        scenario = r'''
const assert=require('node:assert/strict');
const vm=require('node:vm');
const context=vm.createContext({AbortController,setTimeout,clearTimeout});
vm.runInContext(LIBRARY+'\nthis.api={updatePolicy,createUpdateClient};',context);
const {updatePolicy,createUpdateClient}=context.api;
assert.equal(updatePolicy(null).badge,'正在读取');
const automatic=updatePolicy({enabled:true,auto_update_interval_seconds:60});
assert.equal(automatic.badge,'自动更新');
assert.match(automatic.hint,/每 60 秒/); assert.match(automatic.hint,/自动安装/);
assert.match(automatic.footer,/回退后/); assert.match(automatic.footer,/再次自动安装/);
for(const fields of [{enabled:true},{enabled:true,auto_update_interval_seconds:0},
                     {enabled:true,auto_update_interval_seconds:'60'}]) {
  assert.equal(updatePolicy(fields).badge,'手动更新');
  assert.doesNotMatch(updatePolicy(fields).hint,/自动安装/);
}
assert.equal(updatePolicy({enabled:false,auto_update_interval_seconds:60}).badge,'更新未启用');
// 即使后台自动安装启用，页面刷新仍只查询状态，不发起安装或检查。
const calls=[];
const client=createUpdateClient({fetchImpl:async(url,options)=>{
  calls.push({url,options});
  return {ok:true,status:200,json:async()=>url==='/api/v1/auth/me'?{username:'root',role:'admin'}:
    {enabled:true,auto_update_interval_seconds:60,state:'idle',current_version:'1'}};
},onChange:()=>{},reload:()=>{}});
(async()=>{
  await client.refresh(); await client.refresh();
  assert.ok(calls.every(call=>call.options.method==='GET'));
  assert.ok(calls.every(call=>!['/api/update/check','/api/update/apply'].includes(call.url)));
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        import json
        result = subprocess.run([node, "-e", "const LIBRARY=" + json.dumps(library) + ";\n" + scenario],
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    # 更新页共用平台导航，当前版本、可用版本、任务状态分别呈现。
    def test_shared_shell_and_separate_update_regions(self):
        page = PAGE.read_text(encoding="utf-8")
        self.assertIn('href="/assets/platform.css"', page)
        for css_class in ('app-shell', 'app-topbar', 'page-heading'):
            self.assertIn(f'class="{css_class}"', page)
        self.assertNotIn('href="/modbus-templates#trash"', page)
        self.assertIn('平台维护</a>', page)
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
        for field in ("current", "latest", "previous", "channel", "state", "message", "check", "apply", "rollback", "username", "logout"):
            self.assertIn(f'id="{field}"', page)
        self.assertNotIn("innerHTML", page)
        self.assertIn("textContent", page)
        self.assertIn("/api/update/status", page)
        self.assertIn("X-MskDSP-Action", page)
        self.assertIn("/readyz", page)
        self.assertIn("/api/v1/auth/me", page)
        self.assertIn("/api/v1/auth/logout", page)

    # 安装前可核对版本与缺失说明；取消不提交，连续点击仅提交一次。
    def test_install_confirmation_behavior(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("未找到 Node，页面静态契约检查仍执行")
        page = PAGE.read_text(encoding="utf-8")
        self.assertIn('id="release-notes"', page)
        self.assertIn("confirmApply:review => window.confirm(review)", page)
        script = re.search(r'<script type="module">([\s\S]*?)</script>', page).group(1)
        library = script.split("// 页面启动")[0]
        scenario = r'''
const assert=require('node:assert/strict');
const vm=require('node:vm');
const context=vm.createContext({AbortController,setTimeout,clearTimeout});
vm.runInContext(LIBRARY+'\nthis.api={createUpdateClient,installConfirmation,getView};',context);
const {createUpdateClient,installConfirmation,getView}=context.api;
const status={enabled:true,current_version:'1.0',latest_version:'2.0',state:'available',update_available:true,message:'发现新版本，可手动更新'};
const review=installConfirmation(status);
assert.match(review,/当前版本：1\.0/); assert.match(review,/目标版本：2\.0/);
assert.match(review,/1\.0 → 2\.0/);
assert.match(review,/未提供变更说明/); assert.match(review,/短暂重连/);
assert.match(review,/实际安装版本以任务结果为准/);
assert.doesNotMatch(review,/发现新版本，可手动更新/);
assert.equal(getView({...status,latest_version:''}).applyDisabled,true);
const response=(body,code=200)=>({ok:code<400,status:code,json:async()=>body});
let accept=false,finishPost,calls=[],reviews=[];
const client=createUpdateClient({fetchImpl:async(url,options)=>{
  calls.push({url,options});
  if(url==='/api/v1/auth/me') return response({username:'admin',role:'admin'});
  if(options.method==='POST') return await new Promise(resolve=>{finishPost=resolve});
  return response(status);
},onChange:()=>{},reload:()=>{},confirmApply:review=>{reviews.push(review);return accept}});
(async()=>{
  await client.refresh(); await client.action('apply');
  assert.equal(reviews[0],review); assert.equal(calls.filter(c=>c.options.method==='POST').length,0);
  assert.equal(client.snapshot().pending,false); assert.equal(client.snapshot().view.applyDisabled,false);
  accept=true;
  const sending=client.action('apply'); assert.equal(client.snapshot().pending,true);
  await client.action('apply'); await client.refresh();
  assert.equal(reviews.length,2); assert.equal(calls.filter(c=>c.options.method==='POST').length,1);
  assert.equal(calls.at(-1).url,'/api/update/apply'); assert.equal(calls.at(-1).options.body,'{}');
  finishPost(response({...status,state:'updating',job_id:'job'},202)); await sending;
  assert.equal(client.snapshot().view.applyDisabled,true);
  const unconfirmed=createUpdateClient({fetchImpl:async(url,options)=>{
    assert.notEqual(options.method,'POST');
    return response(url==='/api/v1/auth/me'?{username:'admin',role:'admin'}:status);
  },onChange:()=>{},reload:()=>{}});
  await unconfirmed.refresh(); await unconfirmed.action('apply');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        import json
        result = subprocess.run([node, "-e", "const LIBRARY=" + json.dumps(library) + ";\n" + scenario], capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    # 回退必须确认具体目标；没有更新源时也可用，失败和重连不能重复提交。
    def test_rollback_client_behavior(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("未找到 Node，页面静态契约检查仍执行")
        page = PAGE.read_text(encoding="utf-8")
        script = re.search(r'<script type="module">([\s\S]*?)</script>', page).group(1)
        library = script.split("// 页面启动")[0]
        scenario = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const context = vm.createContext({AbortController, setTimeout, clearTimeout});
vm.runInContext(LIBRARY + '\nthis.api = {getView, createUpdateClient};', context);
const {getView, createUpdateClient} = context.api;
const status = fields => ({enabled:false,current_version:'2',previous_version:'1',rollback_available:true,update_available:false,state:'idle',job_id:'',...fields});
const response = (body, code=200) => ({ok:code<400,status:code,json:async()=>body});
let current=status(), accept=false, fail=false, reject=0, reloads=0, redirects=0, confirms=[], calls=[], finishPost;
const client=createUpdateClient({
  fetchImpl:async(url,options)=>{
    calls.push({url,options});
    if(url==='/api/v1/auth/me') return response({username:'admin',role:'admin'});
    if(url==='/readyz') return response({status:'ready'});
    if(url==='/api/update/rollback') {
      if(fail) throw new Error('离线');
      if(reject) return response({error:'上一版本已变更，请刷新'},reject);
      return await new Promise(resolve=>{finishPost=resolve});
    }
    return response(current);
  },onChange:()=>{},reload:()=>reloads++,onUnauthorized:()=>redirects++,
  confirmRollback:version=>{confirms.push(version);return accept;}
});
(async()=>{
  assert.equal(client.snapshot().view.rollbackDisabled,true);
  await client.refresh();
  assert.equal(client.snapshot().view.checkDisabled,true);
  assert.equal(client.snapshot().view.rollbackDisabled,false);
  assert.equal(calls.some(c=>c.options.method==='POST'),false);
  await client.action('rollback');
  assert.deepEqual(confirms,['1']);
  assert.equal(calls.some(c=>c.options.method==='POST'),false);
  assert.equal(client.snapshot().pending,false);
  for(const fields of [{rollback_available:false},{previous_version:''},{state:'checking'},{state:'updating'},{state:'rolling_back'}]) {
    assert.equal(getView(status(fields)).rollbackDisabled,true);
  }
  for(const flag of ['pending','refreshing','loggingOut','disconnected','unauthorized']) {
    assert.equal(getView(status(),{[flag]:true}).rollbackDisabled,true);
  }
  accept=true;
  const submitting=client.action('rollback');
  assert.equal(client.snapshot().pending,true);
  assert.equal(client.snapshot().view.rollbackDisabled,true);
  const count=calls.length;
  await client.action('rollback'); await client.refresh();
  assert.equal(calls.length,count);
  assert.deepEqual(confirms,['1','1']);
  const post=calls.at(-1);
  assert.equal(post.url,'/api/update/rollback');
  assert.equal(post.options.method,'POST');
  assert.equal(post.options.credentials,'same-origin');
  assert.equal(post.options.headers['X-MskDSP-Action'],'update');
  assert.equal(post.options.body,JSON.stringify({target_version:'1'}));
  finishPost(response(status({state:'rolling_back',job_id:'rollback-job'}),202));
  await submitting;
  assert.equal(client.snapshot().view.rollbackDisabled,true);
  current=status({state:'error',job_id:'rollback-job',message:'回退失败，已恢复当前版本'});
  await client.refresh();
  assert.equal(reloads,0);
  assert.equal(client.snapshot().view.rollbackDisabled,false);
  reject=409;
  await client.action('rollback');
  assert.match(client.snapshot().actionError,/上一版本已变更/);
  assert.equal(client.snapshot().view.rollbackDisabled,false);
  reject=0; fail=true;
  await client.action('rollback');
  assert.equal(client.snapshot().view.rollbackDisabled,true);
  const posts=calls.filter(c=>c.options.method==='POST').length;
  fail=false; await client.refresh();
  assert.equal(calls.filter(c=>c.options.method==='POST').length,posts);
  assert.equal(client.snapshot().view.rollbackDisabled,false);
  // 页面已有历史成功状态不触发刷新，只有本页面发起的成功任务才等待就绪。
  current=status({state:'rolled_back',current_version:'1',job_id:'history',previous_version:'',rollback_available:false});
  await client.refresh(); assert.equal(reloads,0);
  current=status(); await client.refresh();
  const retry=client.action('rollback');
  finishPost(response(status({state:'rolling_back',job_id:'retry'}),202)); await retry;
  current=status({state:'rolled_back',current_version:'1',job_id:'other'});
  await client.refresh(); assert.equal(reloads,0);
  current=status({state:'rolled_back',current_version:'1',job_id:'retry',previous_version:'',rollback_available:false});
  await client.refresh(); assert.equal(reloads,1);
  await client.refresh(); assert.equal(reloads,1);
  assert.equal(client.snapshot().view.rollbackDisabled,true);
  current=status(); await client.refresh(); reject=401;
  await client.action('rollback');
  assert.equal(redirects,1);
  assert.equal(client.snapshot().stopped,true);
  assert.equal(client.snapshot().view.rollbackDisabled,true);
  const stoppedCount=calls.length;
  await client.action('rollback'); await client.refresh();
  assert.equal(calls.length,stoppedCount);
  // 未显式提供确认能力时安全禁用提交；默认行为不得代替管理员确认。
  const unconfirmed=createUpdateClient({fetchImpl:async(url,options)=>{
    assert.notEqual(options.method,'POST');
    return response(url==='/api/v1/auth/me'?{username:'admin',role:'admin'}:status());
  },onChange:()=>{},reload:()=>{}});
  await unconfirmed.refresh(); await unconfirmed.action('rollback');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        import json
        command = "const LIBRARY = " + json.dumps(library) + ";\n" + scenario
        result = subprocess.run([node, "-e", command], capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
  }, onChange:()=>{}, reload:()=>reloads++, confirmApply:()=>true
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
  current=status({state:'empty',latest_version:'',update_available:false,message:'暂无可升级版本'});
  await client.refresh();
  assert.equal(client.snapshot().status.state,'empty');
  assert.equal(client.snapshot().view.checkDisabled,false);
  assert.equal(client.snapshot().view.applyDisabled,true);
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
      },onChange:()=>{},reload:()=>{},onUnauthorized:()=>redirects++,confirmApply:()=>true
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
    },onChange:()=>{},reload:()=>raceReloads++,onUnauthorized:()=>{},confirmApply:()=>true
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
