"""设备管理页面的实际脚本语法与创建、复制、凭据清理行为测试。"""
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

PAGE = Path(__file__).resolve().parents[1] / "web" / "devices.html"


class DevicePageTests(unittest.TestCase):
    def run_scenario(self, scenario):
        page = PAGE.read_text(encoding="utf-8")
        script = re.search(r'<script type="module">([\s\S]*?)</script>', page).group(1)
        node = shutil.which("node")
        self.assertIsNotNone(node)
        harness = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const elements = new Map(), windowEvents = {}, calls = [], copied = [];
let fallbackWorks = true, fallbackCalls = 0, failReload = false;
let active = null, chosen = null;
class Element {
  constructor(id = '', tagName = '') {
    this.id = id; this.tagName = tagName; this.value = ''; this.type = '';
    this.textContent = ''; this.hidden = true; this.disabled = false;
    this.children = []; this.listeners = {}; this.style = {}; this.dataset = {};
  }
  set innerHTML(value) { throw Error('禁止将动态字段写入 HTML'); }
  addEventListener(type, handler) { this.listeners[type] = handler; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; this.textContent = ''; }
  reset() { this.fields = {}; }
  focus() { active = this; }
  select() { chosen = this; }
  setSelectionRange() { chosen = this; }
  setAttribute() {}
  remove() { document.body.children = document.body.children.filter(child => child !== this); }
}
const element = id => {
  if (!elements.has(id)) elements.set(id, new Element(id));
  return elements.get(id);
};
for (const match of PAGE.matchAll(/\bid="([^"]+)"/g)) element(match[1]);
const document = {
  getElementById: id => { assert.ok(elements.has(id), `页面缺少 ${id}`); return element(id); },
  createElement: tag => new Element('', tag),
  createTextNode: text => ({textContent: text}),
  querySelectorAll: () => [...elements.values(), ...element('devices').children],
  body: new Element('body'),
  get activeElement() { return active; },
  execCommand: command => {
    assert.equal(command, 'copy'); fallbackCalls++;
    if (fallbackWorks) copied.push(chosen.value);
    return fallbackWorks;
  },
};
const navigator = {clipboard: {writeText: async value => {copied.push(value);}}};
let records = [];
const created = {device_id: 'device-1234567890abcdef', display_name: '一号网关<img onerror=alert(1)>', token: 'first-token', enabled: true};
const response = (body, status = 200) => ({ok: status < 400, status, json: async () => body});
const fetch = async (url, options) => {
  calls.push({url, options});
  if (url === '/api/v1/auth/me') return response({username: 'admin', role: 'admin'});
  if (url === '/api/v1/accounts') return response([]);
  if (url === '/api/v1/devices' && options.method === 'POST') {
    records = [{...created}]; delete records[0].token;
    return response(created, 201);
  }
  if (url === '/api/v1/devices') return failReload ? response({error: '刷新失败'}, 503) : response(records);
  if (url.endsWith('/grants')) return response([]);
  if (url.endsWith('/token')) return response({...created, token: 'rotated-token'});
  throw Error(`未预期请求 ${url}`);
};
const context = vm.createContext({document, navigator, fetch, confirm: () => true,
  location: {assign() {}}, FormData: class {constructor(form) {this.form = form;} get(key) {return this.form.fields[key] ?? null;}},
  window: {addEventListener: (type, handler) => {windowEvents[type] = handler;}},
});
vm.runInContext(SOURCE, context);
async function settle() {
  for (let i = 0; i < 30; i++) {
    await new Promise(resolve => setImmediate(resolve));
    if (!vm.runInContext('busy', context)) return;
  }
  throw Error('页面操作未结束');
}
async function click(id) {element(id).listeners.click({target: element(id)}); await settle();}
async function create() {
  const form = element('create-form'); form.fields = {display_name: created.display_name};
  form.listeners.submit({target: form, preventDefault() {}}); await settle();
}
async function assertCleared() {
  assert.equal(element('credential-panel').hidden, true);
  for (const id of ['credential-name', 'credential-id', 'credential']) assert.equal(element(id).value, '');
  assert.equal(element('credential').type, 'password');
}
'''
        source = "const PAGE=" + json.dumps(page) + ";const SOURCE=" + json.dumps(script) + ";\n"
        source += harness + "\n(async () => {await settle();\n" + scenario
        source += "\n})().catch(error => {console.error(error); process.exitCode = 1;});"
        result = subprocess.run([node, "-e", source], capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    # 使用 Node 解析实际页面脚本，拒绝交付语法错误。
    def test_device_page_script_syntax(self):
        page = PAGE.read_text(encoding="utf-8")
        script = re.search(r'<script type="module">([\s\S]*?)</script>', page).group(1)
        node = shutil.which("node")
        self.assertIsNotNone(node)
        result = subprocess.run([node, "--check", "--input-type=module"], input=script,
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    # 创建时仅输入中文名称，读取服务器生成的 ID 与令牌，动态名称不能执行 HTML。
    def test_create_with_name_and_copy_credentials(self):
        page = PAGE.read_text(encoding="utf-8")
        form = re.search(r'<form id="create-form"[\s\S]*?</form>', page).group()
        self.assertNotIn('name="device_id"', form)
        self.assertIn('name="display_name"', form)
        self.run_scenario(r'''
await create();
const request = calls.find(call => call.url === '/api/v1/devices' && call.options.method === 'POST');
assert.deepEqual(JSON.parse(request.options.body), {display_name: created.display_name});
assert.equal(element('credential-panel').hidden, false);
assert.equal(element('credential-name').value, created.display_name);
assert.equal(element('credential-id').value, created.device_id);
assert.equal(element('credential').value, created.token);
assert.equal(element('credential').type, 'password');
assert.equal(vm.runInContext('selected.token', context), undefined);
await click('copy-name'); await click('copy-id'); await click('copy-token'); await click('copy-config');
assert.deepEqual(copied.slice(0, 3), [created.display_name, created.device_id, created.token]);
assert.equal(copied[3], `设备名称：${created.display_name}\n设备 ID：${created.device_id}\n设备令牌：${created.token}`);
assert.equal(element('credential').type, 'password');
await click('reveal'); assert.equal(element('credential').type, 'text');
await click('dismiss'); await assertCleared();
''')

    # HTTP 环境无 Clipboard API 或授权失败时仍可复制；后备失败必须提示且清理临时节点。
    def test_copy_http_fallback_and_error(self):
        self.run_scenario(r'''
await create(); navigator.clipboard = undefined;
await click('copy-id'); assert.equal(copied.at(-1), created.device_id);
navigator.clipboard = {writeText: async () => {throw Error('禁止访问');}};
await click('copy-token'); assert.equal(copied.at(-1), created.token);
assert.equal(fallbackCalls, 2); assert.equal(document.body.children.length, 0);
fallbackWorks = false; await click('copy-token');
assert.match(element('message').textContent, /复制失败/);
assert.equal(document.body.children.length, 0);
assert.equal(element('credential').value, created.token);
''')

    # 轮换显示新凭据，切换设备和离开页面都会清理 ID、名称、令牌及明文状态。
    def test_rotate_and_clear_lifecycle(self):
        self.run_scenario(r'''
await create(); await click('rotate');
assert.equal(element('credential-name').value, created.display_name);
assert.equal(element('credential-id').value, created.device_id);
assert.equal(element('credential').value, 'rotated-token');
element('devices').children[0].listeners.click(); await settle(); await assertCleared();
await click('rotate'); await click('reveal');
windowEvents.pagehide(); await assertCleared();
''')

    # 设备创建已成功但后续刷新失败时，仍必须保留只返回一次的令牌供配置人员保存。
    def test_created_token_survives_followup_failure(self):
        self.run_scenario(r'''
failReload = true; await create();
assert.match(element('message').textContent, /刷新失败/);
assert.equal(element('credential-panel').hidden, false);
assert.equal(element('credential-id').value, created.device_id);
assert.equal(element('credential').value, created.token);
''')
