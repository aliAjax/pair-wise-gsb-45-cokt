"""港口泊位与航道调度领域规则、状态转换与可恢复排班判定。"""
from typing import Any, Dict, List, Tuple

from .domain import Conflict, ValidationError, boolean, choice, integer, number, text


INITIAL_STATE = "draft"
CREATE_ROLES = {'port_controller'}
ACTION_ROLES = {
    'confirm': {'port_controller'},
    'confirm_schedule': {'port_controller'},
    'berth': {'port_controller'},
    'depart': {'port_controller'},
    'cancel': {'port_controller'},
}
TRANSITIONS = {
    'confirm': {'draft': 'confirmed'},
    'confirm_schedule': {'draft': 'confirmed'},
    'berth': {'confirmed': 'berthed'},
    'depart': {'berthed': 'departed'},
    'cancel': {'draft': 'cancelled', 'confirmed': 'cancelled'},
}

# 排班（assignment）状态：
# pending  -> 系统在计划窗口内排出槽位，等待值班员确认
# queued   -> 容量不足被迫排队（窗口外/延迟槽位），等待资源释放
# conflict -> 潮汐窗口内进水不足，无法排入
# active   -> 已确认，占用拖轮与引航员
# berthed  -> 已靠泊，结果锁定
SCHEDULE_STATES = {"pending", "queued", "conflict", "active", "berthed"}
PENDING_SCHEDULE_STATES = {"pending", "queued"}
TERMINAL_RECORD_STATES = {"cancelled", "departed"}

RESOURCE_KINDS = {"tug", "pilot"}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        vessel = text(p, "vessel")
        berth = text(p, "berth")
        vessel_length = number(p, "vessel_length_m", 1)
        berth_length = number(p, "berth_length_m", 1)
        draft = number(p, "draft_m", 0)
        berth_depth = number(p, "berth_depth_m", 0)
        eta = integer(p, "eta_hour", 0, 23)
        etd = integer(p, "etd_hour", 1, 24)
        choice(p, "risk_level", ["low", "medium", "high"])
        dangerous = boolean(p, "dangerous_goods")
        if etd <= eta:
            raise ValidationError("etd_hour必须晚于eta_hour")
        if berth_length < vessel_length:
            raise ValidationError("泊位长度不足")
        if berth_depth - draft < 0.5:
            raise ValidationError("剩余水深不足")
        if dangerous:
            text(p, "dangerous_class")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["safety_margin_m"] = round(float(p["berth_depth_m"]) - float(p["draft_m"]), 2)
        p["window_hours"] = int(p["etd_hour"]) - int(p["eta_hour"])
        p["quay_ok"] = bool(p["berth_length_m"] >= p["vessel_length_m"] and p["safety_margin_m"] >= 0.5)
        return p

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def validate_tide_windows(self, windows: Any) -> List[Dict[str, Any]]:
        """校验潮汐窗口：[{"start_hour":0,"end_hour":6,"water_level_m":12.0}, ...]。"""
        if not isinstance(windows, list) or not windows:
            raise ValidationError("tide_windows必须是非空列表")
        normalized: List[Dict[str, Any]] = []
        for raw in windows:
            if not isinstance(raw, dict):
                raise ValidationError("潮汐窗口必须是对象")
            start = integer(raw, "start_hour", 0, 23)
            end = integer(raw, "end_hour", 1, 24)
            level = number(raw, "water_level_m", 0)
            if end <= start:
                raise ValidationError("潮汐窗口end_hour必须晚于start_hour")
            normalized.append({"start_hour": start, "end_hour": end, "water_level_m": level})
        normalized.sort(key=lambda item: (item["start_hour"], item["end_hour"]))
        for prev, cur in zip(normalized, normalized[1:]):
            if cur["start_hour"] < prev["end_hour"]:
                raise ValidationError("潮汐窗口不能重叠")
        return normalized

    def tide_feasible_hours(self, windows: List[Dict[str, Any]], draft_m: float, channel_depth_m: float) -> List[int]:
        """返回进水足够的整点小时集合（水位与航道水深同时满足）。"""
        required = float(draft_m) + 0.5
        hours: List[int] = []
        for window in windows:
            if float(window["water_level_m"]) >= required and float(channel_depth_m) >= required:
                hours.extend(range(int(window["start_hour"]), int(window["end_hour"])))
        return hours

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action in ("confirm", "confirm_schedule"):
            pilot = text(data, "pilot_id")
            changes["pilot_id"] = pilot
            summary = "已确认引航员"
        elif action == "berth":
            actual = number(data, "actual_draft_m", 0)
            if float(p["berth_depth_m"]) - actual < 0.5:
                raise ValidationError("实际吃水导致水深不足")
            changes["actual_draft_m"] = actual
            summary = "船舶已靠泊"
        elif action == "depart":
            if not boolean(data, "cargo_operation_complete"):
                raise ValidationError("货物作业尚未完成")
            summary = "船舶已离泊"
        elif action == "cancel":
            changes["cancel_reason"] = text(data, "cancel_reason")
            summary = "计划已取消"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
