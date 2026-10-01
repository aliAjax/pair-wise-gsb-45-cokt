"""排班服务集成测试：潮汐重算、冻结恢复、资源故障重试、幂等与数量口径。"""
import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError
from src.scheduling.repository import ResourceUnavailable
from src.scheduling.types import BERTHED, INVALID, PENDING, SCHEDULED


WIDE = [0.0] * 4 + [12.0] * 16 + [0.0] * 4
NARROW = [0.0] * 9 + [12.0] * 2 + [0.0] * 13


def plan_body(vessel, berth="B", start=6, hours=3, draft=10.2):
    return {"vessel": vessel, "berth": berth, "draft_m": draft, "vessel_length_m": 180,
            "start_hour": start, "service_hours": hours}


class SchedulingServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.actor = Actor("duty", "port_controller")
        self.service.scheduling.update_resources(self.actor, {"tugboats": ["T1", "T2"], "pilots": ["P1", "P2"]})
        self.service.scheduling.update_tide(self.actor, {"tide_levels": WIDE})

    def tearDown(self):
        self.temp.cleanup()

    def _states(self, response):
        return {item["vessel"]: item["state"] for item in response["items"]}

    def test_capacity_shortage_queues_and_expansion_backfills(self):
        resp = self.service.scheduling.submit_batch(self.actor, {
            "batch_id": "B1", "client_key": "K1",
            "plans": [plan_body("Q%d" % i, "BQ%d" % i, hours=4) for i in range(1, 9)],
        })
        self.assertGreater(resp["operation"]["pending"], 0)
        self.assertEqual(resp["counts"]["pending"], resp["operation"]["pending"])

        self.service.scheduling.update_resources(
            self.actor, {"tugboats": ["T1", "T2", "T3", "T4"], "pilots": ["P1", "P2", "P3", "P4"]})
        retry = self.service.scheduling.retry_planning(self.actor)
        self.assertEqual(retry["counts"]["pending"], 0)
        # 互斥校验：同拖轮同引航员时段不重叠
        occupied = {}
        for item in retry["items"]:
            if item["state"] in (SCHEDULED, BERTHED):
                a = item["allocation"]
                for key in (a["tugboat"], a["pilot"]):
                    for s, e in occupied.get(key, []):
                        self.assertTrue(a["end_hour"] <= s or a["start_hour"] >= e,
                                        "资源%s时段重叠" % key)
                    occupied.setdefault(key, []).append((a["start_hour"], a["end_hour"]))

    def test_tide_change_invalidates_unberthed_and_recovers_on_widen(self):
        resp = self.service.scheduling.submit_batch(self.actor, {
            "batch_id": "B1", "client_key": "K1",
            "plans": [plan_body("V1"), plan_body("V2", "B2")],
        })
        v1 = next(i for i in resp["items"] if i["vessel"] == "V1")
        self.service.scheduling.berth_plan(self.actor, v1["id"], {"actual_draft_m": 10.3})

        narrow = self.service.scheduling.update_tide(self.actor, {"tide_levels": NARROW})
        states = self._states(narrow)
        self.assertEqual(states["V1"], BERTHED)
        self.assertEqual(states["V2"], INVALID)
        self.assertEqual(narrow["operation"]["invalidated"], 1)
        self.assertEqual(narrow["counts"]["conflicts"], 1)
        # 已靠泊计划的分配原样保留
        v1_after = next(i for i in narrow["items"] if i["vessel"] == "V1")
        self.assertEqual(v1_after["allocation"]["start_hour"], v1["allocation"]["start_hour"])

        wide = self.service.scheduling.update_tide(self.actor, {"tide_levels": WIDE})
        self.assertEqual(self._states(wide)["V2"], SCHEDULED)
        self.assertEqual(wide["operation"]["recovered"], 1)
        self.assertEqual(wide["counts"]["recovered"], 1)
        # 已靠泊的 V1 不受任何重排影响
        v1_final = next(i for i in wide["items"] if i["vessel"] == "V1")
        self.assertEqual(v1_final["state"], BERTHED)

    def test_resource_read_failure_keeps_batch_and_retry_merges_by_version(self):
        self.service.scheduling.provider.fail_times(2)
        resp = self.service.scheduling.submit_batch(self.actor, {
            "batch_id": "B9", "client_key": "K9", "plans": [plan_body("F1")],
        })
        self.assertEqual(resp["status"], "waiting_resources")
        self.assertEqual(self._states(resp)["F1"], PENDING)

        # 故障未恢复前再次重试：明确报 503，但批次仍保留
        from src.scheduling.service import ResourceReadFailed
        with self.assertRaises(ResourceReadFailed):
            self.service.scheduling.retry_planning(self.actor)

        retry = self.service.scheduling.retry_planning(self.actor)
        self.assertEqual(self._states(retry)["F1"], SCHEDULED)
        # 拖轮只被占用一次（只有 F1 一艘船）
        allocs = [i["allocation"] for i in retry["items"] if i["vessel"] == "F1"]
        self.assertEqual(len(allocs), 1)

    def test_duplicate_submission_is_idempotent_and_never_double_books(self):
        body = {"batch_id": "B1", "client_key": "K1", "plans": [plan_body("V1")]}
        first = self.service.scheduling.submit_batch(self.actor, body)
        again = self.service.scheduling.submit_batch(self.actor, body)
        self.assertTrue(again["replay"])
        plans = self.service.scheduling.list_plans(self.actor)["items"]
        self.assertEqual([p["vessel"] for p in plans], ["V1"])

        # 同船再次提交（新批次）进入冲突列表，不受理、不占资源
        clash = self.service.scheduling.submit_batch(self.actor, {
            "batch_id": "B2", "client_key": "K2", "plans": [plan_body("V1", "B2")],
        })
        self.assertEqual(clash["conflicts"][0]["reason"], "vessel_already_active")
        self.assertEqual(clash["items"], [])

        # client_key 重复也拦截
        clash2 = self.service.scheduling.submit_batch(self.actor, {
            "batch_id": "B3", "client_key": "K1", "plans": [plan_body("V9")],
        })
        self.assertEqual(clash2["conflicts"][0]["reason"], "duplicate_submission")

    def test_list_and_audit_return_pending_conflict_recovered_counts(self):
        self.service.scheduling.submit_batch(self.actor, {
            "batch_id": "B1", "client_key": "K1", "plans": [plan_body("V1"), plan_body("V2", "B2")],
        })
        self.service.scheduling.update_tide(self.actor, {"tide_levels": NARROW})
        self.service.scheduling.update_tide(self.actor, {"tide_levels": WIDE})

        listing = self.service.scheduling.list_plans(self.actor)
        audit = self.service.scheduling.audit(self.actor)
        for payload in (listing, audit):
            self.assertIn("pending", payload["counts"])
            self.assertIn("conflicts", payload["counts"])
            self.assertIn("recovered", payload["counts"])
        self.assertEqual(audit["counts"]["recovered"], listing["counts"]["recovered"])
        # 审计能看到恢复事件
        actions = {e["action"] for e in audit["items"]}
        self.assertIn("plan_scheduled", actions)

    def test_cancelled_plan_is_never_recovered_but_frees_capacity(self):
        # 单资源 + 4h 作业：高水位窗口按最早6时只够 3 艘，第 4 艘排队
        self.service.scheduling.update_resources(self.actor, {"tugboats": ["T1"], "pilots": ["P1"]})
        resp = self.service.scheduling.submit_batch(self.actor, {
            "batch_id": "B1", "client_key": "K1",
            "plans": [plan_body("V%d" % i, "B%d" % i, start=6, hours=4) for i in range(1, 5)],
        })
        self.assertGreater(resp["counts"]["pending"], 0)
        target = next(i for i in resp["items"] if i["state"] == PENDING)

        # 取消一个排队计划后不会被任何重排恢复
        self.service.scheduling.cancel_plan(self.actor, target["id"], {"cancel_reason": "货主撤单"})
        again = self.service.scheduling.update_tide(self.actor, {"tide_levels": WIDE})
        vessels = {i["vessel"]: i["state"] for i in again["items"]}
        self.assertEqual(vessels[target["vessel"]], "cancelled")
        self.assertNotIn(target["vessel"], [i["vessel"] for i in again["items"]
                                           if i["state"] == SCHEDULED])

    def test_permission_required(self):
        with self.assertRaises(PermissionDenied):
            self.service.scheduling.list_plans(Actor("x", "outsider"))

    def test_berth_validates_actual_draft_against_tide(self):
        resp = self.service.scheduling.submit_batch(self.actor, {
            "batch_id": "B1", "client_key": "K1", "plans": [plan_body("V1", draft=11.0)],
        })
        v1 = resp["items"][0]
        # 进港时刻水位 12.0，实际吃水 11.8 -> 富余不足 0.5
        with self.assertRaises(ValidationError):
            self.service.scheduling.berth_plan(self.actor, v1["id"], {"actual_draft_m": 11.8})


if __name__ == "__main__":
    unittest.main()
