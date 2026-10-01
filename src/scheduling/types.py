"""排班子系统的领域类型与校验。"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..domain import ValidationError, integer, number, text, text_list


# 计划状态：
# pending   已受理，尚未完成资源分配（排队中，或资源读取失败后待重试）
# scheduled 已分配潮汐时刻、拖轮与引航员
# berthed   已靠泊，资源结果冻结，任何重算都不再触碰
# invalid   上一版系统重排时，因新潮汐窗口无法容纳而失效（窗口放宽后可恢复）
# cancelled 用户主动取消，永不自动恢复
PENDING = "pending"
SCHEDULED = "scheduled"
BERTHED = "berthed"
INVALID = "invalid"
CANCELLED = "cancelled"

ACTIVE_STATES = (PENDING, SCHEDULED)
FROZEN_STATES = (BERTHED, CANCELLED)

DEFAULT_SERVICE_HOURS = 2
MIN_KEEL_CLEARANCE = 0.5


class SchedulingError(ValidationError):
    code = "scheduling_error"


def tide_levels(data: Dict[str, Any], key: str = "tide_levels", hours: int = 24) -> List[float]:
    """解析按小时给出的潮位表，返回长度为 hours 的水位序列（米）。"""
    value = data.get(key)
    if not isinstance(value, list) or not value:
        raise ValidationError("%s必须是非空数组" % key)
    if len(value) != hours:
        raise ValidationError("%s长度必须为%s（按小时给出）" % (key, hours))
    levels: List[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValidationError("%s必须全部为数字" % key)
        levels.append(round(float(item), 3))
    return levels


def validate_tide_window(payload: Dict[str, Any]) -> Dict[str, Any]:
    levels = tide_levels(payload)
    return {"tide_levels": levels}


def validate_plan(payload: Dict[str, Any]) -> Dict[str, Any]:
    """校验单船靠泊申请。作业时段为 [start_hour, start_hour+service_hours)。"""
    vessel = text(payload, "vessel")
    berth = text(payload, "berth")
    draft = number(payload, "draft_m", 0)
    length = number(payload, "vessel_length_m", 1)
    start = integer(payload, "start_hour", 0, 23)
    service_hours = integer(payload, "service_hours", 1, 24)
    if start + service_hours > 24:
        raise ValidationError("作业时段不能超过当日24时")
    return {
        "vessel": vessel,
        "berth": berth,
        "draft_m": round(float(draft), 3),
        "vessel_length_m": round(float(length), 3),
        "start_hour": start,
        "service_hours": service_hours,
    }


def validate_resources(payload: Dict[str, Any]) -> Dict[str, Any]:
    tugboats = text_list(payload, "tugboats", minimum=1)
    pilots = text_list(payload, "pilots", minimum=1)
    if len(set(tugboats)) != len(tugboats):
        raise ValidationError("拖轮编号不能重复")
    if len(set(pilots)) != len(pilots):
        raise ValidationError("引航员编号不能重复")
    return {"tugboats": tugboats, "pilots": pilots}


@dataclass
class Allocation:
    """一次成功的排班结果：进港时刻 + 占用的拖轮与引航员。"""
    start_hour: int
    end_hour: int
    tugboat: str
    pilot: str
    tide_level_m: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "start_hour": self.start_hour,
            "end_hour": self.end_hour,
            "tugboat": self.tugboat,
            "pilot": self.pilot,
            "tide_level_m": self.tide_level_m,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Allocation":
        return cls(
            start_hour=int(data["start_hour"]),
            end_hour=int(data["end_hour"]),
            tugboat=str(data["tugboat"]),
            pilot=str(data["pilot"]),
            tide_level_m=float(data.get("tide_level_m", 0.0)),
        )


@dataclass
class ResourceTimeline:
    """单类资源（拖轮或引航员）在一天内的占用时间线。"""
    name: str
    busy: List[tuple] = field(default_factory=list)

    def available(self, start: int, end: int) -> bool:
        return all(end <= b_start or start >= b_end for b_start, b_end in self.busy)

    def reserve(self, start: int, end: int) -> None:
        self.busy.append((start, end))
        self.busy.sort()


class ResourcePool:
    """从当前排班结果重建的拖轮/引航员/泊位互斥池。"""

    def __init__(self, tugboats: List[str], pilots: List[str], berths: List[str] = None) -> None:
        self._tugs = {name: ResourceTimeline(name) for name in tugboats}
        self._pilots = {name: ResourceTimeline(name) for name in pilots}
        # 泊位不一定有全局注册表，按需懒创建时间线。
        self._berths = {name: ResourceTimeline(name) for name in (berths or [])}

    def _berth(self, name: str) -> ResourceTimeline:
        line = self._berths.get(name)
        if line is None:
            line = ResourceTimeline(name)
            self._berths[name] = line
        return line

    def reserve(self, allocation: Allocation, berth: str = None) -> None:
        # 资源目录可能缩减：持有已下线拖轮/引航员的冻结占用直接忽略，
        # 对应的 scheduled 计划会进入候选重排，被重新分配现存资源。
        tug = self._tugs.get(allocation.tugboat)
        if tug is not None:
            tug.reserve(allocation.start_hour, allocation.end_hour)
        pilot = self._pilots.get(allocation.pilot)
        if pilot is not None:
            pilot.reserve(allocation.start_hour, allocation.end_hour)
        if berth:
            self._berth(berth).reserve(allocation.start_hour, allocation.end_hour)

    def find(self, start: int, end: int, berth: str = None) -> Optional[tuple]:
        """返回同一时段同时空闲的 (拖轮, 引航员, 泊位) 容量，不足时返回 None。"""
        if berth is not None and not self._berth(berth).available(start, end):
            return None
        free_tug = next((name for name, line in self._tugs.items() if line.available(start, end)), None)
        if free_tug is None:
            return None
        free_pilot = next((name for name, line in self._pilots.items() if line.available(start, end)), None)
        if free_pilot is None:
            return None
        return free_tug, free_pilot

    @classmethod
    def from_snapshot(
        cls,
        resources: Dict[str, Any],
        plans: List[Dict[str, Any]],
        frozen_states: tuple = (SCHEDULED,),
    ) -> "ResourcePool":
        pool = cls(resources.get("tugboats", []), resources.get("pilots", []), resources.get("berths", []))
        for plan in plans:
            if plan["state"] in frozen_states and plan.get("allocation"):
                pool.reserve(Allocation.from_dict(plan["allocation"]), plan.get("berth"))
        return pool
