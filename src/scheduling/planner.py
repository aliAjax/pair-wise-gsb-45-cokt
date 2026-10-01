"""潮汐窗口与拖轮/引航员互斥的排班算法。

规则：
- 船只需在进港时刻满足 tide_level >= draft + 0.5m（最低龙骨富余）。
- 同一拖轮、同一引航员、同一泊位在同一半开时段 [start, end) 只能服务一艘船。
- 容量不足则在当日内顺延等待；全天找不到可容纳槽位时排队（pending）。
- 有可航潮时但资源全天不足 -> pending（排队等容量）；
  新窗口完全没有可航潮时 -> invalid（系统重排失效，窗口放宽后可恢复）。
"""
from typing import Any, Dict, List, Optional, Tuple

from .types import (
    BERTHED,
    INVALID,
    PENDING,
    SCHEDULED,
    Allocation,
    ResourcePool,
)

REASON_TIDE = "tide_window_unavailable"
REASON_CAPACITY = "capacity_exceeded"


def tide_feasible_hours(plan: Dict[str, Any], tide_levels: List[float]) -> List[int]:
    """整个进港作业时段 [start, start+service_hours) 每个小时都必须满足龙骨富余。"""
    threshold = float(plan["draft_m"]) + 0.5
    latest = 24 - int(plan["service_hours"])
    earliest = int(plan["start_hour"])
    duration = int(plan["service_hours"])
    return [
        hour for hour in range(earliest, latest + 1)
        if all(tide_levels[hour + offset] >= threshold for offset in range(duration))
    ]


def attempt_allocate(
    plan: Dict[str, Any],
    tide_levels: List[float],
    pool: ResourcePool,
) -> Optional[Allocation]:
    """在潮汐可行小时中找最早的「潮时 + 拖轮 + 引航员 + 泊位」同时可用槽位。"""
    for hour in tide_feasible_hours(plan, tide_levels):
        end = hour + int(plan["service_hours"])
        found = pool.find(hour, end, plan["berth"])
        if found is not None:
            tugboat, pilot = found
            allocation = Allocation(
                start_hour=hour,
                end_hour=end,
                tugboat=tugboat,
                pilot=pilot,
                tide_level_m=tide_levels[hour],
            )
            pool.reserve(allocation, plan["berth"])
            return allocation
    return None


def _base_pool(resources: Dict[str, Any], plans: List[Dict[str, Any]],
               reserve_states: tuple = (SCHEDULED, BERTHED)) -> ResourcePool:
    # 全量重排时只冻结已靠泊占用；批次追加时还需保留既有 scheduled。
    return ResourcePool.from_snapshot(resources, plans, frozen_states=reserve_states)


def plan_batch(
    new_plans: List[Dict[str, Any]],
    existing_plans: List[Dict[str, Any]],
    tide_levels: List[float],
    resources: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """只对新受理的一批 pending 计划做分配，不动既有计划。"""
    pool = _base_pool(resources, existing_plans)
    ordered = sorted(new_plans, key=lambda p: (int(p["start_hour"]), int(p["id"])))
    results: List[Dict[str, Any]] = []
    for plan in ordered:
        allocation = attempt_allocate(plan, tide_levels, pool)
        if allocation is not None:
            results.append({"plan_id": plan["id"], "state": SCHEDULED, "allocation": allocation,
                            "reason": "", "recovered": False})
        else:
            feasible = tide_feasible_hours(plan, tide_levels)
            reason = REASON_CAPACITY if feasible else REASON_TIDE
            # 批次提交时窗口仍是当前版本，没有潮时记为失效，其余排队等容量。
            state = INVALID if not feasible else PENDING
            results.append({"plan_id": plan["id"], "state": state, "allocation": None,
                            "reason": reason, "recovered": False})
    return results


def replan_all(
    plans: List[Dict[str, Any]],
    tide_levels: List[float],
    resources: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """潮汐窗口变化或资源重试后的稳定重排。

    - berthed/cancelled：冻结，永不触碰；
    - scheduled 且原分配仍满足潮时/资源/容量：保持原结果不变；
    - 原分配不再可行的 scheduled 释放后回到候选集重排；
    - pending（容量排队）/invalid（系统失效）参与重排；
    - 窗口放宽时只有 invalid 重新获得分配才计 recovered。
    """
    pool = _base_pool(resources, plans, reserve_states=(BERTHED,))
    tug_ids = set(resources["tugboats"])
    pilot_ids = set(resources["pilots"])
    results: List[Dict[str, Any]] = []
    displaced: List[Dict[str, Any]] = []

    def allocation_still_valid(plan: Dict[str, Any]) -> bool:
        alloc = plan.get("allocation")
        if not alloc:
            return False
        if alloc["tugboat"] not in tug_ids or alloc["pilot"] not in pilot_ids:
            return False
        start, end = int(alloc["start_hour"]), int(alloc["end_hour"])
        threshold = float(plan["draft_m"]) + 0.5
        if any(tide_levels[start + offset] < threshold for offset in range(end - start)):
            return False
        if pool.find(start, end, plan["berth"]) is None:
            return False
        return True

    # 第一遍：稳定保留仍然可行的已排班计划，按 id 顺序占用。
    for plan in sorted(plans, key=lambda p: int(p["id"])):
        if plan["state"] == SCHEDULED:
            if allocation_still_valid(plan):
                allocation = Allocation.from_dict(plan["allocation"])
                pool.reserve(allocation, plan["berth"])
                results.append({"plan_id": plan["id"], "state": SCHEDULED, "allocation": allocation,
                                "reason": "", "recovered": False, "was": SCHEDULED, "kept": True})
            else:
                displaced.append(plan)
        elif plan["state"] in (PENDING, INVALID):
            displaced.append(plan)

    # 第二遍：失效/排队/被挤掉的计划按申请优先级确定性重排。
    for plan in sorted(displaced, key=lambda p: (int(p["start_hour"]), int(p["id"]))):
        previous = plan["state"]
        allocation = attempt_allocate(plan, tide_levels, pool)
        if allocation is not None:
            results.append({
                "plan_id": plan["id"],
                "state": SCHEDULED,
                "allocation": allocation,
                "reason": "",
                "recovered": previous == INVALID,
                "was": previous,
                "kept": False,
            })
            continue
        feasible = tide_feasible_hours(plan, tide_levels)
        if feasible:
            # 有可航潮时但拖轮/引航员容量不足 -> 排队等待，不判失效。
            results.append({"plan_id": plan["id"], "state": PENDING, "allocation": None,
                            "reason": REASON_CAPACITY, "recovered": False, "was": previous, "kept": False})
        else:
            # 当前窗口无法容纳 -> 系统置失效；已靠泊计划不会进入这里。
            results.append({"plan_id": plan["id"], "state": INVALID, "allocation": None,
                            "reason": REASON_TIDE, "recovered": False, "was": previous, "kept": False})
    return results


def summarize(results: List[Dict[str, Any]]) -> Dict[str, int]:
    return {
        "scheduled": sum(1 for r in results if r["state"] == SCHEDULED),
        "pending": sum(1 for r in results if r["state"] == PENDING),
        "invalid": sum(1 for r in results if r["state"] == INVALID),
        "recovered": sum(1 for r in results if r.get("recovered")),
        "invalidated": sum(1 for r in results if r["state"] == INVALID and r.get("was") == SCHEDULED),
    }
