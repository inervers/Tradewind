"""Owned source-web safety acceptance; synthetic HTTP only, never the live app.

Run with the project's Python after npm run build:check. --test selects a
focused unittest name. Task8 can add its UI group without replacing safety.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / ".superpowers/sdd/2026-10-07-local-backup-restore"
OLD = "11111111-1111-4111-8111-111111111111"
NEW = "22222222-2222-4222-8222-222222222222"
COUNTS = dict.fromkeys("customers products templates crawler_seen emails outreach_jobs suppressions followup_records followup_deleted_requests followup_plans active_plans photos".split(), 0)


@contextmanager
def fixture_server():
    # Compile the real shared modules into an owned temporary browser harness.
    # UI is the actual production dist. No backend imports, config or credentials.
    with tempfile.TemporaryDirectory(prefix="tradewind-backup-web-") as directory:
        modules = Path(directory)
        compiler = """
const fs = require('fs'), ts = require('typescript'), path = require('path');
for (const name of ['api', 'tasks', 'types', 'dataEpoch']) {
  const source = path.join('src', name + '.ts');
  if (!fs.existsSync(source)) continue;
  const code = ts.transpileModule(fs.readFileSync(source, 'utf8'), {
    compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022}
  }).outputText.replace(/import.meta.env/g, '({})');
  fs.writeFileSync(path.join(process.argv[1], name + '.js'), code);
}
"""
        subprocess.run(["node", "-e", compiler, str(modules)], cwd=ROOT / "frontend", check=True)
        state = {"epoch": OLD, "mode": "ready", "require_header": False,
                 "has_key": True, "requests": [], "mutations": [], "effects": [],
                 "conflict": False, "task_status": "running", "state_reads": 0}
        customer = {"id": "synthetic-customer", "name": "合成甲店", "country": "香港", "city": "九龙",
                    "email": "", "phone": "", "website": "", "notes": "合成备注", "status": "new", "source": "manual"}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, value, status=200, content_type="application/json"):
                body = json.dumps(value, ensure_ascii=False).encode() if content_type == "application/json" else value
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                self.handle_request()

            do_POST = do_PUT = do_DELETE = do_PATCH = do_GET

            def handle_request(self):
                path = urlsplit(self.path).path
                if path == "/harness":
                    return self.reply(b'<script type="module">import {api} from "/modules/api.js"; import * as tasks from "/modules/tasks.js"; window.api=api; window.tasks=tasks; window.harnessReady=true;</script>', content_type="text/html")
                if path.startswith("/modules/"):
                    file = modules / (path.rsplit("/", 1)[-1].removesuffix(".js") + ".js")
                    return self.reply(file.read_bytes(), content_type="text/javascript")
                if not path.startswith("/api/"):
                    file = ROOT / "frontend/dist" / path.lstrip("/")
                    if path == "/":
                        file = ROOT / "frontend/dist/index.html"
                    mime = "text/javascript" if file.suffix == ".js" else "text/css" if file.suffix == ".css" else "text/html"
                    if file.is_file():
                        return self.reply(file.read_bytes(), content_type=mime)
                    return self.reply({}, 404)
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                request = {"path": path, "method": self.command, "epoch": self.headers.get("X-Tradewind-Data-Epoch"),
                           "content_type": self.headers.get("Content-Type", ""), "body": body.decode(errors="replace")}
                state["requests"].append(request)
                if path == "/api/backup/state":
                    state["state_reads"] += 1
                    return self.reply({"mode": state["mode"], "epoch": state["epoch"], "require_header": state["require_header"],
                                       "revision": 0, "prepared": {"operation_id": "synthetic-op", "rescue_id": "synthetic-rescue", "status": "prepared"} if state["mode"] == "prepared" else None,
                                       "rescues": [], "report": {"status": "none", "operation_id": "", "code": "", "counts": COUNTS}, "estimated_bytes": 0, "photo_count": 0})
                if self.command != "GET":
                    state["mutations"].append(request)
                    if state["require_header"] and request["epoch"] != state["epoch"]:
                        return self.reply({"code": "stale_epoch", "detail": "数据版本已变化，请重新打开页面。"}, 409)
                if state["mode"] != "ready":
                    return self.reply({"code": "maintenance_busy", "detail": "资料正在维护，请稍后重试。"}, 503)
                if path == "/api/config":
                    return self.reply({"has_key": state["has_key"], "providers": [], "company_profile": {}, "vision": {}})
                if path == "/api/outreach/email-settings":
                    return self.reply({"provider": "qq_smtp", "address": "", "transport": "ssl", "host": "smtp.example", "port": 465, "has_credential": False})
                if path == "/api/email/start":
                    if not state["has_key"]:
                        return self.reply({"detail": "请先在设置中配置 API Key"}, 400)
                    return self.reply({"task_id": "synthetic-task"})
                if path.startswith("/api/email/tasks/"):
                    status = state["task_status"]
                    result = {"customer": "合成甲店", "customer_id": "synthetic-customer", "country": "香港", "product": "合成产品", "email": "可复制的合成生成草稿", "issues": [], "scores": None, "tokens": 0, "time_s": 0}
                    return self.reply({"status": status, "total": 1, "done": int(status == "done"), "current": "", "results": [], "stream": "可复制的合成流式草稿" if status == "running" else "", "result": result if status == "done" else None})
                if path.endswith("/followup/records") and self.command == "POST":
                    if state["conflict"]:
                        return self.reply({"detail": "沟通记录请求冲突"}, 409)
                    state["effects"].append(request)
                    return self.reply({"ok": True, "item": {}})
                if path.endswith("/followup/timeline"):
                    return self.reply({"items": [], "has_more": False, "next_offset": 0})
                if path.endswith("/followup"):
                    return self.reply({"plan": None})
                if path == "/api/customers/followup-summary":
                    return self.reply({"today": "2026-10-11", "items": [{"customer_id": customer["id"], "plan": None, "due_status": "unplanned", "last_contact_at": None}]})
                if self.command == "GET":
                    return self.reply([customer] if path == "/api/customers" else [])
                state["effects"].append(request)
                return self.reply({"ok": True, "item": {}, "task_id": "synthetic-task"})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        assert server.server_port != 8101
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", state
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


class SafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="msedge", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        from playwright.sync_api import expect
        self.expect = expect
        self.fixture = fixture_server()
        self.base, self.state = self.fixture.__enter__()
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN", timezone_id="Asia/Shanghai", reduced_motion="reduce")
        self.external, self.errors = [], []
        def local_only(route):
            if route.request.url.startswith(self.base + "/"):
                route.continue_()
            else:
                self.external.append(route.request.url)
                route.abort()
        self.context.route("**/*", local_only)
        self.page = self.context.new_page()
        self.page.set_default_timeout(3500)
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def tearDown(self):
        self.context.close()
        self.fixture.__exit__(None, None, None)
        self.assertEqual(self.external, [])
        self.assertEqual(self.errors, [])

    def harness(self):
        self.page.goto(self.base + "/harness")
        self.page.wait_for_function("window.harnessReady")

    def app(self):
        self.page.goto(self.base)

    def panel(self):
        self.app()
        self.page.get_by_role("button", name="客户名单", exact=True).click()
        self.page.get_by_role("button", name="跟进 合成甲店", exact=True).click()
        self.expect(self.page.get_by_label("沟通内容", exact=True)).to_be_enabled()

    def focus(self):
        self.page.evaluate("window.dispatchEvent(new Event('focus'))")

    def test_fresh_page_attaches_epoch_to_json_delete_and_multipart(self):
        self.harness()
        self.page.evaluate("""async () => {
          await api.products.add({title:'合成',content:'合成'});
          await api.products.update('synthetic', {title:'合成',content:'合成'});
          await api.products.remove('synthetic');
          await api.extractStart(new File(['synthetic'], 'synthetic.txt'), 'products');
        }""")
        self.assertEqual([r["epoch"] for r in self.state["mutations"]], [OLD] * 4)
        self.assertEqual(self.state["mutations"][0]["content_type"], "application/json")
        self.assertIn("multipart/form-data; boundary=", self.state["mutations"][3]["content_type"])
        self.assertIn("synthetic.txt", self.state["mutations"][3]["body"])

    def test_old_page_keeps_draft_and_never_refreshes_epoch_for_submit(self):
        self.panel()
        content = self.page.get_by_label("沟通内容", exact=True)
        content.fill("未保存的合成草稿")
        self.state.update(epoch=NEW, require_header=True)
        self.page.get_by_role("button", name="保存沟通记录", exact=True).click()
        self.expect(self.page.locator(".data-maintenance-notice")).to_contain_text("重新打开")
        self.expect(content).to_have_value("未保存的合成草稿")
        self.expect(content).to_be_enabled()
        self.page.get_by_role("button", name="保存沟通记录", exact=True).click()
        self.assertTrue(all(r["epoch"] == OLD for r in self.state["mutations"]))
        self.assertEqual(self.state["effects"], [])

    def test_old_compose_task_id_not_resumed_on_new_epoch(self):
        self.state.update(epoch=NEW, require_header=True)
        self.context.add_init_script(f"localStorage.setItem('tradewind.task.compose','old-checkpoint'); localStorage.setItem('tradewind.task.epoch',{json.dumps(OLD)});")
        self.app()
        self.expect(self.page.get_by_role("button", name="客户名单", exact=True)).to_be_visible()
        self.assertFalse(any(r["path"].endswith("/old-checkpoint") for r in self.state["requests"]))
        self.assertIsNone(self.page.evaluate("localStorage.getItem('tradewind.task.compose')"))

    def test_first_rollout_preserves_unversioned_compose_reference(self):
        self.state["task_status"] = "done"
        self.context.add_init_script("localStorage.setItem('tradewind.task.compose','existing-checkpoint');")
        self.app()
        self.expect(self.page.locator(".email-body")).to_have_text("可复制的合成生成草稿")
        self.assertEqual(self.page.evaluate("localStorage.getItem('tradewind.task.compose')"), "existing-checkpoint")

    def test_same_epoch_preserves_compose_reference(self):
        self.state["task_status"] = "done"
        self.context.add_init_script(f"localStorage.setItem('tradewind.task.compose','same-checkpoint'); localStorage.setItem('tradewind.task.epoch',{json.dumps(OLD)});")
        self.app()
        self.expect(self.page.locator(".email-body")).to_have_text("可复制的合成生成草稿")

    def test_restored_root_clears_unversioned_compose_reference(self):
        self.state.update(epoch=NEW, require_header=True)
        self.context.add_init_script("localStorage.setItem('tradewind.task.compose','old-checkpoint');")
        self.app()
        self.expect(self.page.get_by_role("button", name="客户名单", exact=True)).to_be_visible()
        self.assertFalse(any(r["path"].endswith("/old-checkpoint") for r in self.state["requests"]))
        self.assertIsNone(self.page.evaluate("localStorage.getItem('tradewind.task.compose')"))

    def test_old_not_found_race_keeps_stream_and_manual_draft(self):
        self.context.add_init_script("localStorage.setItem('tradewind.task.compose','running-checkpoint');")
        self.app()
        self.expect(self.page.locator(".email-body")).to_have_text("可复制的合成流式草稿")
        self.page.locator("textarea").fill("手工补充合成草稿")
        # Keep pre-request/periodic state reads OLD until a real not_found response.
        # Post-response reconciliation must happen before notifying ComposeTab.
        race = []
        def racing_task(route):
            response = route.fetch()
            race.append(response.json()["status"])
            route.fulfill(response=response)
        def racing_state(route):
            response = route.fetch()
            payload = response.json()
            payload["epoch"] = NEW if race else OLD
            route.fulfill(json=payload)
        self.page.route("**/api/email/tasks/running-checkpoint", racing_task)
        self.page.route("**/api/backup/state", racing_state)
        reads = self.state["state_reads"]
        self.state.update(epoch=NEW, require_header=True, task_status="not_found")
        self.expect(self.page.locator(".data-maintenance-notice")).to_contain_text("重新打开")
        self.expect(self.page.locator(".email-body")).to_have_text("可复制的合成流式草稿")
        self.expect(self.page.locator("textarea")).to_have_value("手工补充合成草稿")
        self.assertGreater(self.state["state_reads"], reads)
        self.assertIn("not_found", race)

    def test_generated_draft_remains_copyable_and_actions_refused_when_stale(self):
        self.state["task_status"] = "done"
        self.context.add_init_script("localStorage.setItem('tradewind.task.compose','done-checkpoint');")
        self.app()
        self.expect(self.page.locator(".email-body")).to_have_text("可复制的合成生成草稿")
        self.page.get_by_placeholder("输入客户名称，或从客户名单选择").fill("合成甲店")
        self.state.update(epoch=NEW, require_header=True)
        self.focus()
        self.expect(self.page.locator(".data-maintenance-notice")).to_contain_text("重新打开")
        self.page.get_by_role("button", name="生成开发信", exact=True).click()
        self.page.get_by_role("button", name="加入外联队列", exact=True).click()
        self.expect(self.page.locator(".email-body")).to_have_text("可复制的合成生成草稿")
        self.expect(self.page.get_by_role("button", name="复制", exact=True)).to_be_enabled()
        self.assertEqual(self.state["effects"], [])

    def test_restore_between_click_and_notice_keeps_generated_draft(self):
        self.state["task_status"] = "done"
        self.context.add_init_script("localStorage.setItem('tradewind.task.compose','done-checkpoint');")
        self.app()
        self.expect(self.page.locator(".email-body")).to_have_text("可复制的合成生成草稿")
        self.page.get_by_placeholder("输入客户名称，或从客户名单选择").fill("合成甲店")
        self.page.evaluate("""document.addEventListener('click', event => {
          if (event.target.closest('button')?.textContent.includes('生成开发信')) window.restoreClicked = true;
        }, true)""")
        def click_state(route):
            payload = route.fetch().json()
            payload["epoch"] = NEW if self.page.evaluate("!!window.restoreClicked") else OLD
            route.fulfill(json=payload)
        self.page.route("**/api/backup/state", click_state)
        self.state.update(epoch=NEW, require_header=True)
        # No focus/poll acknowledgement first: the user clicks the old form.
        self.page.get_by_role("button", name="生成开发信", exact=True).click()
        self.expect(self.page.locator(".data-maintenance-notice")).to_contain_text("重新打开")
        self.expect(self.page.locator(".email-body")).to_have_text("可复制的合成生成草稿")
        self.assertEqual(self.state["effects"], [])

    def test_maintenance_error_is_notice_not_empty_data(self):
        self.panel()
        content = self.page.get_by_label("沟通内容", exact=True)
        content.fill("维护期间保留草稿")
        self.state["mode"] = "snapshot"
        self.focus()
        self.expect(self.page.locator(".data-maintenance-notice")).to_contain_text("维护")
        self.expect(content).to_have_value("维护期间保留草稿")
        self.expect(self.page.get_by_role("button", name="跟进 合成甲店", exact=True)).to_have_count(1)
        self.state["mode"] = "ready"
        self.focus()
        self.expect(self.page.locator(".data-maintenance-notice")).to_have_count(0)
        self.expect(content).to_have_value("维护期间保留草稿")

    def test_mounted_form_survives_prepared(self):
        self.panel()
        content = self.page.get_by_label("沟通内容", exact=True)
        content.fill("准备完成仍可复制")
        self.state["mode"] = "prepared"
        self.focus()
        self.expect(self.page.locator(".data-maintenance-notice")).to_contain_text("重启")
        self.expect(content).to_have_value("准备完成仍可复制")
        self.expect(content).to_be_enabled()
        self.page.get_by_role("button", name="保存沟通记录", exact=True).click()
        self.assertEqual(self.state["effects"], [])

    def test_fresh_prepared_boot_never_mounts_config_backed_settings(self):
        self.state["mode"] = "prepared"
        self.app()
        self.expect(self.page.locator(".data-maintenance-notice")).to_contain_text("重启")
        self.expect(self.page.locator("#data-maintenance-controls")).to_be_visible()
        self.page.get_by_role("button", name="设置", exact=True).click()
        for width in (375, 768, 1440):
            self.page.set_viewport_size({"width": width, "height": 900})
            notice = self.page.locator(".data-maintenance-notice").bounding_box()
            self.assertLessEqual(notice["x"] + notice["width"], width + 1)
            self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            self.page.screenshot(path=OUTPUT / f"task-7-prepared-{width}.png")
        self.assertFalse(any(r["path"] in {"/api/config", "/api/outreach/email-settings"} for r in self.state["requests"]))
        self.expect(self.page.get_by_role("heading", name="首次使用", exact=True)).to_have_count(0)
        self.state["mode"] = "ready"
        self.focus()
        self.expect(self.page.get_by_role("button", name="客户名单", exact=True)).to_be_visible()
        self.page.get_by_role("button", name="客户名单", exact=True).click()
        self.expect(self.page.get_by_role("button", name="跟进 合成甲店", exact=True)).to_be_visible()

    def test_normal_followup_conflict_does_not_mark_stale(self):
        self.panel()
        self.state["conflict"] = True
        self.page.get_by_label("沟通内容", exact=True).fill("冲突仍保留草稿")
        self.page.get_by_role("button", name="保存沟通记录", exact=True).click()
        self.expect(self.page.get_by_role("alert")).to_contain_text("冲突")
        self.expect(self.page.locator(".data-maintenance-notice")).to_have_count(0)
        self.expect(self.page.get_by_label("沟通内容", exact=True)).to_have_value("冲突仍保留草稿")

    def test_only_stale_epoch_409_marks_stale_and_refuses_later_mutations(self):
        self.harness()
        self.page.evaluate("api.products.list()")
        # Keep bootstrap on OLD to force the HTTP 409 classification path.
        self.page.route("**/api/products", lambda route: route.fulfill(status=409, content_type="application/json", body='{"code":"stale_epoch","detail":"数据版本已变化"}') if route.request.method == "POST" else route.continue_())
        result = self.page.evaluate("""async () => { let errors=[]; for(let i=0;i<2;i++) { try { await api.products.add({title:'合成',content:'合成'}); } catch(e) { errors.push(e.message); } } return errors; }""")
        self.assertEqual(len(result), 2)
        self.assertIn("重新打开", result[1])

    def test_no_key_can_read_customers_settings_and_backup_without_model_calls(self):
        self.state["has_key"] = False
        self.app()
        self.expect(self.page.get_by_role("heading", name="首次使用", exact=True)).to_be_visible()
        self.expect(self.page.get_by_role("button", name="暂不配置，查看本机资料", exact=True)).to_be_visible()
        self.page.get_by_role("button", name="暂不配置，查看本机资料", exact=True).click()
        self.page.get_by_role("button", name="客户名单", exact=True).click()
        self.expect(self.page.get_by_role("button", name="跟进 合成甲店", exact=True)).to_be_visible()
        self.page.get_by_role("button", name="设置", exact=True).click()
        self.expect(self.page.locator(".page-title")).to_contain_text("设置")
        self.assertTrue(any(r["path"] == "/api/backup/state" for r in self.state["requests"]))
        self.page.get_by_role("button", name="开发信", exact=True).click()
        self.page.get_by_placeholder("输入客户名称，或从客户名单选择").fill("合成甲店")
        self.page.get_by_role("button", name="生成开发信", exact=True).click()
        self.expect(self.page.locator("body")).to_contain_text("请先在设置中配置")
        self.assertFalse(any(r["path"] == "/api/email/start" for r in self.state["requests"]))

    def test_configuring_key_after_no_key_boot_allows_generation(self):
        self.state["has_key"] = False
        self.app()
        self.page.get_by_role("button", name="暂不配置，查看本机资料", exact=True).click()
        self.page.get_by_placeholder("输入客户名称，或从客户名单选择").fill("合成甲店")
        # Another settings consumer can save a key without remounting App.
        self.state["has_key"] = True
        self.page.get_by_role("button", name="生成开发信", exact=True).click()
        self.expect(self.page.locator(".email-body")).to_have_text("可复制的合成流式草稿")
        self.assertEqual(len([r for r in self.state["requests"] if r["path"] == "/api/email/start"]), 1)

    def test_backup_state_read_never_rebinds_old_page_epoch(self):
        self.harness()
        self.page.evaluate("api.products.list()")
        self.state.update(epoch=NEW, require_header=True)
        observed = self.page.evaluate("api.backup.state()")
        self.assertEqual(observed["epoch"], NEW)
        # Import the same module URL that api.ts imports; a .js alias would
        # instantiate a second browser module rather than observe its singleton.
        epoch = self.page.evaluate("async () => (await import('/modules/dataEpoch')).getPageEpoch()")
        self.assertEqual(epoch, OLD)
        failure = self.page.evaluate("async () => { try { await api.products.remove('synthetic'); return ''; } catch(e) { return e.message; } }")
        self.assertIn("重新打开", failure)
        self.assertEqual(self.state["mutations"], [])

    def test_post_response_epoch_check_does_not_reuse_older_inflight_state(self):
        self.harness()
        self.page.evaluate("api.products.list()")
        task_routes, state_routes = [], []
        old_payload = self.context.request.get(self.base + "/api/backup/state").json()
        self.page.route("**/api/email/tasks/held-checkpoint", lambda route: task_routes.append(route))
        self.page.evaluate("""() => {
          const originalFetch = window.fetch.bind(window);
          window.fetch = async (...args) => {
            const response = await originalFetch(...args);
            if (String(args[0]).includes('/email/tasks/held-checkpoint')) {
              await response.clone().json(); window.taskResponseSeen = true;
            }
            return response;
          };
          window.taskResult = '';
          api.task('held-checkpoint').then(t => { window.taskResult=t.status; }, () => { window.taskResult='refused'; });
        }""")
        # The preflight completes; the actual task response remains held.
        self.page.wait_for_function("performance.getEntriesByType('resource').some(e => e.name.endsWith('/api/backup/state'))")
        # Pump the browser until the owned route is reached, with a bounded event wait.
        self.page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        self.assertEqual(len(task_routes), 1)
        self.page.route("**/api/backup/state", lambda route: state_routes.append(route) if not state_routes else route.continue_())
        self.page.evaluate("() => { window.olderState = api.backup.state(); }")
        self.page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        self.assertEqual(len(state_routes), 1)
        self.state.update(epoch=NEW, require_header=True, task_status="not_found")
        task_routes[0].fulfill(status=200, json={"status": "not_found", "total": 0, "done": 0, "current": "", "results": []})
        self.page.wait_for_function("window.taskResponseSeen")
        state_routes[0].fulfill(status=200, json=old_payload)
        self.page.wait_for_function("window.taskResult !== ''")
        self.assertEqual(self.page.evaluate("window.taskResult"), "refused")

    def test_maintenance_503_preserves_running_task_and_polling_recovers(self):
        self.harness()
        self.page.evaluate("""async () => {
          await api.products.list();
          window.seenTasks = [];
          window.pollIntervals = [];
          const originalInterval = window.setInterval.bind(window);
          window.setInterval = (callback, delay) => { window.pollIntervals.push(delay); return originalInterval(callback, delay); };
          document.body.insertAdjacentHTML('beforeend', '<output id="task-status"></output>');
        }""")
        failures = []
        def first_failure(route):
            if not failures:
                failures.append(True)
                route.fulfill(status=503, json={"code": "maintenance_busy", "detail": "资料正在维护"})
            else:
                route.continue_()
        self.page.route("**/api/email/tasks/retry-checkpoint", first_failure)
        self.page.evaluate("() => { tasks.watchTask('compose','retry-checkpoint', t => { window.seenTasks.push(t.status); document.querySelector('#task-status').textContent=t.status; }); }")
        try:
            self.expect(self.page.locator("#task-status")).to_have_text("running")
        except AssertionError:
            self.assertEqual(self.page.evaluate("window.pollIntervals"), [1000],
                             f"Expected retry timer after {len(failures)} observed synthetic maintenance 503 response(s)")
            self.fail(json.dumps({"requests": self.state["requests"], "seen": self.page.evaluate("window.seenTasks"),
                                  "safety": self.page.evaluate("async () => (await import('/modules/dataEpoch')).getDataSafetyState()")}, ensure_ascii=False))
        self.assertEqual(failures, [True])

    def test_new_page_initial_maintenance_resumes_without_setup(self):
        self.state["mode"] = "snapshot"
        self.app()
        self.expect(self.page.locator(".data-maintenance-notice")).to_contain_text("维护")
        self.assertFalse(any(r["path"] == "/api/config" for r in self.state["requests"]))
        self.state["mode"] = "ready"
        self.focus()
        self.expect(self.page.get_by_role("button", name="客户名单", exact=True)).to_be_visible()
        self.expect(self.page.get_by_role("button", name="生成开发信", exact=True)).to_be_visible()

    def test_running_task_survives_tab_switch_and_unsaved_followup_guard(self):
        self.context.add_init_script("localStorage.setItem('tradewind.task.compose','running-checkpoint');")
        self.app()
        self.expect(self.page.locator(".email-body")).to_have_text("可复制的合成流式草稿")
        self.page.get_by_role("button", name="客户名单", exact=True).click()
        self.page.get_by_role("button", name="跟进 合成甲店", exact=True).click()
        content = self.page.get_by_label("沟通内容", exact=True)
        self.expect(content).to_be_enabled()
        content.fill("切页前合成草稿")
        self.page.get_by_role("button", name="开发信", exact=True).click()
        self.expect(self.page.get_by_role("dialog", name="离开客户名单并放弃未保存的跟进内容？")).to_be_visible()
        self.page.get_by_role("button", name="取消", exact=True).click()
        self.expect(content).to_have_value("切页前合成草稿")
        self.page.get_by_role("button", name="开发信", exact=True).click()
        self.page.get_by_role("button", name="放弃并切换", exact=True).click()
        self.expect(self.page.locator(".email-body")).to_have_text("可复制的合成流式草稿")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", choices=["safety"], default="safety")
    parser.add_argument("--test", action="append", default=[])
    arguments = parser.parse_args()
    names = ["SafetyTests." + name for name in arguments.test] if arguments.test else ["SafetyTests"]
    suite = unittest.defaultTestLoader.loadTestsFromNames(names, module=__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
