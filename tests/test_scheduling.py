import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ServiceUnavailable, ValidationError


def vessel_data(ref, berth="B1", eta=6, etd=18, draft=10.2):
    return {
        "vessel": ref, "berth": berth, "vessel_length_m": 180, "berth_length_m": 220,
        "draft_m": draft, "berth_depth_m": 11.5, "eta_hour": eta, "etd_hour": etd,
        "risk_level": "medium", "dangerous_goods": False, "dangerous_class": "",
    }


NARROW = [{"start_hour": 2, "end_hour": 3, "water_level_m": 9.0}]
WIDE = [{"start_hour": 0, "end_hour": 24, "water_level_m": 13.0}]


class SchedulingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.actor = Actor("duty", "port_controller")

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, ref, **kwargs):
        return self.service.create(self.actor, ref, vessel_data(ref, **kwargs))["id"]

    def test_batch_schedules_tide_slots_and_counts(self):
        ids = [self._create("V-%d" % i) for i in range(3)]
        result = self.service.submit_batch(self.actor, ids)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["counts"]["pending_confirmation"], 3)
        self.assertEqual(result["counts"]["conflict"], 0)
        # 默认潮汐窗口 0-6/12-18，ETA=6 后最早可行槽位是12点
        self.assertEqual([i["start_hour"] for i in result["items"]], [12, 13, 14])
        # 同一拖轮同一整点只服务一艘：槽位彼此不重叠
        tug_hours = {(i["tug_id"], h) for i in result["items"] for h in range(i["start_hour"], i["end_hour"])}
        self.assertEqual(len(tug_hours), sum(i["end_hour"] - i["start_hour"] for i in result["items"]))

    def test_capacity_shortage_queues(self):
        # 1个泊位、2拖轮、2引航员，窗口内只有2个整点 -> 第3艘排队到窗口外
        ids = [self._create("Q-%d" % i, eta=0, etd=2) for i in range(3)]
        result = self.service.submit_batch(self.actor, ids)
        statuses = sorted(i["status"] for i in result["items"])
        self.assertEqual(statuses, ["pending", "pending", "queued"])
        self.assertEqual(result["counts"]["queued"], 1)

    def test_duplicate_submission_does_not_double_occupy(self):
        ids = [self._create("D-%d" % i) for i in range(2)]
        first = self.service.submit_batch(self.actor, ids)
        slots = {i["record_id"]: (i["tug_id"], i["start_hour"]) for i in first["items"]}
        second = self.service.submit_batch(self.actor, ids)
        self.assertEqual(second["merged"], ids)
        self.assertEqual(second["counts"]["merged"], 2)
        for item in second["items"]:
            self.assertTrue(item["merged"])
            self.assertEqual((item["tug_id"], item["start_hour"]), slots[item["record_id"]])

    def test_pilot_and_tug_cannot_double_book(self):
        ids = [self._create("R-%d" % i) for i in range(4)]
        result = self.service.submit_batch(self.actor, ids)
        used = set()
        for item in result["items"]:
            for hour in range(item["start_hour"], item["end_hour"]):
                self.assertNotIn(("tug", item["tug_id"], hour), used)
                self.assertNotIn(("pilot", item["pilot_id"], hour), used)
                used.add(("tug", item["tug_id"], hour))
                used.add(("pilot", item["pilot_id"], hour))

    def test_confirm_schedule_locks_resources(self):
        rid = self._create("C-1")
        self.service.submit_batch(self.actor, [rid])
        record = self.service.get_record(self.actor, rid)
        confirmed = self.service.confirm_schedule(self.actor, rid, record["version"], {})
        self.assertEqual(confirmed["state"], "confirmed")
        plan = [p for p in self.service.plans(self.actor)["items"] if p["record"]["id"] == rid][0]
        self.assertEqual(plan["schedule"]["status"], "active")
        # 已确认的计划重复提交：幂等合并，不会再占一艘拖轮
        again = self.service.submit_batch(self.actor, [rid])
        self.assertEqual(again["merged"], [rid])
        self.assertEqual(again["items"][0]["status"], "active")

    def test_confirm_schedule_requires_pending_assignment(self):
        rid = self._create("C-2")
        record = self.service.get_record(self.actor, rid)
        with self.assertRaises(ValidationError):
            self.service.confirm_schedule(self.actor, rid, record["version"], {})


class TideRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.actor = Actor("duty", "port_controller")

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, ref, draft=10.2):
        data = vessel_data(ref, draft=draft)
        return self.service.create(self.actor, ref, data)["id"]

    def test_narrow_then_widen_restores_only_system_plans(self):
        ids = [self._create("T-%d" % i) for i in range(3)]
        self.service.submit_batch(self.actor, ids)
        narrowed = self.service.update_tide_windows(self.actor, NARROW)
        self.assertEqual(narrowed["counts"]["conflict"], 3)
        self.assertEqual(narrowed["counts"]["restored"], 0)
        widened = self.service.update_tide_windows(self.actor, WIDE)
        self.assertEqual(widened["counts"]["restored"], 3)
        self.assertEqual(set(widened["restored"]), set(ids))

    def test_berthed_result_is_locked(self):
        rid = self._create("B-1")
        self.service.submit_batch(self.actor, [rid])
        record = self.service.get_record(self.actor, rid)
        record = self.service.confirm_schedule(self.actor, rid, record["version"], {})
        record = self.service.act(self.actor, rid, record["version"], "berth", {"actual_draft_m": 10.3})
        berthed_assignment = [p for p in self.service.plans(self.actor)["items"] if p["record"]["id"] == rid][0]["schedule"]

        other = self._create("B-2")
        self.service.submit_batch(self.actor, [other])
        result = self.service.update_tide_windows(self.actor, NARROW)
        self.assertEqual(result["counts"]["locked"], 1)
        self.assertEqual(self.service.get_record(self.actor, rid)["state"], "berthed")
        # 已靠泊的拖轮/引航结果不变
        after = [p for p in self.service.plans(self.actor)["items"] if p["record"]["id"] == rid][0]["schedule"]
        self.assertEqual(after["status"], "berthed")
        self.assertEqual(after["tug_id"], berthed_assignment["tug_id"])

    def test_confirmed_unberthed_is_invalidated_and_reconfirmed(self):
        rid = self._create("I-1")
        self.service.submit_batch(self.actor, [rid])
        record = self.service.get_record(self.actor, rid)
        self.service.confirm_schedule(self.actor, rid, record["version"], {})
        self.service.update_tide_windows(self.actor, NARROW)
        self.assertEqual(self.service.get_record(self.actor, rid)["state"], "draft")
        actions = [e["action"] for e in self.service.timeline(self.actor, rid)]
        self.assertIn("reschedule", actions)
        # 窗口恢复后可再次确认
        self.service.update_tide_windows(self.actor, WIDE)
        record = self.service.get_record(self.actor, rid)
        self.assertEqual(record["state"], "draft")
        again = self.service.confirm_schedule(self.actor, rid, record["version"], {})
        self.assertEqual(again["state"], "confirmed")

    def test_never_scheduled_draft_is_not_touched(self):
        # 只创建未提交排班的草稿；潮汐变更不应为其生成排班
        rid = self.service.create(self.actor, "U-1", vessel_data("U-1"))["id"]
        result = self.service.update_tide_windows(self.actor, NARROW)
        self.assertEqual(result["counts"]["conflict"], 0)
        self.service.update_tide_windows(self.actor, WIDE)
        view = [p for p in self.service.plans(self.actor)["items"] if p["record"]["id"] == rid][0]
        self.assertIsNone(view["schedule"])

    def test_user_cancelled_plan_is_never_restored(self):
        rid = self._create("X-1")
        self.service.submit_batch(self.actor, [rid])
        record = self.service.get_record(self.actor, rid)
        self.service.act(self.actor, rid, record["version"], "cancel", {"cancel_reason": "撤单"})
        self.service.update_tide_windows(self.actor, NARROW)
        widened = self.service.update_tide_windows(self.actor, WIDE)
        self.assertEqual(widened["counts"]["restored"], 0)
        self.assertEqual(self.service.get_record(self.actor, rid)["state"], "cancelled")

    def test_plan_version_increments_and_audit_carries_counts(self):
        rid = self._create("V-1")
        self.service.submit_batch(self.actor, [rid])
        first = self.service.update_tide_windows(self.actor, NARROW)
        second = self.service.update_tide_windows(self.actor, WIDE)
        self.assertEqual(second["plan_version"], first["plan_version"] + 1)
        feed = self.service.system_feed(self.actor)["items"]
        tide_events = [e for e in feed if e["action"] == "tide_reschedule"]
        for event in tide_events:
            counts = event["details"]["counts"]
            self.assertIn("pending_confirmation", counts)
            self.assertIn("conflict", counts)
            self.assertIn("restored", counts)


class BatchFailureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.actor = Actor("duty", "port_controller")

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, ref):
        return self.service.create(self.actor, ref, vessel_data(ref))["id"]

    def test_preflight_resource_failure_rejects_all(self):
        rid = self._create("F-1")
        self.service.resources_provider.set_failure(1)
        with self.assertRaises(ServiceUnavailable):
            self.service.submit_batch(self.actor, [rid])

    def test_mid_batch_failure_preserves_accepted_and_retry_merges(self):
        n1 = self._create("F-2")
        n2 = self._create("F-3")
        # 第1次=预取成功，第2次=n1成功，第3次=n2失败
        self.service.resources_provider.fail_call(3)
        partial = self.service.submit_batch(self.actor, [n1, n2])
        self.assertEqual(partial["status"], "partial")
        self.assertEqual([i["record_id"] for i in partial["items"]], [n1])
        self.assertEqual(partial["failure"]["retry_record_ids"], [n2])

        retry = self.service.submit_batch(self.actor, [n1, n2])
        self.assertEqual(retry["status"], "accepted")
        self.assertEqual(retry["merged"], [n1])
        by_id = {i["record_id"]: i for i in retry["items"]}
        self.assertTrue(by_id[n1]["merged"])
        self.assertFalse(by_id[n2]["merged"])
        # 重试后同一批两个船不得共用同一拖轮同一时段
        if by_id[n1]["start_hour"] == by_id[n2]["start_hour"]:
            self.assertNotEqual(by_id[n1]["tug_id"], by_id[n2]["tug_id"])

    def test_batch_event_audit_has_counts(self):
        rid = self._create("F-4")
        self.service.submit_batch(self.actor, [rid])
        event = [e for e in self.service.system_feed(self.actor)["items"] if e["action"] == "batch_submit"][0]
        counts = event["details"]["counts"]
        self.assertIn("pending_confirmation", counts)
        self.assertIn("conflict", counts)
        self.assertIn("restored", counts)


class PlanViewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.actor = Actor("duty", "port_controller")

    def tearDown(self):
        self.temp.cleanup()

    def test_plans_list_and_stats_return_counts(self):
        rid = self.service.create(self.actor, "P-1", vessel_data("P-1"))["id"]
        self.service.submit_batch(self.actor, [rid])
        view = self.service.plans(self.actor)
        self.assertEqual(view["counts"]["pending_confirmation"], 1)
        self.assertEqual(view["counts"]["conflict"], 0)
        self.assertEqual(view["counts"]["restored"], 0)
        stats = self.service.stats(self.actor)
        self.assertEqual(stats["schedule_counts"]["pending_confirmation"], 1)
