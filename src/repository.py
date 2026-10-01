"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
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
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tide_windows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_hour INTEGER NOT NULL,
                    end_hour INTEGER NOT NULL,
                    water_level_m REAL NOT NULL,
                    plan_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS resources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    resource_id TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL,
                    batch_id INTEGER,
                    plan_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    start_hour INTEGER,
                    end_hour INTEGER,
                    pilot_id TEXT,
                    tug_id TEXT,
                    reason TEXT NOT NULL DEFAULT '',
                    system_managed INTEGER NOT NULL DEFAULT 1,
                    restored INTEGER NOT NULL DEFAULT 0,
                    superseded INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    submitted_by TEXT NOT NULL,
                    plan_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batch_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL,
                    record_id INTEGER NOT NULL,
                    merged INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS system_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    plan_version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_assignments_record ON assignments(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_assignments_active ON assignments(superseded, status);
                CREATE INDEX IF NOT EXISTS idx_batch_items_batch ON batch_items(batch_id);
                CREATE INDEX IF NOT EXISTS idx_system_events ON system_events(id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        return json.loads(row["value"])

    @staticmethod
    def set_meta_tx(connection: sqlite3.Connection, key: str, value: Any) -> None:
        connection.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False, sort_keys=True)),
        )

    def set_meta(self, key: str, value: Any) -> None:
        with self._connect() as connection:
            self.set_meta_tx(connection, key, value)

    def settings(self) -> Dict[str, Any]:
        return {
            "channel_depth_m": float(self.get_meta("channel_depth_m", 10.0)),
            "service_hours": int(self.get_meta("service_hours", 1)),
            "plan_version": int(self.get_meta("plan_version", 0)),
        }

    # ----------------------------------------------------------- tide windows
    def list_tide_windows(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM tide_windows ORDER BY start_hour, id").fetchall()
        return [dict(row) for row in rows]

    def replace_tide_windows(self, connection: sqlite3.Connection, windows: List[Dict[str, Any]], plan_version: int, now: str) -> None:
        connection.execute("DELETE FROM tide_windows")
        connection.executemany(
            "INSERT INTO tide_windows(start_hour,end_hour,water_level_m,plan_version,created_at) VALUES(?,?,?,?,?)",
            [(w["start_hour"], w["end_hour"], float(w["water_level_m"]), plan_version, now) for w in windows],
        )

    # -------------------------------------------------------------- resources
    def list_resources(self, kind: Optional[str] = None, active_only: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM resources"
        clauses: List[str] = []
        params: List[Any] = []
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if active_only:
            clauses.append("active=1")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY kind, resource_id"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            item["active"] = bool(item["active"])
        return items

    def upsert_resource(self, resource_id: str, kind: str, name: str, active: bool) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO resources(resource_id,kind,name,active,created_at,updated_at) VALUES(?,?,?,?,?,?)
                ON CONFLICT(resource_id) DO UPDATE SET kind=excluded.kind,name=excluded.name,active=excluded.active,updated_at=excluded.updated_at
                """,
                (resource_id, kind, name, 1 if active else 0, now, now),
            )

    # ------------------------------------------------------------ assignments
    def active_assignments(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT a.*, r.payload AS record_payload, r.state AS record_state
                FROM assignments a JOIN records r ON r.id = a.record_id
                WHERE a.superseded=0 AND a.status IN ('pending','queued','active','berthed')
                ORDER BY a.id
                """
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["record_payload"] = json.loads(item["record_payload"])
            result.append(item)
        return result

    def latest_assignment_map(self) -> Dict[int, Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT a.* FROM assignments a
                JOIN (SELECT record_id, MAX(id) AS max_id FROM assignments GROUP BY record_id) m
                ON m.max_id = a.id ORDER BY a.id
                """
            ).fetchall()
        return {int(row["record_id"]): dict(row) for row in rows}

    def supersede_for_records(self, connection: sqlite3.Connection, record_ids: List[int]) -> None:
        if not record_ids:
            return
        connection.executemany(
            "UPDATE assignments SET superseded=1 WHERE record_id=? AND superseded=0",
            [(rid,) for rid in record_ids],
        )

    def insert_assignment(
        self,
        connection: sqlite3.Connection,
        record_id: int,
        batch_id: Optional[int],
        plan_version: int,
        result: Dict[str, Any],
        system_managed: bool,
        restored: bool,
        now: str,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO assignments(record_id,batch_id,plan_version,status,start_hour,end_hour,pilot_id,tug_id,reason,system_managed,restored,superseded,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?)
            """,
            (
                record_id,
                batch_id,
                plan_version,
                result["status"],
                result.get("start_hour"),
                result.get("end_hour"),
                result.get("pilot_id"),
                result.get("tug_id"),
                result.get("reason", ""),
                1 if system_managed else 0,
                1 if restored else 0,
                now,
            ),
        )
        return int(cursor.lastrowid)

    def accept_batch_item(
        self,
        connection: sqlite3.Connection,
        record_id: int,
        batch_id: Optional[int],
        plan_version: int,
        result: Dict[str, Any],
        merged: bool,
        now: str,
    ) -> Dict[str, Any]:
        """单条批次受理（调用方持事务）：已有活跃排班则合并，不重复占用拖轮。"""
        existing = connection.execute(
            "SELECT * FROM assignments WHERE record_id=? AND superseded=0 AND status IN ('pending','queued','active') ORDER BY id DESC LIMIT 1",
            (record_id,),
        ).fetchone()
        if existing is not None and not merged:
            raise Conflict("该计划已有待确认排班，重复提交不会重复占用拖轮")
        if batch_id is not None:
            self.add_batch_item(connection, batch_id, record_id, merged)
        if existing is not None:
            assignment_id = int(existing["id"])
        else:
            assignment_id = self.insert_assignment(connection, record_id, batch_id, plan_version, result, True, False, now)
        return dict(connection.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone())

    def begin_batch_transaction(self) -> sqlite3.Connection:
        """打开立即事务，调用方负责 commit/rollback/close。"""
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        return connection

    # ---------------------------------------------------------------- batches
    def create_batch(self, connection: sqlite3.Connection, actor_id: str, plan_version: int, status: str, now: str) -> int:
        cursor = connection.execute(
            "INSERT INTO batches(submitted_by,plan_version,status,created_at,updated_at) VALUES(?,?,?,?,?)",
            (actor_id, plan_version, status, now, now),
        )
        return int(cursor.lastrowid)

    def add_batch_item(self, connection: sqlite3.Connection, batch_id: int, record_id: int, merged: bool) -> None:
        connection.execute(
            "INSERT INTO batch_items(batch_id,record_id,merged) VALUES(?,?,?)",
            (batch_id, record_id, 1 if merged else 0),
        )

    # ---------------------------------------------------------- system events
    def add_system_event(self, connection: sqlite3.Connection, action: str, actor_id: str, plan_version: int, details: Dict[str, Any], now: str) -> int:
        cursor = connection.execute(
            "INSERT INTO system_events(action,actor_id,plan_version,details,created_at) VALUES(?,?,?,?,?)",
            (action, actor_id, plan_version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
        )
        return int(cursor.lastrowid)

    def system_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM system_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    # ----------------------------------------------------------- tide reschedule
    def persist_reschedule(
        self,
        actor_id: str,
        windows: List[Dict[str, Any]],
        new_version: int,
        targets: List[Dict[str, Any]],
        results: Dict[int, Dict[str, Any]],
        invalidated_record_ids: List[int],
        restored_ids: List[int],
        counts: Dict[str, int],
    ) -> Dict[str, Any]:
        """潮汐窗口变更后的整体重算，单事务完成。

        未靠泊的既有排班全部作废旧版本；confirmed 但未靠泊的回退 draft 重新确认；
        berthed/departed/cancelled 不动。
        """
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.set_meta_tx(connection, "plan_version", new_version)
            self.replace_tide_windows(connection, windows, new_version, now)
            self.supersede_for_records(connection, [int(t["id"]) for t in targets])
            for record in targets:
                rid = int(record["id"])
                result = results[rid]
                self.insert_assignment(connection, rid, None, new_version, result, True, rid in restored_ids, now)
                if rid in invalidated_record_ids:
                    connection.execute(
                        "UPDATE records SET state='draft', version=version+1, updated_by=?, updated_at=? WHERE id=?",
                        (actor_id, now, rid),
                    )
                    connection.execute(
                        "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                        (
                            rid,
                            "reschedule",
                            actor_id,
                            int(connection.execute("SELECT version FROM records WHERE id=?", (rid,)).fetchone()["version"]),
                            json.dumps({"summary": "潮汐窗口变更，未靠泊计划失效重算", "plan_version": new_version}, ensure_ascii=False, sort_keys=True),
                            now,
                        ),
                    )
            self.add_system_event(
                connection,
                "tide_reschedule",
                actor_id,
                new_version,
                {"counts": counts, "invalidated": invalidated_record_ids, "restored": restored_ids},
                now,
            )
            connection.commit()
        return {"plan_version": new_version, "counts": counts, "invalidated": invalidated_record_ids, "restored": restored_ids}

    # ----------------------------------------------------------- record ops
    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def plan_view(self, state: Optional[str] = None, limit: int = 200) -> Dict[str, Any]:
        """靠泊计划列表：记录 + 最近一次排班，外加待确认/冲突/恢复/排队数量。"""
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            records = [self._row(row) for row in rows]
        latest = self.latest_assignment_map()
        plan_version = int(self.get_meta("plan_version", 0))
        items = []
        counts = {"pending_confirmation": 0, "conflict": 0, "restored": 0, "queued": 0}
        for record in records:
            assignment = latest.get(int(record["id"]))
            schedule = None
            if assignment is not None and not assignment["superseded"]:
                schedule = self._schedule_view(assignment)
                if schedule["status"] in ("pending", "queued"):
                    counts["pending_confirmation"] += 1
                if schedule["status"] == "queued":
                    counts["queued"] += 1
                if schedule["status"] == "conflict":
                    counts["conflict"] += 1
                if assignment["restored"]:
                    counts["restored"] += 1
            items.append({"record": record, "schedule": schedule})
        counts["plan_version"] = plan_version
        return {"items": items, "counts": counts}

    @staticmethod
    def _schedule_view(assignment: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": assignment["id"],
            "status": assignment["status"],
            "start_hour": assignment["start_hour"],
            "end_hour": assignment["end_hour"],
            "pilot_id": assignment["pilot_id"],
            "tug_id": assignment["tug_id"],
            "reason": assignment["reason"],
            "plan_version": assignment["plan_version"],
            "restored": bool(assignment["restored"]),
            "system_managed": bool(assignment["system_managed"]),
        }

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def mutate_with_assignment(
        self,
        record_id: int,
        expected_version: int,
        state: str,
        payload: Dict[str, Any],
        actor_id: str,
        action: str,
        details: Dict[str, Any],
        assignment_status: Optional[str] = None,
        supersede: bool = False,
    ) -> Dict[str, Any]:
        """状态动作与排班状态同事务切换，保证确认/靠泊不与重算互相覆盖。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            if supersede:
                connection.execute(
                    "UPDATE assignments SET superseded=1 WHERE record_id=? AND superseded=0", (record_id,)
                )
            if assignment_status:
                connection.execute(
                    "UPDATE assignments SET status=? WHERE record_id=? AND superseded=0 AND status IN ('pending','queued','active')",
                    (assignment_status, record_id),
                )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
