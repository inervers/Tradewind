from __future__ import annotations

import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class CrawlerTaskLifecycleTests(unittest.TestCase):
    def _wait_task(self, task_id: str) -> dict:
        deadline = time.time() + 2
        while time.time() < deadline:
            if server.CRAWLER_TASKS[task_id]["status"] != "running" and not server.CRAWLER_RUN_LOCK.locked():
                break
            time.sleep(0.01)
        return server.CRAWLER_TASKS.pop(task_id)

    def test_web_crawler_task_reaches_done(self) -> None:
        fake_module = types.ModuleType("app.crawler.webs_hunter")
        fake_module.hunt_websites = lambda *_args, **_kwargs: [
            {
                "name": "Example Clinic",
                "website": "https://example.hk",
                "phone": "+85223456789",
            }
        ]

        request = server.CrawlerRequest(
            queries="example clinic",
            country="香港",
            targets=["phone"],
            max_customers=1,
            source="webs",
        )
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(sys.modules, {"app.crawler.webs_hunter": fake_module}), \
                patch.object(server, "CUSTOMERS_FILE", Path(tmp) / "customers.json"), \
                patch.object(server, "CRAWLER_SEEN_FILE", Path(tmp) / "crawler_seen.json"):
            task_id = server.api_crawler_start(request)["task_id"]
            task = self._wait_task(task_id)
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["results"][0]["name"], "Example Clinic")

    def test_two_web_runs_return_different_domains_by_default(self) -> None:
        fake_module = types.ModuleType("app.crawler.webs_hunter")
        candidates = ["alpha.hk", "beta.hk"]

        def fake_hunt(*_args, excluded_domains=None, **_kwargs):
            domain = next((item for item in candidates if item not in (excluded_domains or set())), "")
            return [] if not domain else [{
                "name": domain, "website": f"https://{domain}", "phone": "+85223456789",
            }]

        fake_module.hunt_websites = fake_hunt
        request = server.CrawlerRequest(
            queries="clinic", country="香港", targets=["phone"], max_customers=1, source="webs",
        )
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {"app.crawler.webs_hunter": fake_module}), \
                patch.object(server, "CUSTOMERS_FILE", Path(tmp) / "customers.json"), \
                patch.object(server, "CRAWLER_SEEN_FILE", Path(tmp) / "crawler_seen.json"):
            first_id = server.api_crawler_start(request)["task_id"]
            first = self._wait_task(first_id)
            second_id = server.api_crawler_start(request)["task_id"]
            second = self._wait_task(second_id)

        self.assertEqual(first["results"][0]["website"], "https://alpha.hk")
        self.assertEqual(second["results"][0]["website"], "https://beta.hk")

    def test_two_maps_runs_return_different_places_by_default(self) -> None:
        fake_module = types.ModuleType("app.crawler.maps_hunter")
        candidates = [
            {"name": "Alpha Clinic", "website": "https://alpha.hk", "phone": "+85211112222", "_place_key": "entity:alpha"},
            {"name": "Beta Clinic", "website": "https://beta.hk", "phone": "+85233334444", "_place_key": "entity:beta"},
        ]

        def fake_hunt(*_args, excluded_place_keys=None, exclude_filter=None, **_kwargs):
            for item in candidates:
                if item["_place_key"] in (excluded_place_keys or set()):
                    continue
                candidate = {**item, "country": "香港", "email": "", "wa_link": "", "instrument": ""}
                if exclude_filter and exclude_filter(candidate):
                    continue
                return [candidate]
            return []

        fake_module.hunt_maps_customers = fake_hunt
        request = server.CrawlerRequest(
            queries="clinic", country="香港", targets=["phone"], max_customers=1, source="maps",
        )
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {"app.crawler.maps_hunter": fake_module}), \
                patch.object(server, "CUSTOMERS_FILE", Path(tmp) / "customers.json"), \
                patch.object(server, "CRAWLER_SEEN_FILE", Path(tmp) / "crawler_seen.json"):
            first_id = server.api_crawler_start(request)["task_id"]
            first = self._wait_task(first_id)
            second_id = server.api_crawler_start(request)["task_id"]
            second = self._wait_task(second_id)

        self.assertEqual(first["results"][0]["website"], "https://alpha.hk")
        self.assertEqual(second["results"][0]["website"], "https://beta.hk")

    def test_maps_photo_save_option_is_forwarded(self) -> None:
        fake_module = types.ModuleType("app.crawler.maps_hunter")
        received: list[bool] = []

        def fake_hunt(*_args, save_photos=True, **_kwargs):
            received.append(save_photos)
            return []

        fake_module.hunt_maps_customers = fake_hunt
        request = server.CrawlerRequest(
            queries="clinic", country="香港", targets=["all"], max_customers=1,
            source="maps", save_photos=False,
        )
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {"app.crawler.maps_hunter": fake_module}), \
                patch.object(server, "CUSTOMERS_FILE", Path(tmp) / "customers.json"), \
                patch.object(server, "CRAWLER_SEEN_FILE", Path(tmp) / "crawler_seen.json"):
            task_id = server.api_crawler_start(request)["task_id"]
            task = self._wait_task(task_id)

        self.assertEqual(task["status"], "done")
        self.assertEqual(received, [False])
        self.assertIn("[maps] 视觉照片落盘：已关闭", task["log"])


if __name__ == "__main__":
    unittest.main()
