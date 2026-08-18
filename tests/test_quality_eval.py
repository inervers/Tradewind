import json
import unittest
from pathlib import Path

from eval.generate_quality_report import evaluate


class QualityEvaluationTests(unittest.TestCase):
    def test_fixture_report_tracks_rule_and_judge_contracts(self) -> None:
        dataset = Path(__file__).parents[1] / "eval" / "fixtures" / "quality_cases.json"
        cases = json.loads(dataset.read_text(encoding="utf-8"))

        report = evaluate(cases)

        self.assertEqual(report["schema_version"], "twd-quality-v1")
        self.assertEqual(report["dataset"]["n"], 6)
        self.assertEqual(report["rules"]["passed"], 5)
        self.assertEqual(report["judge"]["counts"], {
            "valid": 3,
            "disabled": 1,
            "malformed": 1,
            "unavailable": 1,
        })
        self.assertEqual(report["latency"]["n"], 6)

    def test_rule_failure_is_explainable_without_email_content_in_report(self) -> None:
        report = evaluate([{
            "id": "bad",
            "email": "Dear Sir/Madam, contact us.",
            "product_kws": ["Diode-808"],
            "identifiers": [],
            "language": "en",
            "format": "email",
            "judge_requested": False,
        }])

        case = report["cases"][0]
        self.assertFalse(case["rules_pass"])
        self.assertIn("使用了模板化称呼（Dear Sir/Madam/敬啟者）", case["issues"])
        self.assertNotIn("email", case)


if __name__ == "__main__":
    unittest.main()
