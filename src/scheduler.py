"""可恢复进港排班引擎：潮汐窗口 + 靠泊计划 + 拖轮/引航资源。

纯函数式计算，不触碰数据库，便于重试与重放：
- 同一拖轮、同一引航员、同一泊位在同一整点小时只服务一艘船；
- 容量不足时顺延排队；潮汐窗口内进水不足判为冲突。
"""
from typing import Any, Dict, List, Optional, Set, Tuple

from .rules import DomainRules


DEFAULT_SERVICE_HOURS = 1
HORIZON_END = 24


def _interval_set(start: int, end: int) -> Set[int]:
    return set(range(int(start), int(end)))


class Scheduler:
    def __init__(self, rules: DomainRules = None) -> None:
        self.rules = rules or DomainRules()

    def _base_occupancy(self, occupations: List[Dict[str, Any]]) -> Tuple[Set[Tuple[str, int]], Set[Tuple[str, str, int]]]:
        berth_occ: Set[Tuple[str, int]] = set()
        resource_occ: Set[Tuple[str, str, int]] = set()
        for item in occupations:
            hours = _interval_set(item["start_hour"], item["end_hour"])
            berth = item.get("berth")
            if berth:
                berth_occ.update((berth, hour) for hour in hours)
            tug = item.get("tug_id")
            if tug:
                resource_occ.update(("tug", tug, hour) for hour in hours)
            pilot = item.get("pilot_id")
            if pilot:
                resource_occ.update(("pilot", pilot, hour) for hour in hours)
        return berth_occ, resource_occ

    def _free_resource(self, kind: str, resources: List[Dict[str, Any]], used: Set[Tuple[str, str, int]], hours: Set[int]) -> Optional[str]:
        for resource in sorted((r for r in resources if r["kind"] == kind and r.get("active", True)), key=lambda r: r["resource_id"]):
            rid = resource["resource_id"]
            if all((kind, rid, hour) not in used for hour in hours):
                return rid
        return None

    def plan(
        self,
        plans: List[Dict[str, Any]],
        tide_windows: List[Dict[str, Any]],
        resources: List[Dict[str, Any]],
        occupations: List[Dict[str, Any]],
        channel_depth_m: float,
    ) -> Dict[int, Dict[str, Any]]:
        """按 ETA 先后为每个计划寻找最早可行槽位。

        返回 {record_id: {status,start_hour,end_hour,pilot_id,tug_id,reason}}。
        """
        berth_occ, resource_occ = self._base_occupancy(occupations)
        results: Dict[int, Dict[str, Any]] = {}
        ordered = sorted(plans, key=lambda r: (int(r["payload"]["eta_hour"]), int(r["id"])))
        for record in ordered:
            result = self._plan_one(record, tide_windows, resources, channel_depth_m, berth_occ, resource_occ)
            results[int(record["id"])] = result
            if result["status"] in ("pending", "queued"):
                hours = _interval_set(result["start_hour"], result["end_hour"])
                berth_occ.update((record["payload"]["berth"], hour) for hour in hours)
                resource_occ.update(("tug", result["tug_id"], hour) for hour in hours)
                resource_occ.update(("pilot", result["pilot_id"], hour) for hour in hours)
        return results

    def _plan_one(
        self,
        record: Dict[str, Any],
        tide_windows: List[Dict[str, Any]],
        resources: List[Dict[str, Any]],
        channel_depth_m: float,
        berth_occ: Set[Tuple[str, int]],
        resource_occ: Set[Tuple[str, str, int]],
    ) -> Dict[str, Any]:
        payload = record["payload"]
        draft = float(payload["draft_m"])
        berth = payload["berth"]
        eta = int(payload["eta_hour"])
        etd = int(payload["etd_hour"])
        service = int(payload.get("service_hours") or DEFAULT_SERVICE_HOURS)
        feasible_hours = set(self.rules.tide_feasible_hours(tide_windows, draft, channel_depth_m))

        tide_feasible_any = False
        earliest: Optional[Dict[str, Any]] = None
        for start in range(eta, HORIZON_END - service + 1):
            hours = _interval_set(start, start + service)
            if not hours <= feasible_hours:
                continue
            tide_feasible_any = True
            if any((berth, hour) in berth_occ for hour in hours):
                continue
            tug = self._free_resource("tug", resources, resource_occ, hours)
            if tug is None:
                continue
            pilot = self._free_resource("pilot", resources, resource_occ, hours)
            if pilot is None:
                continue
            earliest = {"start_hour": start, "end_hour": start + service, "tug_id": tug, "pilot_id": pilot}
            break

        if earliest is None:
            reason = "tide_window_unavailable" if not tide_feasible_any else "resource_capacity"
            return {"status": "conflict", "start_hour": None, "end_hour": None, "pilot_id": None, "tug_id": None, "reason": reason}
        status = "pending" if earliest["start_hour"] < etd else "queued"
        result = {"status": status, "reason": "" if status == "pending" else "queued_beyond_window"}
        result.update(earliest)
        return result
