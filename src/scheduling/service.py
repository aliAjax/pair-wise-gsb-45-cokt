"""排班用例编排：批次受理（幂等）、潮汐重算、资源重试、靠泊冻结与审计数量。

所有响应统一携带 counts：待确认(pending)、冲突(conflicts)、恢复(recovered)。
"""
from typing import Any, Dict, List, Optional

from ..domain import Actor, DomainError, PermissionDenied, ValidationError, text
from ..scheduling import planner
from ..scheduling.repository import ResourceUnavailable, SchedulingRepository
from ..scheduling.resources import ResourceProvider
from ..scheduling.types import (
    BERTHED,
    CANCELLED,
    INVALID,
    PENDING,
    SCHEDULED,
    validate_plan,
    validate_resources,
    validate_tide_window,
)

CONTROLLER_ROLES = {"port_controller", "admin"}


class ResourceReadFailed(DomainError):
    status = 503
    code = "resource_unavailable"


class SchedulingService:
    def __init__(self, repository: SchedulingRepository, provider: ResourceProvider = None) -> None:
        self.repository = repository
        self.provider = provider or ResourceProvider(repository)

    # ---- 通用 -----------------------------------------------------------
    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        if actor.role not in CONTROLLER_ROLES:
            raise PermissionDenied("角色无权操作进港排班")
        return actor

    def counts(self) -> Dict[str, int]:
        plans = self.repository.list_plans()
        return {
            "pending": sum(1 for p in plans if p["state"] == PENDING),
            "conflicts": sum(1 for p in plans if p["state"] == INVALID),
            "recovered": self.repository.recovered_total(),
            "scheduled": sum(1 for p in plans if p["state"] == SCHEDULED),
            "berthed": sum(1 for p in plans if p["state"] == BERTHED),
            "cancelled": sum(1 for p in plans if p["state"] == CANCELLED),
        }

    def _envelope(self, items: List[Dict[str, Any]], conflicts: List[Dict[str, Any]] = None,
                  operation: Dict[str, int] = None, note: str = "", **extra: Any) -> Dict[str, Any]:
        envelope = dict(extra)
        envelope.update({
            "items": items,
            "conflicts": conflicts or [],
            "operation": operation or {},
            "counts": self.counts(),
            "note": note,
        })
        return envelope

    def _fetch_resources(self) -> Optional[Dict[str, Any]]:
        try:
            return self.provider.fetch()
        except ResourceUnavailable:
            return None

    # ---- 批次提交（幂等） -----------------------------------------------
    def submit_batch(self, actor: Actor, body: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        batch_id = text(body, "batch_id")
        client_key = text(body, "client_key")

        # 幂等：重复提交同一 batch_id / client_key 不重新受理、不重复占用拖轮。
        existing = self.repository.get_batch(batch_id)
        if existing is not None:
            items = self.repository.batch_plans(batch_id)
            return self._envelope(
                items, note="批次已存在，返回已受理结果（幂等）",
                replay=True, batch_id=batch_id, status=existing["status"],
                operation={"accepted": 0, "scheduled": 0, "pending": len(items),
                           "conflicts": 0, "recovered": 0},
            )
        same_client = self._find_batch_by_client(client_key)
        if same_client is not None:
            items = self.repository.batch_plans(same_client["batch_id"])
            return self._envelope(
                items, conflicts=[{"reason": "duplicate_submission",
                                   "message": "client_key已提交过批次", "batch_id": same_client["batch_id"]}],
                note="重复提交已被拦截", replay=True, batch_id=same_client["batch_id"],
                status=same_client["status"],
                operation={"accepted": 0, "scheduled": 0, "pending": 0, "conflicts": 1, "recovered": 0},
            )

        raw_plans = body.get("plans")
        if not isinstance(raw_plans, list) or not raw_plans:
            raise ValidationError("plans必须是非空数组")

        parsed: List[Dict[str, Any]] = []
        conflicts: List[Dict[str, Any]] = []
        seen_in_batch: Dict[str, int] = {}
        for index, raw in enumerate(raw_plans):
            if not isinstance(raw, dict):
                raise ValidationError("plans[%s]必须是对象" % index)
            try:
                plan = validate_plan(raw)
            except ValidationError as exc:
                conflicts.append({"index": index, "vessel": raw.get("vessel", ""),
                                  "reason": "invalid_plan", "message": str(exc)})
                continue
            vessel = plan["vessel"]
            if vessel in seen_in_batch:
                conflicts.append({"index": index, "vessel": vessel,
                                  "reason": "duplicate_in_batch",
                                  "message": "同一批次内船舶重复提交"})
                continue
            if vessel in self.repository.active_vessels():
                conflicts.append({"index": index, "vessel": vessel,
                                  "reason": "vessel_already_active",
                                  "message": "该船已有未结束的进港计划，重复提交不会重复占用拖轮"})
                continue
            seen_in_batch[vessel] = index
            parsed.append(plan)

        if not parsed:
            return self._envelope(
                [], conflicts=conflicts, note="没有可受理的新计划",
                batch_id=batch_id, status="rejected",
                operation={"accepted": 0, "scheduled": 0, "pending": 0,
                           "conflicts": len(conflicts), "recovered": 0},
            )

        tide_version = int(self.repository.state()["tide_version"])
        items = self.repository.create_batch(batch_id, client_key, actor.user_id, parsed, tide_version)

        # 资源读取失败：已受理批次保留为 pending，稍后重试按计划版本合并。
        resources = self._fetch_resources()
        state = self.repository.state()
        note = ""
        if resources is None:
            note = "资源读取失败，已受理批次保留待重试"
            self.repository.add_audit("batch", batch_id, "resource_read_failed", actor.user_id,
                                      tide_version, int(state["plan_version"]),
                                      {"accepted": len(items), "reason": note})
            operation = {"accepted": len(items), "scheduled": 0, "pending": len(items),
                         "conflicts": len(conflicts), "recovered": 0}
            items = self.repository.batch_plans(batch_id)
            return self._envelope(items, conflicts=conflicts, operation=operation, note=note,
                                  batch_id=batch_id, status="waiting_resources")

        tide_levels = state["tide_levels"]
        if not tide_levels:
            note = "潮汐表尚未配置，计划保留待确认"
            operation = {"accepted": len(items), "scheduled": 0, "pending": len(items),
                         "conflicts": len(conflicts), "recovered": 0}
            return self._envelope(self.repository.batch_plans(batch_id), conflicts=conflicts,
                                  operation=operation, note=note, batch_id=batch_id,
                                  status="waiting_tide")

        operation = self._run_batch_planning(items, tide_levels, resources, actor.user_id)
        operation["accepted"] = len(items)
        operation["conflicts"] = len(conflicts)
        batch = self.repository.get_batch(batch_id)
        return self._envelope(self.repository.batch_plans(batch_id), conflicts=conflicts,
                              operation=operation, note=note, batch_id=batch_id,
                              status=batch["status"])

    def _run_batch_planning(self, new_plans: List[Dict[str, Any]], tide_levels: List[float],
                            resources: Dict[str, Any], actor_id: str) -> Dict[str, int]:
        snapshot = self.repository.begin_replan()
        try:
            results = planner.plan_batch(new_plans, snapshot["plans"], tide_levels, resources)
            merged = self.repository.apply_replan(snapshot, results, actor_id,
                                                  "batch_planned", int(snapshot["state"]["tide_version"]))
            self.repository.commit_replan(snapshot)
        except Exception:
            self.repository.rollback_replan(snapshot)
            raise
        return {"scheduled": merged["scheduled"], "pending": merged["pending"],
                "conflicts": merged["invalid"], "recovered": 0}

    def _find_batch_by_client(self, client_key: str) -> Optional[Dict[str, Any]]:
        return self.repository.find_batch_by_client(client_key)

    # ---- 潮汐窗口变更：未靠泊失效重算，已靠泊冻结，放宽只恢复系统失效项 ----
    def update_tide(self, actor: Actor, body: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        tide = validate_tide_window(body or {})
        state = self.repository.save_tide(tide["tide_levels"])
        tide_version = int(state["tide_version"])
        resources = self._fetch_resources()
        if resources is None:
            note = "潮汐窗口已更新，但资源读取失败；未靠泊计划待资源恢复后重算"
            self.repository.add_audit("system", "system", "tide_updated_resource_failed",
                                      actor.user_id, tide_version, int(state["plan_version"]),
                                      {"reason": note})
            return self._envelope(self.repository.list_plans(), operation={}, note=note,
                                  tide_version=tide_version)
        operation = self._run_full_replan(resources, actor.user_id, "tide_replanned")
        note = "潮汐窗口变更已按计划版本重排，已靠泊计划保持原结果"
        return self._envelope(self.repository.list_plans(), operation=operation, note=note,
                              tide_version=tide_version)

    def retry_planning(self, actor: Actor) -> Dict[str, Any]:
        """资源恢复后重试：全量重排，按计划版本合并，不会重复占用拖轮。"""
        actor = self._actor(actor)
        state = self.repository.state()
        if not state["tide_levels"]:
            raise ValidationError("潮汐表尚未配置，无法重排")
        resources = self._fetch_resources()
        if resources is None:
            raise ResourceReadFailed("资源读取仍然失败，已受理批次已保留，请稍后重试")
        operation = self._run_full_replan(resources, actor.user_id, "resource_retry_replanned")
        note = "资源恢复，已按最新计划版本合并重排"
        return self._envelope(self.repository.list_plans(), operation=operation, note=note)

    def _run_full_replan(self, resources: Dict[str, Any], actor_id: str, action: str) -> Dict[str, int]:
        snapshot = self.repository.begin_replan()
        try:
            tide_levels = snapshot["state"]["tide_levels"]
            tide_version = int(snapshot["state"]["tide_version"])
            results = planner.replan_all(snapshot["plans"], tide_levels, resources)
            merged = self.repository.apply_replan(snapshot, results, actor_id, action, tide_version)
            self.repository.commit_replan(snapshot)
        except Exception:
            self.repository.rollback_replan(snapshot)
            raise
        return {
            "scheduled": merged["scheduled"],
            "pending": merged["pending"],
            "conflicts": merged["invalid"],
            "invalidated": merged["invalid"] - merged["recovered"],
            "recovered": merged["recovered"],
            "merged": merged["merged"],
            "skipped": merged["skipped"],
        }

    # ---- 资源目录维护 ----------------------------------------------------
    def update_resources(self, actor: Actor, body: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        resources = validate_resources(body or {})
        self.repository.save_resources(resources)
        state = self.repository.state()
        operation: Dict[str, int] = {}
        note = "拖轮/引航员目录已更新"
        if state["tide_levels"]:
            operation = self._run_full_replan(resources, actor.user_id, "resources_updated_replanned")
        return self._envelope(self.repository.list_plans(), operation=operation, note=note)

    # ---- 靠泊：结果冻结，后续潮汐重算不再触碰 ------------------------------
    def berth_plan(self, actor: Actor, plan_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        plan = self.repository.get_plan(plan_id)
        if plan["state"] != SCHEDULED:
            raise ValidationError("只有已排班(scheduled)的计划可以靠泊")
        if "actual_draft_m" not in (body or {}):
            actual = float(plan["draft_m"])
        else:
            raw_draft = body["actual_draft_m"]
            if isinstance(raw_draft, bool) or not isinstance(raw_draft, (int, float)):
                raise ValidationError("actual_draft_m必须是数字")
            actual = float(raw_draft)
        allocation = plan["allocation"] or {}
        if float(allocation.get("tide_level_m", 0.0)) - actual < 0.5:
            raise ValidationError("进港时刻实际潮位不足以保证龙骨富余")
        updated = self.repository.mark_berthed(plan_id, actor.user_id, actual)
        return self._envelope([updated], note="已靠泊，排班结果冻结，潮汐变更不再重算该计划",
                              batch_id=updated["batch_id"])

    def cancel_plan(self, actor: Actor, plan_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        reason = text(body or {}, "cancel_reason")
        updated = self.repository.cancel_plan(plan_id, actor.user_id, reason)
        # 取消释放容量后，排队计划顺次补位（用户取消的计划永不自动恢复）。
        resources = self._fetch_resources()
        operation: Dict[str, int] = {}
        note = "计划已取消，资源已释放"
        if resources is not None and self.repository.state()["tide_levels"]:
            operation = self._run_full_replan(resources, actor.user_id, "cancel_replanned")
        return self._envelope([updated], operation=operation, note=note,
                              batch_id=updated["batch_id"])

    # ---- 查询 ------------------------------------------------------------
    def list_plans(self, actor: Actor, state: Optional[str] = None,
                   batch_id: Optional[str] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        if state and state not in (PENDING, SCHEDULED, BERTHED, INVALID, CANCELLED):
            raise ValidationError("未知的计划状态")
        plans = self.repository.list_plans(state=state, batch_id=batch_id)
        return self._envelope(plans)

    def get_plan(self, actor: Actor, plan_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        return self._envelope([self.repository.get_plan(plan_id)])

    def get_batch(self, actor: Actor, batch_id: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        batch = self.repository.get_batch(text({"batch_id": batch_id}, "batch_id"))
        if batch is None:
            from ..domain import NotFound
            raise NotFound("批次不存在")
        return self._envelope(self.repository.batch_plans(batch_id),
                              batch_id=batch_id, status=batch["status"])

    def audit(self, actor: Actor, scope: Optional[str] = None,
              ref: Optional[str] = None, limit: int = 200) -> Dict[str, Any]:
        actor = self._actor(actor)
        items = self.repository.audit(scope=scope, ref=ref, limit=limit)
        return {"items": items, "counts": self.counts()}
