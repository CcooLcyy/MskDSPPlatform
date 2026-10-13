"""以真实平台进程验证点表 HTTP 合约、重启持久化及版本冲突。"""
import copy
import http.client
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / ("mskdsp-platform.exe" if os.name == "nt" else "mskdsp-platform")


def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def template():
    return {
        "schema_version": 1, "id": "test-precision", "name": "逆变器点表", "manufacturer": "测试厂家",
        "models": ["测试型号"], "protocol_version": "V3", "address_base": 1, "notes": ["测试备注"], "sources": [],
        "points": [{"id": "active", "tag": "有功功率", "group": "遥测", "unit": "kW", "function_code": 3,
                    "address": 40122, "data_type": "INT32", "reg_count": 2, "scale": "0.000000000000000123",
                    "offset": "-1e-3", "deadband": "0.01", "word_order": "LH", "byte_order": "BA",
                    "bit_index": None, "source_page": 12, "description": "说明 <script>",
                    "default_selected": False, "condition": "按型号选择"}],
    }


@unittest.skipUnless(BINARY.exists(), "请先执行 cargo build --locked")
class TemplateApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.http_port, self.grpc_port = port(), port()
        self.config = self.folder / "config.toml"
        password_hash = subprocess.run([str(BINARY), "--hash-admin-password"], input="test-password-123", text=True,
                                       encoding="utf-8", capture_output=True, check=True).stdout.strip()
        self.config.write_text(f'[server]\nhttp_addr="127.0.0.1:{self.http_port}"\ngrpc_addr="127.0.0.1:{self.grpc_port}"\n'
                               f'[auth]\npassword_hash="{password_hash}"\n', encoding="utf-8")
        self.child = None
        self.cookie = ""
        self.addCleanup(self.stop)
        self.start()

    def start(self):
        env = dict(os.environ, MSKDSP_PLATFORM_CONFIG=str(self.config), MSKDSP_PLATFORM_DATA_DIR=str(self.folder / "data"))
        env.pop("MSKDSP_PLATFORM_HTTP_ADDR", None)
        self.child = subprocess.Popen([str(BINARY)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if self.child.poll() is not None:
                self.fail("平台进程提前退出")
            try:
                if self.request("GET", "/healthz")[0] == 200:
                    status, raw, headers = self.request("POST", "/api/v1/auth/login", {"username": "root", "password": "test-password-123"}, {"X-MskDSP-Action": "auth"})
                    self.assertEqual(status, 200, raw)
                    self.cookie = headers["set-cookie"].split(";", 1)[0]
                    return
            except OSError:
                pass
            time.sleep(0.05)
        self.fail("平台 HTTP 未就绪")

    def stop(self):
        if self.child and self.child.poll() is None:
            self.child.terminate()
            self.child.wait(timeout=5)

    def request(self, method, path, body=None, headers=None):
        marked = {"Content-Type": "application/json", "X-MskDSP-Action": "modbus-template"}
        if self.cookie:
            marked["Cookie"] = self.cookie
        marked.update(headers or {})
        raw = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        connection = http.client.HTTPConnection("127.0.0.1", self.http_port, timeout=5)
        try:
            connection.request(method, path, raw, marked)
            response = connection.getresponse()
            content = response.read()
            return response.status, content, {key.lower(): value for key, value in response.getheaders()}
        finally:
            connection.close()

    # 点表全字段及十进制精度经创建、下载、替换和进程重启保持一致。
    def test_roundtrip_restart_edit_and_revision_conflict(self):
        base = "/api/v1/modbus-templates"
        status, raw, _ = self.request("POST", base, template())
        self.assertEqual(status, 201, raw)
        record = json.loads(raw)
        path = base + "/" + record["id"]
        self.assertEqual(record["template"], template())
        self.assertEqual(json.loads(self.request("GET", path + "/download")[1]), template())
        self.stop()
        self.start()
        self.assertEqual(json.loads(self.request("GET", path)[1]), record)
        listing = json.loads(self.request("GET", base)[1])
        self.assertEqual(listing[0]["point_count"], 1)
        self.assertNotIn("template", listing[0])
        edited = template()
        edited["points"][0]["tag"] = "修改后的点位"
        status, raw, _ = self.request("PUT", path, edited, {"If-Match": str(record["revision"])})
        self.assertEqual(status, 200, raw)
        latest = json.loads(raw)
        self.assertEqual(latest["revision"], record["revision"] + 1)
        self.assertEqual(self.request("PUT", path, template(), {"If-Match": str(record["revision"])})[0], 409)
        self.assertEqual(self.request("DELETE", path, headers={"If-Match": str(record["revision"])})[0], 409)
        self.assertEqual(json.loads(self.request("GET", path)[1])["template"], edited)
        self.assertEqual(self.request("DELETE", path, headers={"If-Match": str(latest["revision"])})[0], 204)
        self.assertEqual(self.request("GET", path)[0], 404)

    # 写操作校验标记、版本、JSON 和上位机不支持的模板，并拒绝超限文件。
    def test_invalid_requests(self):
        base = "/api/v1/modbus-templates"
        for method, path in (("GET", base), ("POST", base), ("PUT", base + "/missing"),
                             ("DELETE", base + "/missing"), ("GET", base + "/missing/download")):
            self.assertEqual(self.request(method, path, template() if method in ("POST", "PUT") else None, {"Cookie": ""})[0], 401)
        self.assertEqual(self.request("POST", base, template(), {"X-MskDSP-Action": ""})[0], 403)
        self.assertEqual(self.request("POST", base, template(), {"Origin": "https://evil.example"})[0], 403)
        for mutate in (lambda x: x.update(schema_version=2), lambda x: x.update(points=[]),
                       lambda x: x["points"][0].update(scale=1.0), lambda x: x["points"][0].update(function_code=2),
                       lambda x: x["points"].append(copy.deepcopy(x["points"][0]))):
            invalid = template()
            mutate(invalid)
            status, raw, _ = self.request("POST", base, invalid)
            self.assertEqual(status, 400, raw)
            self.assertTrue(json.loads(raw)["error"])
        self.assertEqual(self.request("POST", base, {"padding": "x" * (2 * 1024 * 1024)})[0], 413)
        record = json.loads(self.request("POST", base, template())[1])
        self.assertEqual(self.request("PUT", base + "/" + record["id"], template())[0], 428)
        self.assertEqual(self.request("DELETE", base + "/" + record["id"], headers={"If-Match": "abc"})[0], 400)
        self.assertEqual(json.loads(self.request("GET", base)[1])[0]["point_count"], 1)

    # 平台入口和点表页面均可访问，现有程序更新页面继续保留。
    def test_pages_and_upper_builtin_compatibility(self):
        self.assertIn("点表".encode(), self.request("GET", "/modbus-templates")[1])
        self.assertIn(b"/modbus-templates", self.request("GET", "/")[1])
        self.assertEqual(self.request("GET", "/update")[0], 200)
        fixtures = ROOT.parent / "MskDSPUpper" / "src" / "features" / "modbus-templates" / "builtin"
        for file in fixtures.glob("*.json"):
            expected = json.loads(file.read_text(encoding="utf-8-sig"))
            status, raw, _ = self.request("POST", "/api/v1/modbus-templates", expected)
            self.assertEqual(status, 201, file.name + ": " + raw.decode())
            record = json.loads(raw)
            self.assertEqual(record["template"], expected)

    # 真正调用上位机解析器与个人目录保存逻辑，验证平台导出的副本能够导入。
    def test_download_imports_into_upper_personal_catalog(self):
        upper = ROOT.parent / "MskDSPUpper" / "src" / "features" / "modbus-templates"
        node = shutil.which("node")
        if not node or not (upper / "store-core.ts").exists():
            self.skipTest("仅在同时存在上位机源码与 Node 时执行跨项目导入契约")
        downloaded = []
        for index, file in enumerate((upper / "builtin").glob("*.json")):
            document = json.loads(file.read_text(encoding="utf-8-sig"))
            document["id"] = f"platform-personal-copy-{index}"
            status, raw, _ = self.request("POST", "/api/v1/modbus-templates", document)
            self.assertEqual(status, 201, raw)
            record = json.loads(raw)
            downloaded.append(self.request("GET", "/api/v1/modbus-templates/" + record["id"] + "/download")[1].decode())
        self.assertTrue(downloaded, "上位机至少应有一个内置模板夹具")
        script = '''
import assert from 'node:assert/strict';
import fs from 'node:fs';
const base = process.argv[1];
const {parseModbusTemplateJson} = await import(base + '/model.ts');
const {createPersonalTemplateStore} = await import(base + '/store-core.ts');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
let saved = [];
const catalog = createPersonalTemplateStore([], {read:()=>saved, write:async value=>{saved=value;}});
for (const raw of input) {
  const parsed = parseModbusTemplateJson(raw);
  await catalog.save(parsed);
  assert.deepEqual(catalog.list().at(-1), parsed);
}
assert.equal(catalog.list().length, input.length);
'''
        result = subprocess.run([node, "--experimental-strip-types", "--input-type=module", "-e", script, upper.as_uri()],
                                input=json.dumps(downloaded, ensure_ascii=False), text=True, encoding="utf-8",
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
