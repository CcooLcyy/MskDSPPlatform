"""更新页面契约与浏览器状态逻辑测试，仅使用 Python/Node 标准库。"""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "web" / "update.html"


class UpdatePageTests(unittest.TestCase):
    # 所有动态内容走文本赋值，网页不自动提交检查或安装请求。
    def test_page_contract(self):
        page = PAGE.read_text(encoding="utf-8")
        self.assertIn('name="viewport"', page)
        for field in ("current", "latest", "channel", "source", "state", "message", "check", "apply"):
            self.assertIn(f'id="{field}"', page)
        self.assertNotIn("innerHTML", page)
        self.assertIn("textContent", page)
        self.assertIn("/api/update/status", page)
        self.assertIn("X-MskDSP-Action", page)
        self.assertIn("/readyz", page)

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
  assert.equal(calls.length,1);
  assert.equal(calls[0].url,'/api/update/status');
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
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        # 将源码作为 JSON 字符串注入，避免 shell 引号与命令替换。
        import json
        command = "const LIBRARY = " + json.dumps(library) + ";\n" + scenario
        result = subprocess.run([node, "-e", command], capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
