import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from fineapi_core.model_status import (
    analyze,
    extract_status_code,
    parse_other,
    render,
)

NOW = 1_700_000_000


def log(model, user_id, status=None, *, content="", age=60, error_code=None):
    other = {}
    if status is not None:
        other["status_code"] = status
    if error_code:
        other["error_code"] = error_code
    return {
        "model_name": model,
        "user_id": user_id,
        "created_at": NOW - age,
        "content": content,
        "other": other,
    }


PERF = {
    "summary": {"success_rate": 0.98},
    "models": [
        {"model_name": "model-a", "success_rate": 0.95},
        {"model_name": "model-b", "success_rate": 0.72},
        {"model_name": "model-c", "success_rate": 1.0},
        {"model_name": "model-d", "success_rate": 0.99},
    ],
}


class ModelStatusTest(unittest.TestCase):
    def test_parse_other(self):
        self.assertEqual(parse_other({"other": '{"status_code":502}'}), {"status_code": 502})
        self.assertEqual(parse_other({"other": {"status_code": 500}}), {"status_code": 500})
        self.assertEqual(parse_other({"other": "not-json"}), {})

    def test_status_fallback_from_content(self):
        item = {"content": "upstream error (status code: 502)"}
        self.assertEqual(extract_status_code(item, {}), 502)

    def test_analysis_excludes_4xx_and_dedupes_users(self):
        items = [
            log("model-a", 1, 500),
            log("model-a", 1, 502),  # 同一用户重复，只算 1 个用户
            log("model-b", 1, 500),
            log("model-b", 2, 503),  # 两个不同用户 -> 异常
            log("model-c", 3, 400),
            log("model-c", 4, 429),
            log("model-d", 5, content="status code: 500"),
            log("model-z", 6, 500, age=3600),  # 窗口外
        ]
        result = analyze(
            items,
            perf_summary=PERF,
            window_minutes=30,
            abnormal_users=2,
            top_n=10,
            now=NOW,
        )
        by_model = {row["model"]: row for row in result.models}

        self.assertEqual(by_model["model-a"]["level"], "warn")
        self.assertEqual(by_model["model-a"]["error_users"], 1)
        self.assertEqual(by_model["model-a"]["error_count"], 2)

        self.assertEqual(by_model["model-b"]["level"], "down")
        self.assertEqual(by_model["model-b"]["error_users"], 2)

        self.assertEqual(by_model["model-d"]["level"], "warn")
        self.assertEqual(by_model["model-d"]["error_users"], 1)

        self.assertNotIn("model-z", by_model)
        self.assertEqual(result.ignored_client_errors, 2)
        self.assertEqual(by_model["model-c"]["client_error_count"], 2)

    def test_single_user_abnormal_threshold(self):
        result = analyze(
            [log("model-a", 1, 500)],
            window_minutes=30,
            abnormal_users=1,
            now=NOW,
        )
        self.assertEqual(result.models[0]["level"], "down")

    def test_include_client_errors(self):
        result = analyze(
            [log("model-a", 1, 400)],
            window_minutes=30,
            exclude_client_errors=False,
            abnormal_users=2,
            now=NOW,
        )
        self.assertEqual(result.models[0]["level"], "warn")
        self.assertEqual(result.ignored_client_errors, 0)

    def test_render_warns_when_error_logs_missing(self):
        result = analyze(
            [],
            perf_summary={
                "summary": {"success_rate": 0.5},
                "models": [{"model_name": "model-x", "success_rate": 0.5}],
            },
            window_minutes=30,
            now=NOW,
        )
        text = render(result, now=NOW)
        self.assertIn("ERROR_LOG_ENABLED", text)
        self.assertIn("50.0%", text)

    def test_render(self):
        result = analyze(
            [
                log("model-b", 1, 500),
                log("model-b", 2, 502, error_code="upstream_error"),
                log("model-a", 3, 500),
                log("model-c", 4, 400),
            ],
            perf_summary=PERF,
            window_minutes=30,
            abnormal_users=2,
            now=NOW,
        )
        text = render(result, now=NOW, show_ok=True)
        self.assertIn("近 30 分钟", text)
        self.assertIn("异常", text)
        self.assertIn("波动", text)
        self.assertIn("model-b", text)
        self.assertIn("已忽略 4xx", text)
        self.assertIn("98.0%", text)


if __name__ == "__main__":
    unittest.main()
