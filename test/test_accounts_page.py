"""运行账号页面中的实际客户端脚本，验证永久删除交互。"""
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest


PAGE = Path(__file__).resolve().parents[1] / "web" / "accounts.html"


class AccountsPageTests(unittest.TestCase):
    def run_scenario(self, scenario):
        page = PAGE.read_text(encoding="utf-8")
        script = re.search(r'<script type="module">([\s\S]*?)</script>', page).group(1)
        client = script[:script.index("// 页面渲染")]
        setup = r"""
const assert = require('node:assert/strict');
const record = {id:'old-id',username:'field',display_name:'现场账号',role:'operator',enabled:true,revision:7};
let records = [{...record}], requests = [], confirmations = [], confirmResult = true, deleteStatus = 204, releaseDelete;
let pendingDelete = false, refreshFails = false;
const response = (status, data) => ({status,ok:status >= 200 && status < 300,json:async()=>data});
const client = createAccountClient({
  confirmAction: message => { confirmations.push(message); return confirmResult; },
  fetchImpl: async(path, options) => {
    requests.push({path,...options});
    if (path === '/api/v1/auth/me') return response(200, {username:'root',role:'admin'});
    if (options.method === 'DELETE') {
      if (pendingDelete) await new Promise(resolve => releaseDelete = resolve);
      if (deleteStatus === 204) records = [];
      return response(deleteStatus, {error:'测试删除失败'});
    }
    if (path === '/api/v1/accounts') {
      if (refreshFails) throw new Error('测试断网');
      return response(200, records.map(item=>({...item})));
    }
    return response(200, {...record});
  }
});
const writes = () => requests.filter(item=>item.method==='DELETE');
(async()=>{
  await client.refresh(); await client.select(record.id); requests = [];
"""
        source = client + "\n" + setup + scenario + "\n})().catch(error=>{console.error(error);process.exitCode=1;});"
        result = subprocess.run([shutil.which("node") or "node"], input=source, capture_output=True,
                                text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_delete_cancel_dirty_and_password_protection(self):
        self.run_scenario(r"""
confirmResult = false; await client.deleteAccount();
assert.equal(writes().length, 0); assert.equal(client.snapshot().selected.id, record.id);
assert.match(confirmations[0], /现场账号.*field/); assert.match(confirmations[0], /不可恢复/);
confirmResult = true; client.edit({display_name:'草稿'}); await client.deleteAccount();
assert.equal(writes().length, 0); assert.equal(client.snapshot().draft.display_name, '草稿');
await client.discard(); client.editPassword({password:'new-password',confirm:'new-password'});
await client.deleteAccount(); assert.equal(writes().length, 0);
assert.equal(client.snapshot().passwordDraft.password, 'new-password');
""")

    def test_delete_success_refreshes_and_busy_blocks_duplicate_or_navigation(self):
        self.run_scenario(r"""
pendingDelete = true; const work = client.deleteAccount();
assert.equal(client.snapshot().busy, true); await client.deleteAccount(); client.newAccount();
await client.select('other'); await client.save();
assert.equal(writes().length, 1); assert.equal(client.snapshot().selected.id, record.id);
assert.equal(writes()[0].headers['X-MskDSP-Action'], 'account');
assert.deepEqual(JSON.parse(writes()[0].body), {revision:7});
releaseDelete(); await work;
assert.equal(client.snapshot().selected, null); assert.equal(client.snapshot().draft, null);
assert.equal(client.snapshot().accounts.length, 0); assert.equal(client.snapshot().dirty, false);
assert.deepEqual(client.snapshot().passwordDraft, {password:'',confirm:''});
assert.match(client.snapshot().notice, /已永久删除/);
assert.equal(requests.at(-1).path, '/api/v1/accounts');
""")

    def test_delete_failure_or_conflict_preserves_selection(self):
        for status in [409, 500]:
            with self.subTest(status=status):
                self.run_scenario("deleteStatus=" + str(status) + r"""; await client.deleteAccount();
assert.equal(client.snapshot().selected.id, record.id);
assert.equal(client.snapshot().draft.revision, 7); assert.equal(client.snapshot().accounts.length, 1);
assert.equal(client.snapshot().busy, false); assert.ok(client.snapshot().error);
assert.equal(client.snapshot().conflict, deleteStatus === 409);
""")

    def test_refresh_failure_after_delete_retains_confirmed_result(self):
        self.run_scenario(r"""
refreshFails = true; await client.deleteAccount();
assert.equal(client.snapshot().selected, null); assert.equal(client.snapshot().accounts.length, 0);
assert.match(client.snapshot().error, /已删除.*列表刷新失败/);
""")

    def test_rendered_delete_button_is_wired_and_tracks_dirty_state(self):
        page = PAGE.read_text(encoding="utf-8")
        script = re.search(r'<script type="module">([\s\S]*?)</script>', page).group(1)
        harness = r"""
const assert = require('node:assert/strict'), vm = require('node:vm');
const elements = new Map();
function element(id='') {
  return {id,value:id==='enabled-filter'?'all':'',hidden:false,disabled:false,listeners:{},children:[],
    classList:{toggle(){}},setAttribute(){},focus(){},append(...items){this.children.push(...items);},
    replaceChildren(...items){this.children=items;},addEventListener(event,handler){this.listeners[event]=handler;}};
}
for (const match of PAGE.matchAll(/id="([^"]+)"/g)) elements.set(match[1],element(match[1]));
const record={id:'old-id',username:'field',display_name:'现场账号',role:'operator',enabled:true,revision:7};
let records=[record], writes=0, confirmResult=false;
const context=vm.createContext({TextEncoder,Date,console,
  document:{getElementById:id=>elements.get(id),createElement:()=>element()},
  window:{confirm:()=>confirmResult,addEventListener(){},location:{replace(){}}},
  fetch:async(path,options)=>{
    let data=path.includes('/auth/')?{username:'root',role:'admin'}:path==='/api/v1/accounts'?records:{...record};
    if(options.method==='DELETE'){writes++;records=[];return {status:204,ok:true};}
    return {status:200,ok:true,json:async()=>data};
  }
});
vm.runInContext(SOURCE+'\nthis.api=client;',context);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  await flush();
  const remove=elements.get('delete-account'); assert.equal(remove.hidden,true);
  await context.api.select(record.id); assert.equal(remove.hidden,false); assert.equal(remove.disabled,false);
  elements.get('display-name').value='未保存'; elements.get('display-name').listeners.input();
  assert.equal(remove.disabled,true); confirmResult=true; await context.api.discard(); assert.equal(remove.disabled,false); confirmResult=false;
  await remove.listeners.click(); assert.equal(writes,0); assert.equal(remove.hidden,false);
  confirmResult=true; await remove.listeners.click(); assert.equal(writes,1);
  assert.equal(remove.hidden,true); assert.equal(elements.get('account-form').hidden,true);
  assert.match(elements.get('message').textContent,/已永久删除/);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        source = "const PAGE=" + json.dumps(page) + ";const SOURCE=" + json.dumps(script) + ";\n" + harness
        result = subprocess.run([shutil.which("node") or "node"], input=source, capture_output=True,
                                text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
