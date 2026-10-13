"""主题选择、系统跟随与跨页偏好的回归验证。"""
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
PAGES = sorted((ROOT / "web").glob("*.html"))


class ThemePageTests(unittest.TestCase):
    # 每个入口都在首屏绘制前恢复主题，并覆盖系统深色与无脚本场景。
    def test_page_contract(self):
        scripts, palettes = [], []
        for path in PAGES:
            with self.subTest(page=path.name):
                page = path.read_text(encoding="utf-8")
                self.assertIn('content="light dark"', page)
                self.assertIn('for="theme-select"', page)
                self.assertIn('@media (prefers-color-scheme: dark)', page)
                self.assertIn(':root:not([data-theme="light"])', page)
                self.assertIn(':root[data-theme="dark"]', page)
                self.assertLess(page.index('id="theme-script"'), page.index("<body>"))
                scripts.append(re.search(r'<script id="theme-script">([\s\S]*?)</script>', page).group(1))
                styles = re.search(r'<style>([\s\S]*?)</style>', page).group(1)
                palettes.append(styles.split('    * {')[0])
                # 业务样式不能残留浅色背景，主题变量之外只保留主按钮固定色。
                body_styles = styles.split('    * {')[1]
                self.assertNotIn('background: white', body_styles)
                self.assertEqual(set(re.findall(r'#[0-9a-fA-F]{3,8}\b', body_styles)), {'#2563eb'})
        self.assertTrue(all(script == scripts[0] for script in scripts))
        self.assertTrue(all(palette == palettes[0] for palette in palettes))

    # 模拟系统变化、手动优先、刷新恢复、其他标签页和存储被禁用的情况。
    def test_theme_behavior(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "主题行为测试需要 Node")
        page = PAGES[0].read_text(encoding="utf-8")
        script = re.search(r'<script id="theme-script">([\s\S]*?)</script>', page).group(1)
        scenario = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
function open({saved=null, dark=false, blocked=false}={}) {
  const events={}, mediaEvents={}, documentEvents={}, selectEvents={};
  const select={value:'system',addEventListener:(name,fn)=>selectEvents[name]=fn};
  let mounted=false;
  const document={documentElement:{dataset:{}},getElementById:()=>mounted?select:null,
    addEventListener:(name,fn)=>documentEvents[name]=fn};
  const media={matches:dark,addEventListener:(name,fn)=>mediaEvents[name]=fn};
  const storage={getItem:()=>{if(blocked)throw Error('禁止存储');return saved;},
    setItem:(key,value)=>{if(blocked)throw Error('禁止存储');saved=value;}};
  const window={matchMedia:()=>media,localStorage:storage,addEventListener:(name,fn)=>events[name]=fn};
  vm.runInNewContext(SCRIPT,{window,document});
  return {theme:()=>document.documentElement.dataset.theme,saved:()=>saved,select,
    mount:()=>{mounted=true;documentEvents.DOMContentLoaded();},
    choose:value=>{select.value=value;selectEvents.change();},
    system:dark=>{media.matches=dark;mediaEvents.change();},
    storage:(value,key='mskdsp-platform-theme')=>events.storage({key,newValue:value})};
}
const page=open({dark:true});assert.equal(page.theme(),'dark');page.mount();
assert.equal(page.select.value,'system');page.system(false);assert.equal(page.theme(),'light');
page.choose('dark');assert.equal(page.theme(),'dark');assert.equal(page.saved(),'dark');
page.system(false);assert.equal(page.theme(),'dark');
const reload=open({saved:page.saved()});assert.equal(reload.theme(),'dark');reload.mount();
assert.equal(reload.select.value,'dark');reload.choose('light');reload.system(true);
assert.equal(reload.theme(),'light');reload.choose('system');assert.equal(reload.theme(),'dark');
reload.storage('light');assert.equal(reload.theme(),'light');assert.equal(reload.select.value,'light');
reload.storage('dark','unrelated');assert.equal(reload.theme(),'light');
reload.storage(null);assert.equal(reload.theme(),'dark');assert.equal(reload.select.value,'system');
reload.storage('invalid');assert.equal(reload.select.value,'system');
reload.storage(null,null);assert.equal(reload.theme(),'dark');
const invalid=open({saved:'invalid'});assert.equal(invalid.theme(),'light');invalid.mount();
assert.equal(invalid.select.value,'system');
const blocked=open({blocked:true,dark:true});blocked.mount();blocked.choose('light');
assert.equal(blocked.theme(),'light');blocked.system(true);assert.equal(blocked.theme(),'light');
blocked.choose('system');assert.equal(blocked.theme(),'dark');
'''
        result = subprocess.run([node, "-e", "const SCRIPT=" + json.dumps(script) + ";\n" + scenario],
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
