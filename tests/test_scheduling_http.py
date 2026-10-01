"""排班 HTTP 接口端到端测试。"""
import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path

from app import build_service
from src.http_api import create_server


WIDE = [0.0] * 4 + [12.0] * 16 + [0.0] * 4
NARROW = [0.0] * 9 + [12.0] * 2 + [0.0] * 13
HEADERS = {"X-User-Id": "duty", "X-Role": "port_controller", "Content-Type": "application/json"}


class HttpSchedulingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "http.db"))
        self.server = create_server("127.0.0.1", 0, self.service, Path("static"))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def _request(self, method, path, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method, headers=HEADERS)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_recoverable_schedule_over_http(self):
        self._request("POST", "/api/scheduling/resources",
                      {"tugboats": ["T1"], "pilots": ["P1"]})
        self._request("POST", "/api/scheduling/tide", {"tide_levels": WIDE})

        # 两艘船同抢唯一拖轮 -> 第二艘排队顺延
        status, body = self._request("POST", "/api/scheduling/batches", {
            "batch_id": "B1", "client_key": "K1",
            "plans": [
                {"vessel": "V1", "berth": "B1", "draft_m": 10.2, "vessel_length_m": 180,
                 "start_hour": 6, "service_hours": 2},
                {"vessel": "V2", "berth": "B2", "draft_m": 10.2, "vessel_length_m": 180,
                 "start_hour": 6, "service_hours": 2},
            ],
        })
        self.assertEqual(status, 200)
        self.assertIn("operation", body)
        self.assertEqual(body["counts"]["scheduled"], 2)

        # 重复提交幂等
        status, replay = self._request("POST", "/api/scheduling/batches", {
            "batch_id": "B1", "client_key": "K1", "plans": []})
        self.assertTrue(replay["replay"])

        # V1 靠泊冻结
        plan_id = body["items"][0]["id"]
        status, berthed = self._request("POST", "/api/scheduling/plans/%d/berth" % plan_id,
                                        {"actual_draft_m": 10.3})
        self.assertEqual(berthed["items"][0]["state"], "berthed")

        # 资源读取失败：批次保留；接口仍返回待确认数量
        self.service.scheduling.provider.fail_times(2)
        status, failed = self._request("POST", "/api/scheduling/batches", {
            "batch_id": "B2", "client_key": "K2",
            "plans": [{"vessel": "V9", "berth": "B9", "draft_m": 10.2, "vessel_length_m": 180,
                       "start_hour": 6, "service_hours": 3}]})
        self.assertEqual(failed["status"], "waiting_resources")
        self.assertEqual(failed["counts"]["pending"], 1)
        status, retry_fail = self._request("POST", "/api/scheduling/retry", {})
        self.assertEqual(status, 503)
        self.assertEqual(retry_fail["error"], "resource_unavailable")
        status, retried = self._request("POST", "/api/scheduling/retry", {})
        self.assertEqual(status, 200)
        self.assertEqual(retried["counts"]["pending"], 0)

        # 窗口收窄：已靠泊 V1 不动，3h 的 V9 失效；列表与审计都带三个数量
        status, narrow = self._request("POST", "/api/scheduling/tide", {"tide_levels": NARROW})
        self.assertEqual(narrow["counts"]["conflicts"], 1)
        status, listing = self._request("GET", "/api/scheduling/plans", None)
        self.assertEqual(set(["pending", "conflicts", "recovered"]).issubset(listing["counts"]), True)
        status, audit = self._request("GET", "/api/scheduling/audit", None)
        self.assertEqual(audit["counts"]["conflicts"], listing["counts"]["conflicts"])

        # 放宽恢复
        status, wide = self._request("POST", "/api/scheduling/tide", {"tide_levels": WIDE})
        self.assertEqual(wide["counts"]["recovered"], 1)
        self.assertEqual(wide["operation"]["recovered"], 1)


if __name__ == "__main__":
    unittest.main()
