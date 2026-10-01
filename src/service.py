"""业务用例编排：权限、乐观并发、审计与可恢复进港排班。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, ServiceUnavailable, ValidationError, text
from .repository import Repository, _now
from .rules import DomainRules, PENDING_SCHEDULE_STATES, RESOURCE_KINDS
from .scheduler import Scheduler


DEFAULT_TIDE_WINDOWS = [
    {"start_hour": 0, "end_hour": 6, "water_level_m": 12.0},
    {"start_hour": 12, "end_hour": 18, "water_level_m": 12.0},
]
DEFAULT_RESOURCES = [
    ("TUG-01", "tug", "拖轮1号", True),
    ("TUG-02", "tug", "拖轮2号", True),
    ("PILOT-01", "pilot", "引航员甲", True),
    ("PILOT-02", "pilot", "引航员乙", True),
]


class ResourceProvider:
    """拖轮/引航资源读取。可注入失败以模拟外部资源系统不可读。"""

    def __init__(self, repository: Repository) -> None:
        self.repository = repository
        self.fail_next = 0
        self.fail_total = 0
        self.calls = 0
        self.fail_at_calls: set = set()

    def set_failure(self, fail_next: int = 1) -> None:
        self.fail_next = max(int(fail_next), 0)

    def fail_call(self, *indices: int) -> None:
        """标记第 N 次（从1计）读取失败，便于模拟批次处理中途不可读。"""
        self.fail_at_calls = {int(i) for i in indices}

    def read_resources(self) -> List[Dict[str, Any]]:
        self.calls += 1
        if self.fail_next > 0 or self.calls in self.fail_at_calls:
            self.fail_next = max(self.fail_next - 1, 0)
            self.fail_total += 1
            raise ServiceUnavailable("拖轮/引航资源读取失败，请稍后重试")
        return self.repository.list_resources(active_only=True)


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        self.scheduler = Scheduler(rules)
        self.resources_provider = ResourceProvider(repository)
        self._seed_defaults()

    def _seed_defaults(self) -> None:
        if self.repository.get_meta("seeded"):
            return
        self.repository.set_meta("channel_depth_m", 12.0)
        self.repository.set_meta("service_hours", 1)
        self.repository.set_meta("plan_version", 0)
        now = _now()
        with self.repository._connect() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO resources(resource_id,kind,name,active,created_at,updated_at) VALUES(?,?,?,1,?,?)",
                [(rid, kind, name, now, now) for rid, kind, name, _ in DEFAULT_RESOURCES],
            )
            connection.executemany(
                "INSERT INTO tide_windows(start_hour,end_hour,water_level_m,plan_version,created_at) VALUES(?,?,?,0,?)",
                [(w["start_hour"], w["end_hour"], w["water_level_m"], now) for w in DEFAULT_TIDE_WINDOWS],
            )
            connection.execute(
                "INSERT INTO meta(key,value) VALUES('seeded', 'true') ON CONFLICT(key) DO NOTHING"
            )

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def _settings(self) -> Dict[str, Any]:
        return self.repository.settings()

    def _occupations(self) -> List[Dict[str, Any]]:
        """当前占用：active/berthed/pending/queued 的活跃排班（重算时由调用方剔除重算目标）。"""
        occupations = []
        for assignment in self.repository.active_assignments():
            if assignment["start_hour"] is None:
                continue
            occupations.append(
                {
                    "record_id": assignment["record_id"],
                    "berth": assignment["record_payload"].get("berth"),
                    "start_hour": assignment["start_hour"],
                    "end_hour": assignment["end_hour"],
                    "tug_id": assignment["tug_id"],
                    "pilot_id": assignment["pilot_id"],
                }
            )
        return occupations

    @staticmethod
    def _count_schedule_results(results: Dict[int, Dict[str, Any]]) -> Dict[str, int]:
        counts = {"pending_confirmation": 0, "queued": 0, "conflict": 0, "restored": 0}
        for result in results.values():
            if result["status"] in PENDING_SCHEDULE_STATES:
                counts["pending_confirmation"] += 1
            if result["status"] == "queued":
                counts["queued"] += 1
            if result["status"] == "conflict":
                counts["conflict"] += 1
        return counts

    # ------------------------------------------------------------- records
    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        # 泊位时间窗冲突不再在创建阶段硬拒：容量不足由排班引擎顺延排队。
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def plans(self, actor: Actor, state: Optional[str] = None, limit: int = 200) -> Dict[str, Any]:
        """靠泊计划列表：每条记录附带最近排班，并返回待确认/冲突/恢复/排队数量。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.plan_view(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    # ------------------------------------------------------------ tide window
    def list_tide_windows(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return {"items": self.repository.list_tide_windows(), "plan_version": self._settings()["plan_version"]}

    def update_tide_windows(self, actor: Actor, windows: Any) -> Dict[str, Any]:
        """潮汐窗口变更：未靠泊计划失效重算，已靠泊保持原结果。

        窗口改宽后只恢复系统重排（system_managed）的计划；用户取消的不恢复。
        """
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        normalized = self.rules.validate_tide_windows(windows)
        # 资源系统不可读：整批拒绝，旧版本计划保持，调用方可安全重试。
        resources = self.resources_provider.read_resources()

        settings = self._settings()
        new_version = settings["plan_version"] + 1
        channel_depth = settings["channel_depth_m"]

        latest = self.repository.latest_assignment_map()
        all_records = self.repository.list_records(limit=500)
        targets: List[Dict[str, Any]] = []
        invalidated: List[int] = []
        restored: List[int] = []
        locked: List[int] = []
        for record in all_records:
            state = record["state"]
            assignment = latest.get(int(record["id"]))
            if state == "berthed":
                if assignment is not None:
                    locked.append(int(record["id"]))
                continue
            if state in ("cancelled", "departed"):
                continue
            # 从未进入排班流程（没有任何排班记录）的草稿不在潮汐重算范围内。
            if assignment is None:
                continue
            if state == "confirmed":
                invalidated.append(int(record["id"]))
            targets.append(record)
            # 窗口改宽恢复判定：上一版系统重排且处于冲突的计划，本次重排可行即恢复。
            if assignment["system_managed"] and assignment["status"] == "conflict":
                restored.append(int(record["id"]))

        plans = []
        for record in targets:
            payload = dict(record["payload"])
            payload.setdefault("service_hours", settings["service_hours"])
            plans.append({"id": int(record["id"]), "state": record["state"], "payload": payload})

        # 已靠泊的锁定结果继续占用拖轮/引航；未靠泊目标全部重排，故剔除其旧占用。
        target_ids = {int(t["id"]) for t in plans}
        occupations = [occ for occ in self._occupations() if occ["record_id"] not in target_ids]
        results = self.scheduler.plan(plans, normalized, resources, occupations, channel_depth)

        # 恢复仅指真正从冲突变为可排入（pending/queued）。
        restored = [rid for rid in restored if results[rid]["status"] in PENDING_SCHEDULE_STATES]

        counts = self._count_schedule_results(results)
        counts["restored"] = len(restored)
        counts["locked"] = len(locked)
        persisted = self.repository.persist_reschedule(
            actor_id=actor.user_id,
            windows=normalized,
            new_version=new_version,
            targets=plans,
            results=results,
            invalidated_record_ids=invalidated,
            restored_ids=restored,
            counts=counts,
        )
        persisted["items"] = [
            {
                "record_id": rid,
                "status": results[rid]["status"],
                "start_hour": results[rid].get("start_hour"),
                "end_hour": results[rid].get("end_hour"),
                "pilot_id": results[rid].get("pilot_id"),
                "tug_id": results[rid].get("tug_id"),
                "restored": rid in restored,
            }
            for rid in sorted(results)
        ]
        return persisted

    # -------------------------------------------------------------- resources
    def list_resources(self, actor: Actor, kind: Optional[str] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if kind and kind not in RESOURCE_KINDS:
            raise ValidationError("kind只能是tug/pilot")
        return {"items": self.repository.list_resources(kind=kind)}

    def upsert_resource(self, actor: Actor, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        resource_id = text(data or {}, "resource_id")
        kind = text(data, "kind")
        if kind not in RESOURCE_KINDS:
            raise ValidationError("kind只能是tug/pilot")
        name = data.get("name", "")
        if not isinstance(name, str):
            raise ValidationError("name必须是文本")
        active = data.get("active", True)
        if not isinstance(active, bool):
            raise ValidationError("active必须是布尔值")
        self.repository.upsert_resource(resource_id, kind, name.strip(), active)
        return {"resource_id": resource_id, "kind": kind, "name": name.strip(), "active": active}

    def resource_health(self, actor: Actor) -> Dict[str, Any]:
        """主动读取一次资源系统，验证是否可读。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        items = self.resources_provider.read_resources()
        return {"readable": True, "total": len(items)}

    # ------------------------------------------------------------------ batch
    def submit_batch(self, actor: Actor, record_ids: Any) -> Dict[str, Any]:
        """批次提交进港排班。

        - 重复提交不重复占用拖轮：已有活跃排班的计划按原结果合并；
        - 资源读取失败：保留已受理批次，未处理条目可原样重试，按计划版本合并。
        """
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not isinstance(record_ids, list) or not record_ids:
            raise ValidationError("record_ids必须是非空列表")
        ids: List[int] = []
        seen = set()
        for raw in record_ids:
            if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
                raise ValidationError("record_ids必须是正整数列表")
            if raw in seen:
                continue
            seen.add(raw)
            ids.append(raw)

        settings = self._settings()
        plan_version = settings["plan_version"]
        windows = self.repository.list_tide_windows()
        resources = self.resources_provider.read_resources()

        # 预取并校验全部目标记录（事务前失败不留下任何批次）。
        records_by_id = {rid: self.repository.get(rid) for rid in ids}
        for rid in ids:
            if records_by_id[rid]["state"] in ("cancelled", "departed", "berthed"):
                raise ValidationError("记录%s当前状态不能提交排班" % rid)
        latest = self.repository.latest_assignment_map()

        connection = self.repository.begin_batch_transaction()
        now = _now()
        batch_id = self.repository.create_batch(connection, actor.user_id, plan_version, "accepted", now)
        accepted: List[Dict[str, Any]] = []
        merged_ids: List[int] = []
        failed: Optional[Dict[str, Any]] = None
        try:
            for index, rid in enumerate(ids):
                record = records_by_id[rid]
                assignment = latest.get(rid)
                if assignment is not None and not assignment["superseded"] and assignment["status"] in PENDING_SCHEDULE_STATES | {"active"}:
                    # 同计划版本直接合并；旧版本（潮汐已变）需要在新窗口重排。
                    if int(assignment["plan_version"]) >= plan_version:
                        self.repository.add_batch_item(connection, batch_id, rid, True)
                        merged_ids.append(rid)
                        accepted.append(
                            {
                                "record_id": rid,
                                "status": assignment["status"],
                                "start_hour": assignment["start_hour"],
                                "end_hour": assignment["end_hour"],
                                "pilot_id": assignment["pilot_id"],
                                "tug_id": assignment["tug_id"],
                                "merged": True,
                            }
                        )
                        continue

                # 新条目：读取资源（可能中途失败）并在当前快照上排队。
                try:
                    resources = self.resources_provider.read_resources()
                except ServiceUnavailable as exc:
                    failed = {
                        "code": exc.code,
                        "message": str(exc),
                        "at_record_id": rid,
                        # 已受理的无需重试；从当前条目起未处理的原样重放即可幂等合并。
                        "retry_record_ids": [r for r in ids[index:] if r not in merged_ids],
                    }
                    break

                payload = dict(record["payload"])
                payload.setdefault("service_hours", settings["service_hours"])
                plan = [{"id": rid, "state": record["state"], "payload": payload}]
                # 占用 = 既有活跃排班 + 本批已受理（含合并项，冲突项无槽位不占资源）。
                handled_ids = {item["record_id"] for item in accepted}
                occupations = [occ for occ in self._occupations() if occ["record_id"] not in handled_ids]
                for item in accepted:
                    if item["start_hour"] is None:
                        continue
                    occ_record = records_by_id.get(item["record_id"])
                    occupations.append(
                        {
                            "record_id": item["record_id"],
                            "berth": occ_record["payload"]["berth"] if occ_record else None,
                            "start_hour": item["start_hour"],
                            "end_hour": item["end_hour"],
                            "tug_id": item["tug_id"],
                            "pilot_id": item["pilot_id"],
                        }
                    )
                result = self.scheduler.plan(plan, windows, resources, occupations, settings["channel_depth_m"])[rid]
                saved = self.repository.accept_batch_item(connection, rid, batch_id, plan_version, result, False, now)
                accepted.append(
                    {
                        "record_id": rid,
                        "status": saved["status"],
                        "start_hour": saved["start_hour"],
                        "end_hour": saved["end_hour"],
                        "pilot_id": saved["pilot_id"],
                        "tug_id": saved["tug_id"],
                        "merged": False,
                    }
                )

            status = "partial" if failed else "accepted"
            connection.execute("UPDATE batches SET status=?,updated_at=? WHERE id=?", (status, now, batch_id))
            counts = {
                "pending_confirmation": sum(1 for a in accepted if a["status"] in PENDING_SCHEDULE_STATES),
                "queued": sum(1 for a in accepted if a["status"] == "queued"),
                "conflict": sum(1 for a in accepted if a["status"] == "conflict"),
                "restored": 0,
                "merged": len(merged_ids),
                "accepted": len(accepted),
            }
            self.repository.add_system_event(
                connection,
                "batch_submit",
                actor.user_id,
                plan_version,
                {"batch_id": batch_id, "counts": counts, "failed": failed},
                now,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {
            "batch_id": batch_id,
            "plan_version": plan_version,
            "status": status,
            "items": accepted,
            "merged": merged_ids,
            "counts": counts,
            "failure": failed,
        }

    # ------------------------------------------------------------- confirm etc
    def confirm_schedule(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """值班员确认系统排班：锁定拖轮/引航员，draft -> confirmed。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        self.rules.require_transition(record, "confirm_schedule")
        latest = self.repository.latest_assignment_map().get(record_id)
        if latest is None or latest["superseded"] or latest["status"] not in PENDING_SCHEDULE_STATES:
            raise ValidationError("没有待确认的排班，请先提交排班或等待重算")
        data = dict(data or {})
        confirm_data = {"pilot_id": latest["pilot_id"]}
        if "pilot_id" in data and str(data["pilot_id"]).strip() != str(latest["pilot_id"]):
            raise ValidationError("引航员与系统排班不一致")
        new_state, new_payload, summary = self.rules.apply_action(record, "confirm_schedule", confirm_data)
        return self.repository.mutate_with_assignment(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action="confirm_schedule",
            details={"summary": summary, "assignment_id": latest["id"], "from": record["state"], "to": new_state},
            assignment_status="active",
        )

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        assignment_status = None
        supersede = False
        if action == "confirm":
            # 手动确认：若存在待确认的系统排班，引航员必须一致，并把排班转为 active。
            latest = self.repository.latest_assignment_map().get(record_id)
            if latest is not None and not latest["superseded"] and latest["status"] in PENDING_SCHEDULE_STATES:
                if str(new_payload.get("pilot_id")) != str(latest["pilot_id"]):
                    raise ValidationError("引航员与系统排班不一致，请使用confirm_schedule")
                assignment_status = "active"
        elif action == "berth":
            assignment_status = "berthed"
        elif action in ("cancel", "depart"):
            # 取消或离泊后释放拖轮/引航员，排班不再参与占用。
            supersede = True
        return self.repository.mutate_with_assignment(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
            assignment_status=assignment_status,
            supersede=supersede,
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def system_feed(self, actor: Actor, limit: int = 100) -> Dict[str, Any]:
        """系统级审计：潮汐重算/批次提交，每条带待确认、冲突、恢复数量。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return {"items": self.repository.system_events(limit=limit)}

    def stats(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        data = self.repository.stats()
        data["schedule_counts"] = self.repository.plan_view(limit=500)["counts"]
        return data
