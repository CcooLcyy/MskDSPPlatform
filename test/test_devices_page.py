"""设备管理页面脚本实际语法检查。"""
from pathlib import Path
import re
import shutil
import subprocess
import unittest
ROOT = Path(__file__).resolve().parents[1]
class DevicePageTests(unittest.TestCase):
    # 使用 Node 解析实际页面脚本，拒绝交付语法错误。
    def test_device_page_script_syntax(self):
        page = (ROOT / 'web/devices.html').read_text(encoding='utf-8')
        script = re.search(r'<script type="module">([\s\S]*?)</script>',page).group(1)
        node = shutil.which('node')
        self.assertIsNotNone(node)
        result = subprocess.run([node,'--check','--input-type=module'],input=script,capture_output=True,text=True,encoding='utf-8',timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
