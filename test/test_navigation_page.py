"""统一导航的页面契约，防止跨页入口、选中状态和回收站层级漂移。"""
from html.parser import HTMLParser
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class NavigationParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_navigation = False
        self.current = None
        self.links = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "nav" and "app-nav" in values.get("class", "").split():
            self.in_navigation = True
        if tag == "a" and self.in_navigation:
            self.current = {"href": values.get("href"), "current": values.get("aria-current"), "text": ""}

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self.current is not None:
            self.current["text"] = self.current["text"].strip()
            self.links.append(self.current)
            self.current = None
        if tag == "nav":
            self.in_navigation = False


class NavigationPageTests(unittest.TestCase):
    # 三个管理页入口相同，且仅当前页面高亮，不把回收站提升到全局导航。
    def test_management_navigation_matches_routes(self):
        expected = [
            ("/modbus-templates", "点表工作台"),
            ("/software", "软件管理"),
            ("/update", "平台维护"),
        ]
        for filename, route in (("modbus-templates.html", "/modbus-templates"), ("software.html", "/software"), ("update.html", "/update")):
            with self.subTest(page=filename):
                parser = NavigationParser()
                page = (ROOT / "web" / filename).read_text(encoding="utf-8")
                parser.feed(page)
                self.assertEqual([(link["href"], link["text"]) for link in parser.links], expected)
                self.assertEqual([link["href"] for link in parser.links if link["current"] == "page"], [route])
                self.assertNotIn('class="app-sidebar"', page)
                self.assertNotIn('程序更新页', page)


if __name__ == "__main__":
    unittest.main()
