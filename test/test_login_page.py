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
