"""潮汐排班规则与算法的单元测试。"""
import unittest

from src.scheduling import planner
from src.scheduling.types import BERTHED, INVALID, PENDING, SCHEDULED, ResourcePool
from src.scheduling.types import Allocation


WIDE = [0.0] * 4 + [12.0] * 16 + [0.0] * 4
RESOURCES = {"tugboats": ["T1", "T2"], "pilots": ["P1", "P2"], "berths": []}


def plan(plan_id, vessel="V", berth="B", start=6, hours=2, draft=10.0, state=PENDING, alloc=None):
    item = {
        "id": plan_id, "vessel": vessel, "berth": berth, "draft_m": draft,
        "vessel_length_m": 180, "start_hour": start, "service_hour": hours,
        "service_hours": hours, "state": state, "reason": "", "allocation": alloc,
    }
    return item


class TimelineRuleTest(unittest.TestCase):
    def test_same_tug_same_pilot_cannot_serve_two_vessels(self):
        results = planner.plan_batch(
            [plan(1, "V1", "B1"), plan(2, "V2", "B2")],
            [], WIDE, {"tugboats": ["T1"], "pilots": ["P1"]},
        )
        self.assertEqual([r["state"] for r in results], [SCHEDULED, SCHEDULED])
        self.assertEqual(results[0]["allocation"].start_hour, 6)
        # 同一拖轮、同一引航员在 6-8 被占用，V2 顺延到 8-10
        self.assertEqual(results[1]["allocation"].start_hour, 8)
        self.assertEqual(results[1]["allocation"].tugboat, "T1")
        self.assertEqual(results[1]["allocation"].pilot, "P1")

    def test_two_resource_pairs_run_in_parallel(self):
        results = planner.plan_batch(
            [plan(1, "V1", "B1"), plan(2, "V2", "B2")],
            [], WIDE, RESOURCES,
        )
        self.assertEqual([r["allocation"].start_hour for r in results], [6, 6])
        self.assertNotEqual(results[0]["allocation"].tugboat, results[1]["allocation"].tugboat)

    def test_same_berth_is_also_exclusive(self):
        results = planner.plan_batch(
            [plan(1, "V1", "BX"), plan(2, "V2", "BX")],
            [], WIDE, RESOURCES,
        )
        self.assertEqual(results[0]["allocation"].start_hour, 6)
        self.assertEqual(results[1]["allocation"].start_hour, 8)

    def test_capacity_shortage_queues_within_day(self):
        results = planner.plan_batch(
            [plan(i, "V%d" % i, "B%d" % i, hours=4) for i in range(1, 11)],
            [], WIDE, {"tugboats": ["T1"], "pilots": ["P1"]},
        )
        # 高水位 4-20（16h），单资源 4h 槽位仅 4 个（起始 6/10/14，按最早6时算只有3个）
        scheduled = [r for r in results if r["state"] == SCHEDULED]
        queued = [r for r in results if r["state"] == PENDING]
        self.assertTrue(queued)
        self.assertTrue(all(r["reason"] == planner.REASON_CAPACITY for r in queued))
        # 任意两分配的同资源时段不重叠
        used = []
        for r in scheduled:
            a = r["allocation"]
            self.assertFalse(any(a.tugboat == t and not (a.end_hour <= s or a.start_hour >= e)
                                 for t, s, e in used))
            used.append((a.tugboat, a.start_hour, a.end_hour))

    def test_whole_service_window_must_meet_tide(self):
        # 只有 9-11 两小时高水位，3 小时作业整段都无法满足 -> 失效
        narrow = [0.0] * 9 + [12.0] * 2 + [0.0] * 13
        results = planner.replan_all([plan(1, hours=3)], narrow, RESOURCES)
        self.assertEqual(results[0]["state"], INVALID)
        self.assertEqual(results[0]["reason"], planner.REASON_TIDE)


class ReplanTest(unittest.TestCase):
    def test_berthed_plan_is_frozen(self):
        frozen = Allocation(6, 8, "T1", "P1", 12.0).to_dict()
        existing = [plan(1, "V1", "B1", state=BERTHED, alloc=frozen), plan(2, "V2", "B2", hours=3)]
        # 即使新窗口只有 9-11 可航，已靠泊计划保持 6 时原结果；3h 计划无法容纳而失效
        narrow = [0.0] * 9 + [12.0] * 2 + [0.0] * 13
        results = planner.replan_all(existing, narrow, RESOURCES)
        by_id = {r["plan_id"]: r for r in results}
        self.assertNotIn(1, by_id)  # berthed 不参与重排，结果由存储层保留
        self.assertEqual(by_id[2]["state"], INVALID)

    def test_widening_recovers_only_system_invalid_plans(self):
        narrow = [0.0] * 9 + [12.0] * 2 + [0.0] * 13
        first = planner.replan_all([plan(1, hours=3)], narrow, RESOURCES)
        self.assertEqual(first[0]["state"], INVALID)
        wide = WIDE
        second = planner.replan_all(
            [plan(1, hours=3, state=INVALID)], wide, RESOURCES,
        )
        self.assertEqual(second[0]["state"], SCHEDULED)
        self.assertTrue(second[0]["recovered"])

    def test_still_valid_scheduled_plan_is_kept(self):
        alloc = Allocation(6, 8, "T1", "P1", 12.0).to_dict()
        results = planner.replan_all([plan(1, state=SCHEDULED, alloc=alloc)], WIDE, RESOURCES)
        self.assertTrue(results[0]["kept"])
        self.assertEqual(results[0]["allocation"].start_hour, 6)

    def test_shrunk_resource_catalog_displaces_plan(self):
        alloc = Allocation(6, 8, "T9", "P9", 12.0).to_dict()  # T9/P9 已下线
        results = planner.replan_all(
            [plan(1, state=SCHEDULED, alloc=alloc)], WIDE,
            {"tugboats": ["T1"], "pilots": ["P1"]},
        )
        self.assertFalse(results[0]["kept"])
        self.assertEqual(results[0]["state"], SCHEDULED)
        self.assertEqual(results[0]["allocation"].tugboat, "T1")


if __name__ == "__main__":
    unittest.main()
