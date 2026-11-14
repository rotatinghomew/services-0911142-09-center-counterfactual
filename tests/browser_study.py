from pathlib import Path
import json
import sys
import tempfile
import threading
import unittest

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class StudyBrowserWorkflow(unittest.TestCase):
    def test_counterfactual_run_freeze_and_reopen(self):
        package = json.loads((ROOT / "package.json").read_text())
        module = package["scripts"]["start"].split()[2]
        server_module = __import__(module + ".server", fromlist=["server"])
        with tempfile.TemporaryDirectory() as state:
            server = server_module.make_server(ROOT, state, port=0)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with sync_playwright() as runtime:
                    browser = runtime.chromium.launch(headless=True)
                    try:
                        page = browser.new_page()
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto("http://127.0.0.1:" + str(server.server_port),
                                  wait_until="networkidle")
                        page.locator("#study-status").filter(has_text="反事实").wait_for()
                        # 默认 home 渠道应通过识别诊断
                        self.assertEqual(
                            page.locator("#study-status").get_attribute("class"), "verdict-ok")
                        self.assertIn("可讨论该反事实轨迹",
                                      page.locator(".verdict-ok").last.inner_text() +
                                      page.locator("#study-conclusion").inner_text())
                        # 污染片区 A1/A2/A3 显示为排除且不可勾选
                        for area in ("A1", "A2", "A3"):
                            box = page.locator("#exclude-" + area)
                            self.assertTrue(box.is_checked())
                            self.assertTrue(box.is_disabled())
                        # 权重表只含合格对照 A4-A8
                        weight_text = page.locator("#study-weights").inner_text()
                        for banned in ("A1", "A2", "A3"):
                            self.assertNotIn(banned, weight_text)
                        for allowed in ("A4", "A8"):
                            self.assertIn(allowed, weight_text)
                        # 切到 total：必须明确显示无法识别
                        page.locator("#study-outcome").select_option("total")
                        page.locator("#study-run").click()
                        page.locator("#study-status.verdict-bad").wait_for()
                        self.assertIn("不输出平滑效果线",
                                      page.locator("#study-conclusion").inner_text())
                        # 手动剔除一个合格片区后重跑，仍能给出结果且诊断更新
                        page.locator("#study-outcome").select_option("home")
                        page.locator("#exclude-A5").check()
                        page.locator("#study-run").click()
                        page.wait_for_function(
                            "() => JSON.parse(document.getElementById('study-details').textContent)"
                            ".selection.excluded_areas.includes('A5')")
                        details = json.loads(page.locator("#study-details").inner_text())
                        excluded = details["selection"]["excluded_areas"]
                        self.assertEqual(excluded, ["A5"])
                        self.assertNotIn("A5", details["analysis"]["outcomes"]["home"]["weights"])
                        # 冻结保存并重新打开，资格与权重保持
                        page.locator("#study-title").fill("浏览器反事实研究")
                        page.locator("#study-save").click()
                        page.locator("#study-status").filter(has_text="已冻结研究").wait_for()
                        frozen = json.loads(page.locator("#study-details").inner_text())
                        page.locator("#study-archive button").first.click()
                        page.locator("#study-status").filter(has_text="已读取冻结研究").wait_for()
                        reopened = json.loads(page.locator("#study-details").inner_text())
                        self.assertEqual(reopened["selection"], frozen["selection"])
                        self.assertEqual(reopened["analysis"]["outcomes"]["home"]["weights"],
                                         frozen["analysis"]["outcomes"]["home"]["weights"])
                        self.assertEqual(reopened["analysis"]["eligibility"],
                                         frozen["analysis"]["eligibility"])
                        # 训练期调整：改成更短窗口，参数被服务端接受并重算
                        page.locator("#study-train-end").fill("30")
                        page.locator("#study-eval-end").fill("30")
                        page.locator("#study-eval-start").fill("20")
                        page.locator("#study-placebo").fill("20")
                        page.locator("#study-run").click()
                        page.wait_for_function(
                            "() => JSON.parse(document.getElementById('study-details').textContent)"
                            ".selection.train_end === 30")
                        self.assertEqual(errors, [])
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                worker.join()
                server.server_close()

    def test_invalid_training_window_shows_server_error(self):
        package = json.loads((ROOT / "package.json").read_text())
        module = package["scripts"]["start"].split()[2]
        server_module = __import__(module + ".server", fromlist=["server"])
        with tempfile.TemporaryDirectory() as state:
            server = server_module.make_server(ROOT, state, port=0)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with sync_playwright() as runtime:
                    browser = runtime.chromium.launch(headless=True)
                    try:
                        page = browser.new_page()
                        page.goto("http://127.0.0.1:" + str(server.server_port),
                                  wait_until="networkidle")
                        page.locator("#study-run").wait_for()
                        page.locator("#study-train-end").fill("3")
                        page.locator("#study-run").click()
                        page.locator("#status.error").wait_for()
                        self.assertIn("训练窗口", page.locator("#status").inner_text())
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                worker.join()
                server.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
