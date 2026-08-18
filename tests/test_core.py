from __future__ import annotations

import time
import sys
import types
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from app.crawler.equipment import build_catalog, gap_analysis

# 核心规则本身不依赖网络；为无依赖的标准库测试环境提供导入占位。
dotenv = types.ModuleType("dotenv")
dotenv.load_dotenv = lambda *_args, **_kwargs: None
sys.modules.setdefault("dotenv", dotenv)
langchain_openai = types.ModuleType("langchain_openai")
langchain_openai.ChatOpenAI = object
sys.modules.setdefault("langchain_openai", langchain_openai)
try:
    import httpx as _httpx  # noqa: F401 - 已安装时保留真实模块，避免污染后续测试
except ImportError:
    sys.modules.setdefault("httpx", types.ModuleType("httpx"))

from app.email_agent import _ensure_signature, _parse_judge_scores, _render, _rule_check, generate_email
from app.task_utils import TASK_TTL_SECONDS, prune_finished_tasks
from app.tools.search import LocalSearchTool


class EquipmentTests(unittest.TestCase):
    def test_gap_analysis_recommends_only_missing_categories(self) -> None:
        products = [
            {"title": "808nm 激光脱毛仪", "tags": ["产品", "激光脱毛"]},
            {"title": "IPL 光子嫩肤仪", "tags": ["产品", "光子嫩肤"]},
        ]
        catalog = build_catalog(products)

        recommendations = gap_analysis(["激光脱毛"], catalog)

        self.assertTrue(any("光子" in item for item in recommendations))
        self.assertFalse(any("808" in item for item in recommendations))


class LocalSearchIsolationTests(unittest.TestCase):
    def test_search_can_isolate_product_and_template_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixtures = {
                "products.json": [{"title": "产品设备", "content": "激光设备", "tags": ["产品"]}],
                "emails.json": [{"title": "邮件模板", "content": "激光开发信", "tags": ["邮件"]}],
                "customers.json": [{"title": "客户备注", "content": "激光产品", "tags": ["产品"]}],
            }
            for name, records in fixtures.items():
                (root / name).write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")

            products = LocalSearchTool(str(root), include_files=("products.json",)).search("激光", 5)
            templates = LocalSearchTool(str(root), include_files=("emails.json",)).search("激光", 5)

        self.assertEqual([item.title for item in products], ["产品设备"])
        self.assertEqual([item.title for item in templates], ["邮件模板"])


class EmailRuleTests(unittest.TestCase):
    def test_judge_scores_require_numeric_fields_in_range(self) -> None:
        valid = '{"personalization": 4, "value_prop": 3.5, "clarity": 5, "cta": 4, "overall": 4, "suggestions": "加一条 CTA"}'
        scores, status = _parse_judge_scores(valid)
        self.assertEqual(status, "valid")
        self.assertEqual(scores["overall"], 4)
        self.assertEqual(scores["value_prop"], 3.5)

    def test_judge_scores_reject_malformed_and_out_of_range(self) -> None:
        malformed, malformed_status = _parse_judge_scores('{"overall": "5"}')
        out_of_range, out_status = _parse_judge_scores('{"personalization": 6, "value_prop": 3, "clarity": 3, "cta": 3, "overall": 3}')
        self.assertIsNone(malformed)
        self.assertEqual(malformed_status, "malformed")
        self.assertIsNone(out_of_range)
        self.assertEqual(out_status, "malformed")

    def test_valid_english_email_passes_basic_rules(self) -> None:
        email = (
            "Subject: Diode-808 for Glow Clinic\n\n"
            "Hi Glow Clinic, our Diode-808 could complement your current services. "
            "Reply for a catalog. Best regards"
        )

        self.assertEqual(
            _rule_check(email, ["激光脱毛"], ["Diode-808"], language="en"),
            [],
        )

    @patch("app.email_agent.save_email")
    @patch("app.email_agent._stream_email", return_value=("Subject: Test\n\nReply for details. Best regards", 12))
    @patch("app.email_agent.build_llm")
    @patch("app.email_agent._local_records", return_value=[{"title": "Laser", "content": "Diode-808"}])
    def test_selected_template_is_injected_and_reported(self, _records, _llm, stream, _save) -> None:
        tail = "这是用户后来补充、必须保留的关键话术"
        template = {
            "id": "eml-1",
            "title": "香港首次联系",
            "content": "先讲客户业务，再给轻量 CTA" + "甲" * 500 + tail,
        }

        result = generate_email(
            "Glow", "香港", "激光", template_record=template,
            company_profile={"sender_name": "Demo User", "company_name": "DemoMed"},
        )

        prompt = stream.call_args.args[1]
        self.assertIn("先讲客户业务，再给轻量 CTA", prompt)
        self.assertIn(tail, prompt)
        self.assertIn("用户已明确选择上述模板", prompt)
        self.assertEqual(result["templates_used"], ["香港首次联系"])

    def test_render_limit_is_configurable(self) -> None:
        content = "A" * 900
        self.assertEqual(_render([{"title": "T", "content": content}], 800).count("A"), 800)

    def test_email_and_whatsapp_signatures_use_different_shapes(self) -> None:
        profile = {
            "sender_name": "Demo User",
            "company_name": "DemoMed – Medical Aesthetic Equipment",
            "email": "sales@example.com",
            "whatsapp": "+00 000 0000 0000",
            "website": "www.example.com",
        }
        email = _ensure_signature("Best regards", profile, "email")
        whatsapp = _ensure_signature("你好，想了解可以回覆我。", profile, "whatsapp")

        self.assertIn("Email: sales@example.com", email)
        self.assertIn("WhatsApp: +00 000 0000 0000", email)
        self.assertTrue(whatsapp.endswith("Demo User｜DemoMed"))
        self.assertNotIn("sales@example.com", whatsapp)

    def test_existing_signature_is_not_duplicated(self) -> None:
        profile = {
            "sender_name": "Demo User", "company_name": "DemoMed",
            "email": "sales@example.com", "whatsapp": "+00 000",
            "website": "www.example.com",
        }
        signed = "Body\n\nDemo User\nDemoMed\nEmail: sales@example.com\nWhatsApp: +00 000\nWebsite: www.example.com"
        self.assertEqual(_ensure_signature(signed, profile, "email"), signed)


class TaskCleanupTests(unittest.TestCase):
    def test_prune_removes_only_expired_finished_tasks(self) -> None:
        old = time.time() - TASK_TTL_SECONDS - 1
        store = {
            "expired": {"status": "done", "finished_at": old},
            "recent": {"status": "done", "finished_at": time.time()},
            "running": {"status": "running", "created_at": old},
        }

        prune_finished_tasks(store)

        self.assertNotIn("expired", store)
        self.assertIn("recent", store)
        self.assertIn("running", store)


if __name__ == "__main__":
    unittest.main()
