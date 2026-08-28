import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = PROJECT_ROOT / "server.py"


def load_server_module():
    spec = importlib.util.spec_from_file_location("aihot_review_server_standalone", SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class AIHotReviewTest(unittest.TestCase):
    def setUp(self):
        self.server = load_server_module()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_dir = Path(self.temp_dir.name) / "data"
        self.server.configure_runtime(self.data_dir, tree_path=None)

    def write_run(self, run_id, signals):
        run = {
            "run_id": run_id,
            "generated_at_bj": "2026-08-28T09:00:00+08:00",
            "since_bj": "2026-08-27T09:00:00+08:00",
            "pull_mode": "hours",
            "total_pulled": len(signals),
            "signal_count": len(signals),
            "signals": signals,
        }
        self.server.write_json_atomic(self.server.RUNS_DIR / f"{run_id}.json", run)
        self.server.invalidate_inbox_cache()

    def test_low_score_items_are_retained(self):
        signal = self.server.score_item(
            {
                "id": "low-score",
                "title": "天气语音指令技巧",
                "summary": "",
                "source": "source",
                "category": "tip",
                "url": "https://example.com/low",
                "publishedAt": "2026-08-28T00:00:00Z",
            }
        )

        self.assertLess(signal["score"], 4)
        self.assertEqual(signal["decision"], "review")
        self.assertEqual(signal["suggested_node"], "")

    def test_empty_inbox_does_not_implicitly_contact_upstream(self):
        with mock.patch.object(self.server, "fetch_aihot") as fetch:
            payload = self.server.query_inbox({"offset": ["0"], "limit": ["10"]})

        fetch.assert_not_called()
        self.assertEqual(payload["runs_count"], 0)
        self.assertEqual(payload["signals"], [])
        self.assertEqual(payload["total_count"], 0)

    def test_inbox_merges_runs_and_keeps_latest_signal_body(self):
        self.write_run(
            "aihot-20260828-090000",
            [
                {"id": "sig-old", "title": "old only", "score": 1, "decision": "review", "tags": []},
                {"id": "sig-dup", "title": "old body", "score": 2, "decision": "review", "tags": []},
            ],
        )
        self.write_run(
            "aihot-20260828-100000",
            [
                {"id": "sig-dup", "title": "new body", "score": 9, "decision": "keep", "tags": ["chips_compute"]},
                {"id": "sig-new", "title": "new only", "score": 4, "decision": "review", "tags": []},
            ],
        )

        payload = self.server.query_inbox({"offset": ["0"], "limit": ["10"]})
        by_id = {signal["id"]: signal for signal in payload["signals"]}

        self.assertEqual(payload["runs_count"], 2)
        self.assertEqual(payload["unique_count"], 3)
        self.assertEqual(by_id["sig-dup"]["title"], "new body")
        self.assertEqual(by_id["sig-dup"]["first_seen_run_id"], "aihot-20260828-090000")
        self.assertEqual(by_id["sig-dup"]["last_seen_run_id"], "aihot-20260828-100000")

    def test_decision_and_view_events_apply_to_signals(self):
        self.write_run(
            "aihot-20260828-090000",
            [
                {"id": "sig-keep", "title": "HBM", "score": 9, "decision": "keep", "strength": "high", "tags": []},
                {"id": "sig-view", "title": "robot", "score": 5, "decision": "review", "strength": "medium", "tags": []},
            ],
        )

        decision = self.server.save_decision(
            {
                "id": "sig-keep",
                "decision": "drop",
                "strength": "low",
                "suggested_node": "ai",
                "user_note": "not useful",
            }
        )
        view = self.server.save_view({"id": "sig-view"})
        payload = self.server.query_inbox({"offset": ["0"], "limit": ["10"]})
        by_id = {signal["id"]: signal for signal in payload["signals"]}

        self.assertEqual(by_id["sig-keep"]["decision"], "drop")
        self.assertEqual(by_id["sig-keep"]["reviewed_at"], decision["reviewed_at"])
        self.assertTrue(by_id["sig-keep"]["viewed_inferred"])
        self.assertEqual(by_id["sig-view"]["viewed_at"], view["viewed_at"])

    def test_cached_inbox_is_patched_without_reloading_runs(self):
        self.write_run(
            "aihot-20260828-090000",
            [{"id": "sig-a", "title": "A", "score": 1, "decision": "review", "strength": "medium", "tags": []}],
        )
        original_load_run = self.server.load_run
        load_calls = []

        def counted_load_run(path):
            load_calls.append(path.name)
            return original_load_run(path)

        self.server.load_run = counted_load_run
        self.server.query_inbox({"offset": ["0"], "limit": ["10"]})
        self.server.save_decision(
            {
                "id": "sig-a",
                "decision": "drop",
                "strength": "low",
                "suggested_node": "ai",
                "user_note": "cached",
            }
        )
        self.server.save_view({"id": "sig-a"})
        result = self.server.query_inbox({"offset": ["0"], "limit": ["10"]})

        self.assertEqual(result["signals"][0]["decision"], "drop")
        self.assertEqual(load_calls, ["aihot-20260828-090000.json"])

    def test_fetch_paginates_and_sends_required_user_agent(self):
        pages = [
            {
                "items": [
                    {
                        "id": "one",
                        "title": "HBM update",
                        "url": "https://example.com/one",
                        "source": "example",
                        "publishedAt": "2026-08-28T00:00:00Z",
                        "category": "industry",
                    }
                ],
                "hasNext": True,
                "nextCursor": "opaque-cursor",
            },
            {
                "items": [
                    {
                        "id": "two",
                        "title": "Robot update",
                        "url": "https://example.com/two",
                        "source": "example",
                        "publishedAt": "2026-08-28T00:01:00Z",
                        "category": "industry",
                    }
                ],
                "hasNext": False,
                "nextCursor": None,
            },
        ]
        requests = []

        def fake_urlopen(request, timeout):
            requests.append(request)
            return FakeResponse(pages[len(requests) - 1])

        with mock.patch.object(self.server.urllib.request, "urlopen", side_effect=fake_urlopen):
            with mock.patch.object(self.server.time, "sleep"):
                result = self.server.fetch_aihot(hours=24)

        self.assertEqual(result["total_pulled"], 2)
        self.assertEqual(len(requests), 2)
        self.assertIn("cursor=opaque-cursor", requests[1].full_url)
        self.assertIn("aihot-skill/0.2.0", requests[0].get_header("User-agent"))
        self.assertTrue((self.data_dir / "aihot-latest.json").exists())
        self.assertFalse(list(self.data_dir.glob(".*.tmp")))

    def test_optional_miner_tree_is_loaded(self):
        tree_path = Path(self.temp_dir.name) / "tree.json"
        tree_path.write_text(
            json.dumps(
                {
                    "nodes": {
                        "ai": {"title": "AI", "status": "expanded", "depth": 0},
                        "models": {"title": "Models", "status": "open", "depth": 1},
                    }
                }
            ),
            encoding="utf-8",
        )
        self.server.configure_runtime(self.data_dir, tree_path=tree_path)

        nodes = self.server.load_tree_nodes()

        self.assertEqual([node["id"] for node in nodes], ["ai", "models"])
        self.assertEqual(
            self.server.infer_node("HBM memory update"),
            "hbm4-capacity-bandwidth-and-ai-memory-supply",
        )

    def test_health_endpoint_reports_local_state_without_upstream_access(self):
        self.write_run(
            "aihot-20260828-090000",
            [{"id": "sig-a", "title": "A", "score": 1, "decision": "review", "tags": []}],
        )
        httpd = self.server.ThreadingHTTPServer(("127.0.0.1", 0), self.server.ReviewHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)

        with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_port}/api/health", timeout=3) as response:
            payload = json.loads(response.read())

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["version"], "0.2.1")
        self.assertEqual(payload["runs_count"], 1)
        self.assertEqual(payload["signal_count"], 1)

    def test_invalid_decision_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "decision must be"):
            self.server.save_decision(
                {
                    "id": "sig-a",
                    "decision": "archive",
                    "strength": "low",
                    "suggested_node": "ai",
                    "user_note": "",
                }
            )


if __name__ == "__main__":
    unittest.main()
