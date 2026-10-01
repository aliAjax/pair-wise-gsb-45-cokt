"""排班子系统的 SQLite 持久化：潮汐表、批次、计划与审计。

所有重排在单个 BEGIN IMMEDIATE 事务内按计划版本合并，
已靠泊/已取消或在快照后被更新版本处理过的计划不会被旧结果覆盖，
因此重试或并发重提不会重复占用拖轮。
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ResourceUnavailable(Exception):
    """拖轮/引航员资源表读取失败（上游潮汐/资源服务不可用）。"""


class SchedulingRepository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sched_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    tide_version INTEGER NOT NULL DEFAULT 0,
                    tide_levels TEXT,
                    resources TEXT,
                    plan_version INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sched_batches (
                    batch_id TEXT PRIMARY KEY,
                    client_key TEXT,
                    status TEXT NOT NULL,
                    plan_version INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sched_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL REFERENCES sched_batches(batch_id),
                    vessel TEXT NOT NULL,
                    berth TEXT NOT NULL,
                    draft_m REAL NOT NULL,
                    vessel_length_m REAL NOT NULL,
                    start_hour INTEGER NOT NULL,
                    service_hours INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    allocation TEXT,
                    tide_version INTEGER NOT NULL DEFAULT 0,
                    plan_version INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sched_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL,
                    ref TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    tide_version INTEGER NOT NULL DEFAULT 0,
                    plan_version INTEGER NOT NULL DEFAULT 0,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_active_vessel
                    ON sched_plans(vessel) WHERE state != 'cancelled';
                CREATE INDEX IF NOT EXISTS idx_plans_state ON sched_plans(state);
                CREATE INDEX IF NOT EXISTS idx_plans_batch ON sched_plans(batch_id);
                CREATE INDEX IF NOT EXISTS idx_sched_audit_ref ON sched_audit(scope, ref, id);
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO sched_state(id, tide_version, tide_levels, resources, plan_version, updated_at)"
                " VALUES (1, 0, NULL, NULL, 0, ?)",
                (_now(),),
            )

    # ---- 基础编解码 -----------------------------------------------------
    @staticmethod
    def _plan_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["allocation"] = json.loads(item["allocation"]) if item["allocation"] else None
        return item

    def state(self) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM sched_state WHERE id=1").fetchone()
        data = dict(row)
        data["tide_levels"] = json.loads(data["tide_levels"]) if data["tide_levels"] else None
        data["resources"] = json.loads(data["resources"]) if data["resources"] else None
        return data

    # ---- 潮汐与资源 -----------------------------------------------------
    def save_tide(self, tide_levels: List[float]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT tide_version FROM sched_state WHERE id=1").fetchone()
            new_version = int(row["tide_version"]) + 1
            connection.execute(
                "UPDATE sched_state SET tide_levels=?, tide_version=?, updated_at=? WHERE id=1",
                (json.dumps(tide_levels), new_version, now),
            )
            connection.commit()
        return self.state()

    def save_resources(self, resources: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE sched_state SET resources=?, updated_at=? WHERE id=1",
                (json.dumps(resources, ensure_ascii=False), now),
            )
        return self.state()

    # ---- 批次 -----------------------------------------------------------
    def create_batch(self, batch_id: str, client_key: str, actor_id: str, plans: List[Dict[str, Any]],
                     tide_version: int) -> List[Dict[str, Any]]:
        now = _now()
        with self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO sched_batches(batch_id,client_key,status,plan_version,created_by,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (batch_id, client_key, "accepted", 0, actor_id, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("batch_id已存在") from exc
            for plan in plans:
                connection.execute(
                    "INSERT INTO sched_plans(batch_id,vessel,berth,draft_m,vessel_length_m,start_hour,service_hours,"
                    "state,reason,allocation,tide_version,plan_version,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (batch_id, plan["vessel"], plan["berth"], plan["draft_m"], plan["vessel_length_m"],
                     plan["start_hour"], plan["service_hours"], "pending", "", None, tide_version, 0, now, now),
                )
            connection.execute(
                "INSERT INTO sched_audit(scope,ref,action,actor_id,tide_version,plan_version,details,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                ("batch", batch_id, "batch_accepted", actor_id, tide_version, 0,
                 json.dumps({"plans": len(plans)}, ensure_ascii=False), now),
            )
            rows = connection.execute("SELECT * FROM sched_plans WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
            connection.commit()
        return [self._plan_row(row) for row in rows]

    def get_batch(self, batch_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM sched_batches WHERE batch_id=?", (batch_id,)).fetchone()
        return dict(row) if row else None

    def find_batch_by_client(self, client_key: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sched_batches WHERE client_key=? ORDER BY created_at LIMIT 1",
                (client_key,),
            ).fetchone()
        return dict(row) if row else None

    def batch_plans(self, batch_id: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM sched_plans WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
        return [self._plan_row(row) for row in rows]

    def list_plans(self, state: Optional[str] = None, batch_id: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM sched_plans"
        clauses, params = [], []
        if state:
            clauses.append("state=?")
            params.append(state)
        if batch_id:
            clauses.append("batch_id=?")
            params.append(batch_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._plan_row(row) for row in rows]

    def active_vessels(self) -> set:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT vessel FROM sched_plans WHERE state != 'cancelled'"
            ).fetchall()
        return {row["vessel"] for row in rows}

    def get_plan(self, plan_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM sched_plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFound("计划不存在")
        return self._plan_row(row)

    # ---- 重排合并 -------------------------------------------------------
    def begin_replan(self) -> Dict[str, Any]:
        """开启重排事务并返回快照（状态 + 全部计划），调用方必须以 commit/release 结束。"""
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        state_row = connection.execute("SELECT * FROM sched_state WHERE id=1").fetchone()
        plan_rows = connection.execute("SELECT * FROM sched_plans ORDER BY id").fetchall()
        state = dict(state_row)
        state["tide_levels"] = json.loads(state["tide_levels"]) if state["tide_levels"] else None
        state["resources"] = json.loads(state["resources"]) if state["resources"] else None
        plans = [self._plan_row(row) for row in plan_rows]
        return {"connection": connection, "state": state, "plans": plans}

    def apply_replan(self, snapshot: Dict[str, Any], results: List[Dict[str, Any]], actor_id: str,
                     action: str, tide_version: int) -> Dict[str, int]:
        """在快照事务内按计划版本合并结果。

        - berthed/cancelled：冻结，跳过；
        - 快照之后该计划已被更新版本处理（plan_version 更大）：跳过旧结果，避免重复占用；
        - 其余计划写入新状态、新分配与新版本，并逐条审计。
        返回实际合并数量（含 skipped）。
        """
        connection = snapshot["connection"]
        base_version = int(snapshot["state"]["plan_version"])
        new_version = base_version + 1
        now = _now()
        merged = kept = skipped = recovered = scheduled = pending = invalid = 0
        current = {int(plan["id"]): plan for plan in snapshot["plans"]}
        for result in results:
            plan = current.get(int(result["plan_id"]))
            if plan is None:
                skipped += 1
                continue
            if plan["state"] in ("berthed", "cancelled"):
                skipped += 1
                continue
            if int(plan["plan_version"]) > base_version:
                skipped += 1
                continue
            # 稳定重排：仍可行的已排班计划保持原结果，不产生版本变更与审计。
            if result.get("kept") and plan["state"] == "scheduled":
                kept += 1
                scheduled += 1
                continue
            allocation = result["allocation"].to_dict() if result.get("allocation") else None
            connection.execute(
                "UPDATE sched_plans SET state=?,reason=?,allocation=?,tide_version=?,plan_version=?,updated_at=?"
                " WHERE id=?",
                (result["state"], result.get("reason", ""),
                 json.dumps(allocation, ensure_ascii=False) if allocation else None,
                 tide_version, new_version, now, plan["id"]),
            )
            connection.execute(
                "INSERT INTO sched_audit(scope,ref,action,actor_id,tide_version,plan_version,details,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                ("plan", str(plan["id"]), "plan_%s" % result["state"], actor_id, tide_version, new_version,
                 json.dumps({
                     "was": result.get("was", plan["state"]),
                     "reason": result.get("reason", ""),
                     "recovered": bool(result.get("recovered")),
                     "allocation": allocation,
                     "merge_base_version": base_version,
                 }, ensure_ascii=False), now),
            )
            merged += 1
            if result["state"] == "scheduled":
                scheduled += 1
            elif result["state"] == "pending":
                pending += 1
            elif result["state"] == "invalid":
                invalid += 1
            if result.get("recovered"):
                recovered += 1
        connection.execute("UPDATE sched_state SET plan_version=?, updated_at=? WHERE id=1", (new_version, now))
        connection.execute(
            "INSERT INTO sched_audit(scope,ref,action,actor_id,tide_version,plan_version,details,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            ("system", "system", action, actor_id, tide_version, new_version,
             json.dumps({"merged": merged, "kept": kept, "skipped": skipped, "scheduled": scheduled,
                         "pending": pending, "invalid": invalid, "recovered": recovered}, ensure_ascii=False),
             now),
        )
        # 刷新批次聚合状态
        rows = connection.execute(
            "SELECT batch_id, state, COUNT(*) AS total FROM sched_plans GROUP BY batch_id, state"
        ).fetchall()
        statuses: Dict[str, Dict[str, int]] = {}
        for row in rows:
            statuses.setdefault(row["batch_id"], {})[row["state"]] = int(row["total"])
        for batch_id, counts in statuses.items():
            if counts.get("scheduled", 0) and not (counts.get("pending") or counts.get("invalid")):
                status = "scheduled"
            elif counts.get("berthed") and not (counts.get("pending") or counts.get("invalid") or counts.get("scheduled")):
                status = "berthed"
            elif counts.get("pending"):
                status = "waiting_resources"
            elif counts.get("invalid"):
                status = "invalid"
            else:
                status = "partial"
            connection.execute(
                "UPDATE sched_batches SET status=?, plan_version=?, updated_at=? WHERE batch_id=?",
                (status, new_version, now, batch_id),
            )
        snapshot["connection"] = connection
        snapshot["new_version"] = new_version
        return {"merged": merged, "kept": kept, "skipped": skipped, "scheduled": scheduled,
                "pending": pending, "invalid": invalid, "recovered": recovered}

    def commit_replan(self, snapshot: Dict[str, Any]) -> None:
        snapshot["connection"].commit()

    def rollback_replan(self, snapshot: Dict[str, Any]) -> None:
        snapshot["connection"].rollback()

    # ---- 靠泊 / 取消 ----------------------------------------------------
    def mark_berthed(self, plan_id: int, actor_id: str, actual_draft_m: float) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM sched_plans WHERE id=?", (plan_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("计划不存在")
            plan = self._plan_row(row)
            if plan["state"] != "scheduled":
                connection.rollback()
                raise Conflict("只有已排班(scheduled)的计划可以靠泊")
            new_version = int(dict(connection.execute("SELECT plan_version FROM sched_state WHERE id=1").fetchone())["plan_version"]) + 1
            connection.execute(
                "UPDATE sched_plans SET state='berthed',reason='',plan_version=?,updated_at=? WHERE id=?",
                (new_version, now, plan_id),
            )
            allocation = plan["allocation"] or {}
            connection.execute(
                "INSERT INTO sched_audit(scope,ref,action,actor_id,tide_version,plan_version,details,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                ("plan", str(plan_id), "plan_berthed", actor_id, plan["tide_version"], new_version,
                 json.dumps({"actual_draft_m": actual_draft_m, "frozen_allocation": allocation,
                             "vessel": plan["vessel"]}, ensure_ascii=False), now),
            )
            connection.execute("UPDATE sched_state SET plan_version=?, updated_at=? WHERE id=1", (new_version, now))
            result = connection.execute("SELECT * FROM sched_plans WHERE id=?", (plan_id,)).fetchone()
            connection.commit()
        return self._plan_row(result)

    def cancel_plan(self, plan_id: int, actor_id: str, reason: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM sched_plans WHERE id=?", (plan_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("计划不存在")
            plan = self._plan_row(row)
            if plan["state"] == "berthed":
                connection.rollback()
                raise Conflict("已靠泊计划不能取消")
            if plan["state"] == "cancelled":
                connection.rollback()
                raise Conflict("计划已取消")
            new_version = int(dict(connection.execute("SELECT plan_version FROM sched_state WHERE id=1").fetchone())["plan_version"]) + 1
            connection.execute(
                "UPDATE sched_plans SET state='cancelled',reason=?,allocation=NULL,plan_version=?,updated_at=? WHERE id=?",
                (reason, new_version, now, plan_id),
            )
            connection.execute(
                "INSERT INTO sched_audit(scope,ref,action,actor_id,tide_version,plan_version,details,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                ("plan", str(plan_id), "plan_cancelled", actor_id, plan["tide_version"], new_version,
                 json.dumps({"reason": reason, "vessel": plan["vessel"]}, ensure_ascii=False), now),
            )
            connection.execute("UPDATE sched_state SET plan_version=?, updated_at=? WHERE id=1", (new_version, now))
            result = connection.execute("SELECT * FROM sched_plans WHERE id=?", (plan_id,)).fetchone()
            connection.commit()
        return self._plan_row(result)

    # ---- 审计 -----------------------------------------------------------
    def add_audit(self, scope: str, ref: str, action: str, actor_id: str, tide_version: int,
                  plan_version: int, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO sched_audit(scope,ref,action,actor_id,tide_version,plan_version,details,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (scope, ref, action, actor_id, tide_version, plan_version,
                 json.dumps(details, ensure_ascii=False), _now()),
            )

    def audit(self, scope: Optional[str] = None, ref: Optional[str] = None,
              limit: int = 200) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM sched_audit"
        clauses, params = [], []
        if scope:
            clauses.append("scope=?")
            params.append(scope)
        if ref:
            clauses.append("ref=?")
            params.append(ref)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def recovered_total(self) -> int:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT details FROM sched_audit WHERE action='plan_scheduled'"
            ).fetchall()
        total = 0
        for row in rows:
            if json.loads(row["details"]).get("recovered"):
                total += 1
        return total
