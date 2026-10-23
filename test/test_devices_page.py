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
let fallbackWorks = true, fallbackCalls = 0, failReload = false, failCreate = false;
let holdCreate = false, finishCreate = null;
let failDeviceUpdate = false, holdDeviceUpdate = false, finishDeviceUpdate = null;
let failGrants = false;
let active = null, chosen = null;
class Element {
  constructor(id = '', tagName = '') {
    this.id = id; this.tagName = tagName; this.value = ''; this.type = '';
    this.textContent = ''; this.hidden = true; this.disabled = false;
    this.className = ''; this.open = false; this.scrolls = [];
    this.children = []; this.listeners = {}; this.style = {}; this.dataset = {};
  }
  set innerHTML(value) { throw Error('禁止将动态字段写入 HTML'); }
  addEventListener(type, handler) { this.listeners[type] = handler; }
  append(...nodes) { for (const node of nodes) node.parentNode = this; this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; this.textContent = ''; }
  reset() { this.fields = {}; if (this.id === 'create-form') element('create-name').value = ''; }
  showModal() { this.open = true; }
  close() { const wasOpen = this.open; this.open = false; if (wasOpen) this.listeners.close?.(); }
  scrollIntoView(options) { this.scrolls.push(options); }
  focus() { active = this; }
  select() { chosen = this; }
  setSelectionRange() { chosen = this; }
  setAttribute() {}
  querySelector(selector) {
    const name = /^\[name=([^\]]+)\]$/.exec(selector)?.[1];
    return descendants(this).find(child => child.name === name) || null;
  }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(child => child !== this); this.parentNode = null; }
}
const element = id => {
  if (!elements.has(id)) elements.set(id, new Element(id));
  return elements.get(id);
};
for (const match of PAGE.matchAll(/<([a-z][a-z0-9]*)\b[^>]*\bid="([^"]+)"/g)) element(match[2]).tagName = match[1];
element('device-status').value = 'all';
const descendants = root => (root.children || []).flatMap(child => [child, ...descendants(child)]);
const rowButton = (index, name) => descendants(element('devices').children[index]).find(child => child.className?.split(' ').includes(name));
const document = {
  getElementById: id => { assert.ok(elements.has(id), `页面缺少 ${id}`); return element(id); },
  createElement: tag => new Element('', tag),
  createTextNode: text => ({textContent: text}),
  querySelectorAll: selector => [...new Set([...elements.values(), ...[...elements.values()].flatMap(descendants)])].filter(node => selector === 'button' && node.tagName === 'button'),
  body: new Element('body'),
  get activeElement() { return active; },
  execCommand: command => {
    assert.equal(command, 'copy'); fallbackCalls++;
    if (element('editor').open) assert.equal(chosen.parentNode, element('editor'), '兼容复制节点必须位于活动模态窗口内');
    if (fallbackWorks) copied.push(chosen.value);
    return fallbackWorks;
  },
};
const navigator = {clipboard: {writeText: async value => {copied.push(value);}}};
let records = [], accountRecords = [], grantRecords = [];
const created = {device_id: 'device-1234567890abcdef1234567890abcdef', display_name: '一号网关<img onerror=alert(1)>', token: 'first-token', enabled: true};
const response = (body, status = 200) => ({ok: status < 400, status, json: async () => body});
const fetch = async (url, options) => {
  calls.push({url, options});
  if (url === '/api/v1/auth/me') return response({username: 'admin', role: 'admin'});
  if (url === '/api/v1/accounts') return response(accountRecords.map(account => ({...account})));
  if (url === '/api/v1/devices' && options.method === 'POST') {
    if (holdCreate) await new Promise(resolve => {finishCreate = resolve;});
    if (failCreate) return response({error: '设备名称已存在'}, 409);
    records = [{...created}]; delete records[0].token;
    return response(created, 201);
  }
  if (url === '/api/v1/devices') return failReload ? response({error: '刷新失败'}, 503) : response(records);
  if (url.endsWith('/grants')) {
    if (options.method === 'PUT') { grantRecords = JSON.parse(options.body); return response(null, 204); }
    if (failGrants) return response({error: '读取设备权限失败'}, 503);
    return response(grantRecords);
  }
  if (url.endsWith('/token')) return response({...created, token: 'rotated-token'});
  if (url === `/api/v1/devices/${created.device_id}` && options.method === 'PUT') {
    if (holdDeviceUpdate) await new Promise(resolve => {finishDeviceUpdate = resolve;});
    if (failDeviceUpdate) return response({error: '设备保存失败'}, 503);
    const updated = {...records[0], ...JSON.parse(options.body)};
    records = [updated]; return response(updated);
  }
  if (url === `/api/v1/devices/${created.device_id}` && options.method === 'DELETE') {
    records = []; return response(null, 204);
  }
  throw Error(`未预期请求 ${url}`);
};
const context = vm.createContext({document, navigator, fetch, confirm: () => true,
  location: {assign() {}}, FormData: class {constructor(form) {this.form = form;} get(key) {return this.form.fields?.[key] ?? (key === 'display_name' ? element('create-name').value : null);}},
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
async function clickRow(index, name) {const button = rowButton(index, name); assert.ok(button); button.listeners.click({target: button}); await settle();}
function escapeDialog(id = 'create-dialog') {
  const event = {defaultPrevented: false, preventDefault() {this.defaultPrevented = true;}};
  element(id).listeners.cancel(event);
  if (!event.defaultPrevented) element(id).close();
  return event;
}
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
        result = subprocess.run([node], input=source, capture_output=True, text=True, encoding="utf-8", timeout=15)
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
assert.equal(descendants(element('editor')).filter(child => child.tagName === 'textarea').length, 0);
fallbackWorks = false; await click('copy-token');
assert.match(element('message').textContent, /复制失败/);
assert.equal(document.body.children.length, 0);
assert.equal(element('credential').value, created.token);
assert.equal(element('editor').open, true);
assert.match(element('editor-message').textContent, /复制失败/);
assert.equal(descendants(element('editor')).filter(child => child.tagName === 'textarea').length, 0);
''')

    # 轮换显示新凭据，切换设备和离开页面都会清理 ID、名称、令牌及明文状态。
    def test_rotate_and_clear_lifecycle(self):
        self.run_scenario(r'''
await create(); await click('rotate');
assert.equal(element('credential-name').value, created.display_name);
assert.equal(element('credential-id').value, created.device_id);
assert.equal(element('credential').value, 'rotated-token');
await clickRow(0, 'manage-device'); await assertCleared();
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
assert.equal(element('editor').open, true);
assert.equal(element('settings-fields').hidden, true);
assert.match(element('editor-message').textContent, /刷新失败/);
''')

    # 搜索和状态筛选仅处理已有列表，已禁用设备不能误计为在线或离线。
    def test_search_status_filter_and_empty_states(self):
        self.run_scenario(r'''
assert.equal(element('device-empty').hidden, false);
assert.equal(element('empty-title').textContent, '暂无现场设备');
records = [
  {device_id: 'device-alpha', display_name: '研发网关', enabled: true, online: true},
  {device_id: 'device-beta', display_name: '现场网关', enabled: true, online: false},
  {device_id: 'device-gamma', display_name: '禁用网关', enabled: false, online: true},
];
await click('refresh'); const requestCount = calls.length;
assert.equal(element('devices').children.length, 3);
assert.equal(element('device-total').textContent, '3 台 · 1 台在线');
element('device-search').value = '研发'; element('device-search').listeners.input();
assert.equal(element('devices').children[0].children[0].textContent, '研发网关');
assert.equal(element('device-count').textContent, '显示 1 / 3 台设备');
element('device-search').value = ' DEVICE-BETA '; element('device-search').listeners.input();
assert.equal(element('devices').children[0].children[0].textContent, '现场网关');
element('device-search').value = '';
for (const [state, name] of [['online', '研发网关'], ['offline', '现场网关'], ['disabled', '禁用网关']]) {
  element('device-status').value = state; element('device-status').listeners.change();
  assert.equal(element('devices').children.length, 1);
  assert.equal(element('devices').children[0].children[0].textContent, name);
}
element('device-search').value = '没有这个设备'; element('device-search').listeners.input();
assert.equal(element('devices').children.length, 0);
assert.equal(element('device-empty').hidden, false);
assert.equal(element('empty-title').textContent, '没有匹配的设备');
assert.equal(element('device-count').textContent, '显示 0 / 3 台设备');
assert.equal(calls.length, requestCount);
''')

    # 表格 ID 可缩略，但复制必须使用完整值且保留当前一次性凭据。
    def test_table_id_copy_does_not_select_or_clear_token(self):
        self.run_scenario(r'''
await create();
const row = element('devices').children[0];
assert.equal(row.children[0].textContent, created.display_name, '表格设备名称');
const code = descendants(row).find(child => child.tagName === 'code');
assert.notEqual(code.textContent, created.device_id);
assert.equal(code.title, created.device_id);
const requestCount = calls.length, selectedBefore = vm.runInContext('selected', context);
await clickRow(0, 'copy-device-id');
assert.equal(copied.at(-1), created.device_id);
assert.equal(calls.length, requestCount);
assert.equal(vm.runInContext('selected', context), selectedBefore);
assert.equal(element('credential').value, created.token);
assert.equal(element('credential-panel').hidden, false);
await clickRow(0, 'manage-device');
assert.equal(calls.at(-1).url, `/api/v1/devices/${created.device_id}/grants`);
assert.equal(element('selected-title').textContent, created.display_name, '管理中的设备名称');
assert.equal(element('selected-id').textContent, created.device_id);
assert.equal(element('editor').open, true);
await assertCleared();
''')

    # 取消不创建，服务端失败保留弹窗草稿；成功后关闭弹窗并显示凭据。
    def test_create_dialog_cancel_error_and_retry(self):
        self.run_scenario(r'''
const initialCalls = calls.length;
await click('open-create'); assert.equal(element('create-dialog').open, true);
element('create-name').value = '未提交草稿';
await click('cancel-create'); assert.equal(element('create-dialog').open, false);
assert.equal(calls.length, initialCalls);
await click('open-create'); assert.equal(element('create-name').value, '');
element('create-name').value = '  一号网关  '; failCreate = true;
const form = element('create-form');
form.listeners.submit({target: form, preventDefault() {}}); await settle();
assert.equal(element('create-dialog').open, true);
assert.equal(element('create-name').value, '  一号网关  ');
assert.equal(element('create-error').textContent, '设备名称已存在');
assert.equal(element('message').className, 'status-banner error');
assert.deepEqual(JSON.parse(calls.at(-1).options.body), {display_name: '一号网关'});
assert.equal(calls.at(-1).options.headers['X-MskDSP-Action'], 'device');
assert.equal(calls.at(-1).options.credentials, 'same-origin');
assert.equal(calls.at(-1).options.cache, 'no-store');
failCreate = false;
form.listeners.submit({target: form, preventDefault() {}}); await settle();
assert.equal(element('create-dialog').open, false);
assert.equal(element('create-name').value, '');
assert.equal(element('create-error').textContent, '');
assert.equal(element('credential').value, created.token);
await click('open-create'); await click('close-create');
assert.equal(element('create-dialog').open, false);
await click('open-create');
assert.equal(escapeDialog().defaultPrevented, false);
assert.equal(element('create-dialog').open, false);
''')

    # 等待创建时禁止重复请求与关闭弹窗，避免误以为取消了已经发出的操作。
    def test_create_busy_blocks_duplicates_and_escape(self):
        self.run_scenario(r'''
await click('open-create'); holdCreate = true;
const form = element('create-form'); element('create-name').value = created.display_name;
form.listeners.submit({target: form, preventDefault() {}});
assert.equal(vm.runInContext('busy', context), true);
assert.equal(element('cancel-create').disabled, true);
form.listeners.submit({target: form, preventDefault() {}});
element('cancel-create').listeners.click(); element('close-create').listeners.click();
assert.equal(escapeDialog().defaultPrevented, true);
assert.equal(element('create-dialog').open, true);
assert.equal(calls.filter(call => call.options.method === 'POST').length, 1);
finishCreate(); await settle();
assert.equal(element('create-dialog').open, false);
assert.equal(vm.runInContext('busy', context), false);
assert.equal(element('cancel-create').disabled, false);
for (const button of document.querySelectorAll('button')) assert.equal(button.disabled, false);
assert.equal(element('credential').value, created.token);
''')

    # 管理在模态窗口中打开，关闭和 Escape 都清除选择、权限以及一次性凭据。
    def test_manage_dialog_open_close_and_escape_cleanup(self):
        self.run_scenario(r'''
assert.equal(element('editor').tagName, 'dialog');
assert.equal(element('editor').open, false);
await create();
element('grants').append(new Element('', 'div'));
assert.equal(element('editor').open, true);
assert.equal(element('settings-fields').hidden, false);
assert.equal(element('grants').children.length, 1);
assert.equal(vm.runInContext('selected.device_id', context), created.device_id);
await click('reveal');
await click('close-editor');
assert.equal(element('editor').open, false);
assert.equal(vm.runInContext('selected', context), null);
assert.equal(element('grants').children.length, 0);
assert.equal(element('settings-fields').hidden, true);
await assertCleared();
assert.equal(element('devices').children[0].className, '');
await clickRow(0, 'manage-device');
assert.equal(element('editor').open, true);
assert.equal(element('selected-title').textContent, created.display_name);
await click('rotate'); await click('reveal');
assert.equal(escapeDialog('editor').defaultPrevented, false);
assert.equal(element('editor').open, false);
assert.equal(vm.runInContext('selected', context), null);
await assertCleared();
''')

    # 保存失败在弹窗内反馈并保留草稿，保存成功仍在当前弹窗继续管理。
    def test_manage_save_errors_and_success_keep_dialog_open(self):
        self.run_scenario(r'''
await create(); failDeviceUpdate = true;
element('display-name').value = '修改后的名称';
const form = element('edit-form');
form.listeners.submit({target: form, preventDefault() {}}); await settle();
assert.equal(element('editor').open, true);
assert.equal(element('display-name').value, '修改后的名称');
assert.equal(element('editor-message').textContent, '设备保存失败');
assert.equal(element('editor-message').className, 'status-banner error');
assert.equal(element('credential').value, created.token);
failDeviceUpdate = false;
form.listeners.submit({target: form, preventDefault() {}}); await settle();
assert.equal(element('editor').open, true);
assert.equal(element('selected-title').textContent, '修改后的名称');
assert.equal(element('editor-message').textContent, '设备设置已保存。');
await click('save-grants');
assert.equal(element('editor').open, true);
assert.match(element('editor-message').textContent, /设备权限已保存/);
await click('delete');
assert.equal(element('editor').open, false);
assert.equal(element('devices').children.length, 0);
assert.equal(vm.runInContext('selected', context), null);
await assertCleared();
''')

    # 保存请求尚未完成时阻止关闭和 Escape，结束后恢复关闭并清理凭据。
    def test_manage_busy_blocks_close_and_escape(self):
        self.run_scenario(r'''
await create(); holdDeviceUpdate = true;
const form = element('edit-form');
form.listeners.submit({target: form, preventDefault() {}});
assert.equal(vm.runInContext('busy', context), true);
assert.equal(element('close-editor').disabled, true);
element('close-editor').listeners.click();
assert.equal(escapeDialog('editor').defaultPrevented, true);
assert.equal(element('editor').open, true);
assert.equal(vm.runInContext('selected.device_id', context), created.device_id);
finishDeviceUpdate(); await settle();
assert.equal(element('editor').open, true);
assert.equal(element('close-editor').disabled, false);
await click('close-editor');
assert.equal(element('editor').open, false);
await assertCleared();
''')

    # 首次读取权限失败时不能打开一个可保存旧权限的空管理窗口。
    def test_manage_load_failure_does_not_open_dialog(self):
        self.run_scenario(r'''
await create(); await click('close-editor'); failGrants = true;
await clickRow(0, 'manage-device');
assert.equal(element('editor').open, false);
assert.equal(vm.runInContext('selected', context), null);
assert.equal(element('message').textContent, '读取设备权限失败');
assert.equal(element('grants').children.length, 0);
await assertCleared();
''')
