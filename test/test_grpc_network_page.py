"""网络管理页面的实际请求、冲突与错误显示行为。"""
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class NetworkPageTests(unittest.TestCase):
    def test_page_contract(self):
        page = (ROOT / "web/network.html").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", page)
        for value in ("仅对新建连接生效", "不主动断开现有连接", "平台发送方向", "填入推荐值 1240", 'href="/update"', 'id="theme-script"'):
            self.assertIn(value, page)
        self.assertIn('href="/network"', (ROOT / "web/update.html").read_text(encoding="utf-8"))
        self.assertIn("经 Docker 转发到此端口的外部 IPv4", page)
        self.assertIn("script/grpc_network.py web/network.html", (ROOT / "Dockerfile").read_text(encoding="utf-8"))
        self.assertIn("web/network.html web/update.html", (ROOT / "Dockerfile").read_text(encoding="utf-8"))

    def test_client_only_saves_explicitly_and_handles_conflicts(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("需要 Node 验证 JavaScript")
        page = (ROOT / "web/network.html").read_text(encoding="utf-8")
        script = re.search(r'<script type="module">([\s\S]*?)</script>', page).group(1)
        checked = subprocess.run([node, "--check", "--input-type=module"], input=script, text=True, capture_output=True, encoding="utf-8")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        library = script.split("// 页面渲染只使用 textContent")[0]
        scenario = r'''
const assert = require('node:assert/strict'), vm=require('node:vm');
const timeouts=[];
const context = vm.createContext({AbortController,setTimeout:(callback,ms)=>{timeouts.push(ms);return setTimeout(callback,ms);},clearTimeout});
vm.runInContext(LIBRARY+'\nthis.createNetworkClient=createNetworkClient;',context);
const state={enabled:true,mss:1240,revision:1,port:50000,persistent:true,applied:true,matched_packets:3};
let mode='', calls=[], notices=[], redirect=0;
const response=(code,body)=>({status:code,ok:code===200,json:async()=>body});
const client=context.createNetworkClient({onChange:value=>notices.push(value),onUnauthorized:()=>redirect++,fetchImpl:async(path,options)=>{
  calls.push({path,options});
  if(mode==='abort'){const problem=new Error('timeout');problem.name='AbortError';throw problem;}
  if(mode==='unauth')return response(401,{error:'登录失效'});
  if(mode==='absent')return response(503,{error:'未安装管理服务'});
  if(path==='/api/v1/auth/me')return response(200,{role:'admin',username:'root'});
  if(options.method==='POST'){
    if(mode==='conflict'){state.revision++;return response(409,{error:'冲突'});}
    const payload=JSON.parse(options.body);assert.equal(payload.revision,state.revision);state.revision++;state.mss=payload.mss;return response(200,{...state});
  }
  return response(200,{...state});
}});
(async()=>{
 await client.login();await client.refresh();assert.equal(calls.filter(c=>c.options.method==='POST').length,0);
 await client.save(true,1200);assert.equal(client.snapshot().status.mss,1200);
 const call=calls.find(c=>c.options.method==='POST');assert.equal(call.options.headers['X-MskDSP-Action'],'network');
 assert.equal(call.options.credentials,'same-origin');assert.equal(call.options.cache,'no-store');
 mode='conflict';await client.save(false,1240);assert.match(client.snapshot().message,/其他管理员修改/);assert.equal(client.snapshot().status.revision,state.revision);
 mode='abort';await client.save(true,1240);assert.equal(client.snapshot().status,null);assert.match(client.snapshot().message,/可能已经生效/);assert.match(client.snapshot().message,/刷新状态核对/);
 const before=calls.length;await client.save(true,1240);assert.equal(calls.length,before);
 assert.ok(timeouts.length>0);assert.ok(timeouts.every(value=>value===45000));
 mode='absent';await client.refresh();assert.equal(client.snapshot().status,null);assert.match(client.snapshot().message,/未安装/);assert.equal(client.snapshot().error,true);
 mode='unauth';await client.refresh();assert.equal(redirect,1);assert.equal(client.snapshot().stopped,true);
})();
'''
        result = subprocess.run([node], input="const LIBRARY=" + json.dumps(library) + ";\n" + scenario,
                                text=True, capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
