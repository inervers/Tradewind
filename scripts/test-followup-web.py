"""Source-web acceptance with synthetic data, no keys/model calls or real sending.

Run after frontend build with the project's Python, not LibreOffice Python.
--serve exposes only this temporary fixture for a separate visual QA session.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
import sqlite3
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def fixture_server():
    with tempfile.TemporaryDirectory(prefix="tradewind-followup-") as directory:
        data = Path(directory) / "data"
        data.mkdir()
        rows = [{"id": f"c-qa-{i}", "name": name, "country": "香港", "city": "九龙",
                 "email": "", "website": "", "phone": "", "notes": "合成仪器备注",
                 "source": source, "status": "new"}
                for i, (name, source) in enumerate([
                    ("验收甲店", "maps"), ("验收乙店", "webs"), ("验收丙店", "manual")])]
        (data / "customers.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        for filename in ("products.json", "emails.json"):
            (data / filename).write_text("[]", encoding="utf-8")
        (data / "config.json").write_text(json.dumps({"providers": {"deepseek": {
            "api_key": "synthetic-not-a-real-key"}}}), encoding="utf-8")
        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            port = port_socket.getsockname()[1]
        assert port != 8101
        env = {key: value for key, value in os.environ.items()
               if not key.endswith("_API_KEY") and key not in {"PYTHONPATH", "MEMORY_DB_PATH", "LOCAL_KB_PATH"}}
        env.update(TRADEWIND_DATA_DIR=directory, DEEPSEEK_API_KEY="synthetic-not-a-real-key",
                   TRADEWIND_NO_BROWSER="1", PYTHONIOENCODING="utf-8")
        base = f"http://127.0.0.1:{port}"
        # Production intentionally trusts fixed UI ports. Add only this owned origin
        # in the isolated test process, without weakening the product's origin guard.
        bootstrap = (f"import server, uvicorn; server.LOCAL_UI_ORIGINS.add({base!r}); "
                     f"uvicorn.run(server.app, host='127.0.0.1', port={port}, log_level='warning')")
        command = [sys.executable, "-B", "-c", bootstrap]
        def start():
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.PIPE)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    _, stderr = process.communicate()
                    raise RuntimeError("Isolated server exited before health check: " + stderr.decode("utf-8", errors="replace")[-2000:])
                try:
                    with urlopen(base + "/api/health", timeout=1) as response:
                        if json.load(response)["service"] == "tradewind":
                            return process
                except OSError:
                    time.sleep(0.1)
            process.terminate()
            _, stderr = process.communicate(timeout=10)
            raise RuntimeError("Isolated server health timeout: " + stderr.decode("utf-8", errors="replace")[-2000:])
        process = start()
        def restart():
            nonlocal process
            process.terminate()
            process.wait(timeout=10)
            process = start()
        try:
            yield base, data, restart
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


def acceptance(base, data, restart):
    from playwright.sync_api import sync_playwright, expect
    output = ROOT / "outputs" / "followup-acceptance"
    output.mkdir(parents=True, exist_ok=True)
    checks = []
    def passed(name):
        checks.append(name)
        print("PASS:", name, flush=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce", timezone_id="Asia/Shanghai")
            external = []
            def local_only(route):
                if route.request.url.startswith(base + "/"):
                    route.continue_()
                else:
                    external.append(route.request.url)
                    route.abort()
            context.route("**/*", local_only)
            page = context.new_page()
            page.set_default_timeout(8000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            entry = page.get_by_role("button", name="跟进 验收甲店", exact=True)
            def open_panel():
                entry.click()
                expect(page.get_by_label("沟通内容", exact=True)).to_be_visible()
                expect(page.get_by_label("沟通内容", exact=True)).to_be_enabled()
            def close_panel():
                page.get_by_role("button", name="关闭客户跟进", exact=True).click()
                expect(page.locator(".followup-panel")).to_have_count(0)
            def plan(due, action, modify=False):
                page.get_by_label("跟进日期", exact=True).fill(due)
                page.get_by_label("跟进事项", exact=True).fill(action)
                page.get_by_role("button", name="保存调整" if modify else "保存跟进安排", exact=True).click()
                expect(page.locator(".followup-notice")).to_contain_text("跟进安排已保存")
                expect(page.get_by_role("button", name="标记完成", exact=True)).to_be_enabled()
            today = date.today().isoformat()
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            page.goto(base)
            page.get_by_role("button", name="客户名单", exact=True).click()
            expect(entry).to_be_visible()
            passed("follow-up entry inside customer list")
            open_panel()
            page.get_by_label("沟通内容", exact=True).fill("验收沟通记录一")
            page.get_by_role("button", name="保存沟通记录", exact=True).dblclick()
            expect(page.locator(".followup-notice")).to_have_text("沟通记录已保存")
            expect(page.get_by_label("沟通内容", exact=True)).to_have_value("")
            expect(page.locator(".followup-timeline p", has_text="验收沟通记录一")).to_have_count(1)
            passed("record save and double-click does not duplicate")
            plan(today, "今日补发参数")
            close_panel()
            expect(entry).to_be_focused()
            expect(page.locator(".customer-followup-summary").first).to_contain_text(today)
            passed("active schedule summary and focus restoration")
            page.get_by_role("button", name="今日跟进").click()
            expect(page.locator("tbody tr")).to_have_count(1)
            page.get_by_role("button", name="Webs", exact=True).click()
            expect(entry).to_have_count(0)
            page.get_by_role("button", name="Maps", exact=True).click()
            expect(entry).to_be_visible()
            passed("date/source filters intersect")
            page.get_by_role("checkbox", name="选择客户 验收甲店", exact=True).check()
            page.get_by_role("button", name="未安排跟进").click()
            expect(page.get_by_role("button", name="批量删除", exact=True)).to_have_count(0)
            page.get_by_role("button", name="全部跟进").click()
            passed("hidden selections removed on filter change")
            open_panel()
            plan(yesterday, "已调整的逾期安排", True)
            close_panel()
            page.get_by_role("button", name="逾期跟进").click()
            expect(page.locator("tbody tr")).to_have_count(1)
            open_panel()
            expect(page.locator(".followup-timeline")).to_contain_text("调整跟进安排")
            page.get_by_role("button", name="标记完成", exact=True).click()
            expect(page.locator(".followup-notice")).to_contain_text("安排已完成")
            expect(page.get_by_label("跟进事项", exact=True)).to_have_value("")
            close_panel()
            expect(entry).to_have_count(0)
            page.get_by_role("button", name="全部跟进").click()
            passed("schedule replacement/completion and live filter update")
            open_panel()
            plan(today, "要取消的安排")
            page.get_by_role("button", name="取消安排", exact=True).click()
            page.get_by_role("button", name="确认取消安排", exact=True).click()
            expect(page.locator(".followup-notice")).to_contain_text("安排已取消")
            expect(page.locator(".followup-timeline")).to_contain_text("安排已取消")
            passed("schedule cancellation retained in timeline")

            # Simulate lost response AFTER the isolated server commits; retry must use the same id.
            bodies = []
            def lost_response(route):
                bodies.append(json.loads(route.request.post_data))
                if len(bodies) == 1:
                    response = route.fetch()
                    assert response.ok
                    route.fulfill(status=503, content_type="application/json", body='{"detail":"验收模拟：响应丢失"}')
                else:
                    route.continue_()
            record_url = base + "/api/customers/c-qa-0/followup/records"
            page.route(record_url, lost_response)
            page.get_by_label("沟通内容", exact=True).fill("响应丢失也不能重复记录")
            page.get_by_role("button", name="保存沟通记录", exact=True).click()
            expect(page.get_by_role("alert")).to_contain_text("响应丢失")
            expect(page.get_by_label("沟通内容", exact=True)).to_have_value("响应丢失也不能重复记录")
            page.get_by_role("button", name="保存沟通记录", exact=True).click()
            expect(page.locator(".followup-notice")).to_contain_text("沟通记录已保存")
            assert bodies[0]["request_id"] == bodies[1]["request_id"]
            expect(page.locator(".followup-timeline p", has_text="响应丢失也不能重复记录")).to_have_count(1)
            page.unroute(record_url, lost_response)
            passed("lost-response retry keeps draft and request id, single stored record")
            page.get_by_label("沟通内容", exact=True).fill("暂未保存的草稿")
            page.get_by_label("沟通内容", exact=True).press("Escape")
            expect(page.get_by_role("dialog", name="放弃未保存的跟进内容？")).to_be_visible()
            page.keyboard.press("Escape")
            expect(page.get_by_label("沟通内容", exact=True)).to_have_value("暂未保存的草稿")
            page.get_by_role("button", name="设置", exact=True).click()
            expect(page.get_by_role("dialog", name="离开客户名单并放弃未保存的跟进内容？")).to_be_visible()
            page.get_by_role("button", name="取消", exact=True).click()
            expect(page.get_by_label("沟通内容", exact=True)).to_have_value("暂未保存的草稿")
            page.get_by_role("button", name="设置", exact=True).click()
            page.get_by_role("button", name="放弃并切换", exact=True).click()
            expect(page.locator(".followup-panel")).to_have_count(0)
            page.get_by_role("button", name="客户名单", exact=True).click()
            open_panel()
            expect(page.get_by_label("沟通内容", exact=True)).to_have_value("")
            expect(page.locator(".followup-timeline")).to_contain_text("验收沟通记录一")
            passed("Escape/tab unsaved confirmation, cancel preserves and discard drops only draft")

            # Guard customer changes even if requested while the old drawer is open.
            page.get_by_label("沟通内容", exact=True).fill("切换客户前的草稿")
            page.get_by_role("button", name="跟进 验收乙店", exact=True).dispatch_event("click")
            expect(page.get_by_role("dialog", name="切换客户并放弃未保存的跟进内容？")).to_be_visible()
            page.get_by_role("button", name="取消", exact=True).click()
            expect(page.get_by_label("沟通内容", exact=True)).to_have_value("切换客户前的草稿")
            page.get_by_role("button", name="跟进 验收乙店", exact=True).dispatch_event("click")
            page.get_by_role("button", name="放弃并切换", exact=True).click()
            expect(page.get_by_role("dialog", name="验收乙店", exact=True)).to_be_visible()
            expect(page.get_by_label("沟通内容", exact=True)).to_have_value("")
            close_panel(); open_panel()
            passed("customer switch confirmation prevents draft leak between customers")

            held_requests = []
            def hold_record(route):
                held_requests.append(route)
            page.route(record_url, hold_record)
            page.get_by_label("沟通内容", exact=True).fill("保存中不能离开")
            page.get_by_role("button", name="保存沟通记录", exact=True).click()
            expect(page.get_by_role("button", name="关闭客户跟进", exact=True)).to_be_disabled()
            page.get_by_role("button", name="设置", exact=True).click()
            expect(page.get_by_role("dialog", name="验收甲店", exact=True)).to_be_visible()
            # busy is synchronous; the POST still awaits the epoch/readiness GET.
            # Pump Playwright until the actual interception arrives, bounded.
            dispatch_deadline = time.monotonic() + 8
            while not held_requests and time.monotonic() < dispatch_deadline:
                page.wait_for_timeout(20)
            assert len(held_requests) == 1
            held_requests[0].fulfill(status=503, content_type="application/json", body='{"detail":"验收模拟：稍后重试"}')
            expect(page.get_by_role("alert")).to_contain_text("稍后重试")
            expect(page.get_by_label("沟通内容", exact=True)).to_have_value("保存中不能离开")
            page.unroute(record_url, hold_record)
            page.get_by_label("沟通内容", exact=True).fill("")
            passed("busy saves block close/tab switch; failed save preserves draft")

            # Trap keyboard at the panel boundaries, including initially focused container.
            page.locator(".followup-panel").focus()
            page.keyboard.press("Shift+Tab")
            assert page.evaluate("!!document.activeElement.closest('.followup-panel')")
            for _ in range(26):
                page.keyboard.press("Tab")
                assert page.evaluate("!!document.activeElement.closest('.followup-panel')")
            passed("keyboard focus trap")

            plan(today, "持久化安排")
            second = context.new_page()
            second.goto(base)
            second.get_by_role("button", name="客户名单", exact=True).click()
            second.get_by_role("button", name="跟进 验收甲店", exact=True).click()
            expect(second.get_by_label("跟进事项", exact=True)).to_have_value("持久化安排")
            second.get_by_label("跟进事项", exact=True).fill("另一窗口调整")
            second.get_by_role("button", name="保存调整", exact=True).click()
            expect(second.locator(".followup-notice")).to_contain_text("跟进安排已保存")
            page.get_by_label("跟进事项", exact=True).fill("过期窗口不能覆盖")
            page.get_by_role("button", name="保存调整", exact=True).click()
            expect(page.get_by_role("alert")).to_be_visible()
            expect(page.get_by_label("跟进事项", exact=True)).to_have_value("过期窗口不能覆盖")
            page.get_by_role("button", name="重新读取", exact=True).click()
            page.get_by_role("button", name="放弃并重新读取", exact=True).click()
            expect(page.get_by_label("跟进事项", exact=True)).to_have_value("另一窗口调整")
            second.close()
            passed("two-window optimistic concurrency conflict keeps input")

            # Synthetic history only, through the exact local API; no generation or send endpoints.
            for i in range(35):
                response = context.request.post(record_url, data={"request_id": f"qa-pagination-{i}",
                    "contacted_at": "2020-01-01T08:00:00+08:00", "channel": "other", "outcome": "note", "content": f"分页记录{i:02d}"})
                assert response.ok
            close_panel(); open_panel()
            expect(page.get_by_role("button", name="加载更多", exact=True)).to_be_visible()
            timeline_url = base + "/api/customers/c-qa-0/followup/timeline?limit=30&offset=0"
            timeline_failures = []
            def fail_refresh(route):
                timeline_failures.append(True)
                if len(timeline_failures) == 1:
                    route.fulfill(status=503, content_type="application/json", body='{"detail":"验收模拟：时间线暂不可用"}')
                else:
                    route.continue_()
            page.route(timeline_url, fail_refresh)
            page.get_by_label("沟通内容", exact=True).fill("时间线失败不重复保存")
            page.get_by_role("button", name="保存沟通记录", exact=True).click()
            expect(page.get_by_role("alert")).to_contain_text("时间线暂不可用")
            expect(page.get_by_label("沟通内容", exact=True)).to_have_value("")
            page.get_by_role("button", name="重试时间线", exact=True).click()
            expect(page.locator(".followup-timeline p", has_text="时间线失败不重复保存")).to_have_count(1)
            page.unroute(timeline_url, fail_refresh)
            passed("timeline refresh retry reloads newest page without repeating saved record")
            page.get_by_role("button", name="加载更多", exact=True).click()
            timeline = context.request.get(base + "/api/customers/c-qa-0/followup/timeline?limit=100").json()
            expect(page.locator(".followup-timeline li")).to_have_count(len(timeline["items"]))
            expect(page.locator(".followup-timeline")).to_contain_text("分页记录00")
            assert sum(item["kind"] == "record" for item in timeline["items"]) == 38
            passed("timeline pagination without global job cutoff")
            delete = page.get_by_role("button", name="删除沟通记录", exact=False).first
            old_count = page.locator(".followup-timeline li").count()
            delete.click()
            page.get_by_role("button", name="取消", exact=True).click()
            assert page.locator(".followup-timeline li").count() == old_count
            delete.click()
            page.get_by_role("button", name="删除记录", exact=True).click()
            expect(page.locator(".followup-notice")).to_contain_text("沟通记录已删除")
            passed("manual record deletion requires confirmation")
            close_panel()
            restart()
            page.reload()
            page.get_by_role("button", name="客户名单", exact=True).click()
            open_panel()
            expect(page.get_by_label("跟进事项", exact=True)).to_have_value("另一窗口调整")
            expect(page.locator(".followup-timeline")).to_contain_text("验收沟通记录一")
            passed("page reload and backend restart persistence")

            for width in (375, 768, 1440):
                page.set_viewport_size({"width": width, "height": 900})
                rect = page.locator(".followup-panel").bounding_box()
                assert rect and rect["x"] >= -1 and rect["y"] >= -1
                assert rect["x"] + rect["width"] <= width + 1 and rect["y"] + rect["height"] <= 901
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.locator(".followup-scroll").evaluate("element => element.scrollTop = 0")
                page.screenshot(path=output / f"panel-{width}.png")
                passed(f"viewport {width}: contained panel, no page horizontal overflow")
            close_panel()
            page.get_by_role("button", name="跟进 验收丙店", exact=True).click()
            expect(page.get_by_label("沟通内容", exact=True)).to_be_enabled()
            page.get_by_label("沟通内容", exact=True).fill("客户删除后仍能复制的草稿")
            assert context.request.delete(base + "/api/customers/c-qa-2").ok
            page.evaluate("window.dispatchEvent(new Event('focus'))")
            expect(page.get_by_role("alert")).to_contain_text("客户已从名单中删除")
            expect(page.get_by_label("沟通内容", exact=True)).to_have_value("客户删除后仍能复制的草稿")
            expect(page.get_by_role("button", name="保存沟通记录", exact=True)).to_be_disabled()
            page.get_by_role("button", name="关闭客户跟进", exact=True).click()
            page.get_by_role("button", name="放弃并关闭", exact=True).click()
            passed("external deletion preserves unsaved input for copying")
            row = page.locator("tbody tr", has=entry)
            row.get_by_role("button", name="删除", exact=True).click()
            page.get_by_role("dialog").get_by_role("button", name="删除", exact=True).click()
            expect(entry).to_have_count(0)
            with closing(sqlite3.connect(data / "tradewind_memory.db")) as connection:
                assert connection.execute("SELECT COUNT(*) FROM customer_followup_records WHERE customer_id='c-qa-0'").fetchone()[0] == 0
                assert connection.execute("SELECT COUNT(*) FROM customer_followup_plans WHERE customer_id='c-qa-0'").fetchone()[0] == 0
            assert json.loads((data / "customers.json").read_text(encoding="utf-8"))[0]["notes"] == "合成仪器备注"
            passed("customer delete cleans followups and preserves other customer notes")
            assert not external, external
            assert not errors, errors
            passed("no external browser requests or uncaught frontend errors")
            (output / "web-acceptance.json").write_text(json.dumps({"passed": True, "checks": checks, "external_requests": external, "frontend_errors": errors}, ensure_ascii=False, indent=2), encoding="utf-8")
            context.close()
        finally:
            browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    with fixture_server() as (base, data, restart):
        if args.serve:
            print(base, flush=True)
            try:
                while True:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                pass
        else:
            acceptance(base, data, restart)
            print("Follow-up web acceptance passed.")
