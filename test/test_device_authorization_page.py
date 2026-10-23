"""按账号显式授权的页面实际 JavaScript 行为测试。"""
from pathlib import Path
import unittest

import test_devices_page


class DeviceAuthorizationPageTests(unittest.TestCase):
    run_scenario = test_devices_page.DevicePageTests.run_scenario

    def test_explicit_authorization_hints(self):
        root = Path(__file__).resolve().parents[1]
        devices = (root / "web/devices.html").read_text(encoding="utf-8")
        accounts = (root / "web/accounts.html").read_text(encoding="utf-8")
        self.assertNotIn("为现有账号默认开启", devices)
        self.assertIn("运维人员默认不可见", devices)
        self.assertIn("历史授权需重新保存确认", devices)
        self.assertIn("管理员自动拥有全部设备", accounts)
        self.assertIn("运维人员仅可访问明确授权的设备", accounts)
        self.assertNotIn("具体功能权限后续配置", accounts)

    def test_admin_has_text_permissions_and_save_only_includes_operators(self):
        self.run_scenario(r'''
accountRecords = [
  {id: 'administrator', username: 'alice', display_name: '管理员', role: 'admin', enabled: true},
  {id: 'operator', username: 'admin', display_name: '运维人员', role: 'operator', enabled: true},
];
grantRecords = [{account_id: 'administrator', allow_grpc: false, allow_ssh: false}];
await create();
const [adminRow, operatorRow] = element('grants').children;
assert.match(descendants(adminRow).map(node => node.textContent).join(''), /管理员.*全部权限/);
assert.equal(descendants(adminRow).filter(node => node.tagName === 'input').length, 0);
assert.equal(operatorRow.querySelector('[name=allow_grpc]').checked, false);
assert.equal(operatorRow.querySelector('[name=allow_ssh]').checked, false);
operatorRow.querySelector('[name=allow_grpc]').checked = true;
operatorRow.querySelector('[name=allow_ssh]').checked = true;
await click('save-grants');
const saved = calls.findLast(call => call.url.endsWith('/grants') && call.options.method === 'PUT');
assert.deepEqual(JSON.parse(saved.options.body), [{account_id: 'operator', allow_grpc: true, allow_ssh: true}]);
assert.equal(descendants(adminRow).filter(node => node.tagName === 'input').length, 0);
operatorRow.querySelector('[name=allow_grpc]').checked = false;
operatorRow.querySelector('[name=allow_ssh]').checked = false;
await click('save-grants');
assert.deepEqual(JSON.parse(calls.findLast(call => call.options.method === 'PUT').options.body), []);
''')

    def test_refresh_reloads_current_roles_for_selected_device(self):
        self.run_scenario(r'''
accountRecords = [{id: 'account', username: 'worker', display_name: '人员', role: 'operator', enabled: true}];
await create();
assert.ok(element('grants').children[0].querySelector('[name=allow_grpc]'));
accountRecords[0].role = 'admin';
await click('refresh');
assert.equal(element('grants').children[0].querySelector('[name=allow_grpc]'), null);
assert.match(descendants(element('grants').children[0]).map(node => node.textContent).join(''), /全部权限/);
accountRecords[0].role = 'operator';
await click('refresh');
assert.equal(element('grants').children[0].querySelector('[name=allow_grpc]').checked, false);
assert.equal(element('credential').value, created.token, '刷新权限不清除尚未保存的一次性令牌');
''')

    def test_changed_role_before_save_refreshes_without_writing_stale_grants(self):
        self.run_scenario(r'''
accountRecords = [{id: 'account', username: 'worker', display_name: '人员', role: 'operator', enabled: true}];
await create();
element('grants').children[0].querySelector('[name=allow_grpc]').checked = true;
accountRecords[0].role = 'admin';
await click('save-grants');
assert.equal(calls.filter(call => call.options.method === 'PUT').length, 0);
assert.match(element('message').textContent, /账号角色已变化/);
assert.equal(element('grants').children[0].querySelector('[name=allow_grpc]'), null);
await click('save-grants');
assert.deepEqual(JSON.parse(calls.findLast(call => call.options.method === 'PUT').options.body), []);
accountRecords[0].role = 'operator';
await click('save-grants');
assert.equal(calls.filter(call => call.options.method === 'PUT').length, 1);
assert.equal(element('grants').children[0].querySelector('[name=allow_grpc]').checked, false);
''')


if __name__ == "__main__":
    unittest.main()
