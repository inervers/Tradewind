"""Synthetic browser regressions for nested dialogs and delayed plan-close retries."""

from __future__ import annotations

from datetime import date
import importlib.util
import json
from pathlib import Path
import sys

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("followup_fixture", ROOT / "scripts" / "test-followup-web.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def run_regressions(base: str) -> None:
    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        external = []

        def local_only(route):
            if route.request.url.startswith(base + "/"):
                route.continue_()
            else:
                external.append(route.request.url)
                route.abort()

        context.route("**/*", local_only)

        def open_page():
            page = context.new_page()
            page.set_default_timeout(8000)
            page.goto(base)
            page.get_by_role("button", name="客户名单", exact=True).click()
            page.get_by_role("button", name="跟进 验收甲店", exact=True).click()
            expect(page.get_by_label("沟通内容", exact=True)).to_be_enabled()
            return page

        def check(name, operation):
            page = open_page()
            try:
                operation(page)
                results.append({"name": name, "passed": True})
                print("PASS:", name, flush=True)
            except AssertionError as error:
                results.append({"name": name, "passed": False, "error": str(error)})
                print("FAIL:", name, str(error).splitlines()[0], flush=True)
            finally:
                page.close()

        def cancel_restores_focus(page):
            content = page.get_by_label("沟通内容", exact=True)
            content.fill("合成未保存草稿")
            content.press("Escape")
            expect(page.get_by_role("button", name="取消", exact=True)).to_be_focused()
            page.keyboard.press("Escape")
            expect(page.locator(".confirm-card")).to_have_count(0)
            expect(content).to_be_focused()

        def discard_unlocks_scroll(page):
            page.get_by_label("沟通内容", exact=True).fill("合成关闭草稿")
            page.get_by_role("button", name="关闭客户跟进", exact=True).click()
            page.get_by_role("button", name="放弃并关闭", exact=True).click()
            expect(page.locator(".followup-panel")).to_have_count(0)
            expect(page.locator("body")).not_to_have_css("overflow", "hidden")

        check("cancel nested confirmation restores draft focus", cancel_restores_focus)
        check("discard nested confirmation restores page scrolling", discard_unlocks_scroll)

        for mode in ("complete", "cancel"):
            customer_url = base + "/api/customers/c-qa-0/followup"
            active = context.request.get(customer_url).json()["plan"]
            if active:
                assert context.request.post(f"{customer_url}/plans/{active['id']}/complete").ok
            response = context.request.post(customer_url + "/plans", data={
                "due_date": date.today().isoformat(), "action": "合成原安排",
                "request_id": f"regression-old-{mode}", "expected_plan_id": None})
            assert response.ok
            original = response.json()["item"]

            def retry_close_keeps_new_plan(page, mode=mode, original=original):
                requests = []
                close_url = f"{customer_url}/plans/{original['id']}/{mode}"

                def lost_close_response(route):
                    requests.append(True)
                    if len(requests) == 1:
                        assert route.fetch().ok
                        created = context.request.post(customer_url + "/plans", data={
                            "due_date": date.today().isoformat(), "action": "合成新安排不能被隐藏",
                            "request_id": f"regression-new-{mode}", "expected_plan_id": None})
                        assert created.ok
                        route.fulfill(status=503, content_type="application/json",
                                      body='{"detail":"合成测试：结束安排响应丢失"}')
                    else:
                        route.continue_()

                def close_plan():
                    if mode == "complete":
                        page.get_by_role("button", name="标记完成", exact=True).click()
                    else:
                        page.get_by_role("button", name="取消安排", exact=True).click()
                        page.get_by_role("button", name="确认取消安排", exact=True).click()

                page.route(close_url, lost_close_response)
                close_plan()
                expect(page.get_by_role("alert")).to_contain_text("响应丢失")
                close_plan()
                expect(page.get_by_label("跟进事项", exact=True)).to_have_value("合成新安排不能被隐藏")
                expect(page.locator(".followup-current-plan")).to_contain_text("合成新安排不能被隐藏")

            check(f"delayed {mode} retry displays the newer active plan", retry_close_keeps_new_plan)

        context.close()
        browser.close()
    assert not external, external
    output = ROOT / "outputs" / "followup-checkpoint" / "review-regressions.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    if not all(item["passed"] for item in results):
        raise SystemExit(1)


if __name__ == "__main__":
    with fixture.fixture_server() as (base, _data, _restart):
        run_regressions(base)
