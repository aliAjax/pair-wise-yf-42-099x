import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS permits (
                    permit_no TEXT PRIMARY KEY,
                    species TEXT NOT NULL,
                    year INTEGER NOT NULL,
                    quota INTEGER NOT NULL,
                    valid_from TEXT NOT NULL,
                    valid_to TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_permits_species_year_active
                    ON permits(species, year)
                    WHERE status = 'active';
                CREATE TABLE IF NOT EXISTS quota_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    permit_no TEXT NOT NULL,
                    pairing_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    slot INTEGER,
                    position INTEGER,
                    reason TEXT,
                    balance_after INTEGER NOT NULL,
                    actor_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_quota_permit
                    ON quota_entries(permit_no, id);
                CREATE TABLE IF NOT EXISTS registry_jobs (
                    job_no TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS registry_receipts (
                    receipt_no TEXT PRIMARY KEY,
                    job_no TEXT NOT NULL,
                    permit_no TEXT NOT NULL,
                    occupied INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    sent_at TEXT,
                    state TEXT NOT NULL,
                    conflict_reason TEXT,
                    detail TEXT NOT NULL DEFAULT '{}',
                    received_at TEXT NOT NULL,
                    processed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_receipts_job
                    ON registry_receipts(job_no, receipt_no);
                CREATE TABLE IF NOT EXISTS registry_job_receipts (
                    job_no TEXT NOT NULL,
                    receipt_no TEXT NOT NULL,
                    duplicate_of TEXT,
                    PRIMARY KEY(job_no, receipt_no)
                );
            """)

    # -- 持锁事务：并发批准/重算时把“读现占用—决策—落账”收成一笔事务 ------
    def transaction(self):
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        return connection

    @staticmethod
    def commit(connection):
        connection.commit()
        connection.close()

    @staticmethod
    def rollback(connection):
        connection.rollback()
        connection.close()

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def get_entity_tx(self, conn, entity_id):
        """在同一事务内读取实体（能看到本事务尚未提交的更新）。"""
        row = conn.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True

    # ------------------------------------------------------------------
    # 许可证
    # ------------------------------------------------------------------
    @staticmethod
    def _permit_from_row(row):
        return {
            "permit_no": row["permit_no"],
            "species": row["species"],
            "year": int(row["year"]),
            "quota": int(row["quota"]),
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
            "status": row["status"],
            "version": int(row["version"]),
            "notes": row["notes"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_permit(self, permit_no, conn=None):
        sql = "SELECT * FROM permits WHERE permit_no = ?"
        params = (permit_no,)
        row = conn.execute(sql, params).fetchone() if conn is not None else None
        if conn is None:
            with self._connect() as connection:
                row = connection.execute(sql, params).fetchone()
        return self._permit_from_row(row) if row else None

    def list_permits(self, status=None, species=None, year=None):
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if species:
            clauses.append("species = ?")
            params.append(species)
        if year is not None:
            clauses.append("year = ?")
            params.append(int(year))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM permits" + where + " ORDER BY year, species, permit_no",
                params,
            ).fetchall()
        return [self._permit_from_row(row) for row in rows]

    def insert_permit(self, permit, actor_id, conn=None):
        now = utcnow()
        params = (
            permit["permit_no"], permit["species"], int(permit["year"]),
            int(permit["quota"]), permit["valid_from"], permit["valid_to"],
            "active", 1, permit.get("notes", ""), actor_id, now, now,
        )
        sql = (
            "INSERT INTO permits(permit_no, species, year, quota, valid_from, "
            "valid_to, status, version, notes, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        if conn is not None:
            conn.execute(sql, params)
        else:
            with self._connect() as connection:
                connection.execute(sql, params)
        return self.get_permit(permit["permit_no"])

    def update_permit(self, permit, conn=None):
        now = utcnow()
        params = (
            permit["species"], int(permit["year"]), int(permit["quota"]),
            permit["valid_from"], permit["valid_to"], permit["status"],
            int(permit["version"]), permit.get("notes", ""), now,
            permit["permit_no"],
        )
        sql = (
            "UPDATE permits SET species = ?, year = ?, quota = ?, valid_from = ?, "
            "valid_to = ?, status = ?, version = ?, notes = ?, updated_at = ? "
            "WHERE permit_no = ?"
        )
        if conn is not None:
            conn.execute(sql, params)
        else:
            with self._connect() as connection:
                connection.execute(sql, params)
        return self.get_permit(permit["permit_no"])

    # ------------------------------------------------------------------
    # 配额占用与流水
    # ------------------------------------------------------------------
    def get_open_pairings(self, permit_no, conn=None):
        """读取该许可证下所有仍占名额/排队中的配对。

        排队先后必须严格等于提交先后；created_at 只有秒级精度，同秒内会并列，
        故用 SQLite 单调的 rowid（插入顺序）作为最终排序键。
        """
        sql = (
            "SELECT id, status, data, created_at, rowid AS rid FROM entities "
            "WHERE kind = 'pairing' "
            "AND status IN ('approved', 'queued') "
            "AND json_extract(data, '$.permit_no') = ? "
            "ORDER BY rowid"
        )
        if conn is not None:
            rows = conn.execute(sql, (permit_no,)).fetchall()
        else:
            with self._connect() as connection:
                rows = connection.execute(sql, (permit_no,)).fetchall()
        result = []
        for seq, row in enumerate(rows):
            data = json.loads(row["data"])
            result.append({
                "pairing_id": row["id"],
                "status": row["status"],
                "seq": row["rid"],
                "slot": data.get("slot"),
                "created_at": row["created_at"],
            })
        return result

    def count_pairing_states(self, permit_no, conn=None):
        counts = {"approved": 0, "queued": 0, "executed": 0, "voided": 0}
        rows = self._pairings_for_permit(permit_no, conn=conn)
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return counts

    def _pairings_for_permit(self, permit_no, conn=None):
        sql = (
            "SELECT status, data FROM entities WHERE kind = 'pairing' "
            "AND (json_extract(data, '$.permit_no') = ?)"
        )
        if conn is not None:
            rows = conn.execute(sql, (permit_no,)).fetchall()
        else:
            with self._connect() as connection:
                rows = connection.execute(sql, (permit_no,)).fetchall()
        return list(rows)

    def insert_quota_entry(self, permit_no, pairing_id, kind, balance_after,
                           actor_id, slot=None, position=None, reason=None,
                           conn=None):
        now = utcnow()
        params = (
            permit_no, pairing_id, kind, slot, position, reason,
            int(balance_after), actor_id, now,
        )
        sql = (
            "INSERT INTO quota_entries(permit_no, pairing_id, kind, slot, "
            "position, reason, balance_after, actor_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        if conn is not None:
            cur = conn.execute(sql, params)
            return cur.lastrowid
        with self._connect() as connection:
            cur = connection.execute(sql, params)
            return cur.lastrowid

    def list_quota_entries(self, permit_no=None):
        sql = "SELECT * FROM quota_entries"
        params = ()
        if permit_no:
            sql += " WHERE permit_no = ? ORDER BY id"
            params = (permit_no,)
        else:
            sql += " ORDER BY id"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [
            {
                "id": row["id"],
                "permit_no": row["permit_no"],
                "pairing_id": row["pairing_id"],
                "kind": row["kind"],
                "slot": row["slot"],
                "position": row["position"],
                "reason": row["reason"],
                "balance_after": int(row["balance_after"]),
                "actor_id": row["actor_id"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def update_entity_tx(self, conn, entity_id, expected_version, status, data):
        """在给定持锁事务内做带乐观版本号的实体更新。"""
        now = utcnow()
        row = conn.execute(
            "SELECT version FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("entity not found: " + entity_id)
        current_version = int(row["version"])
        if expected_version is not None and current_version != int(expected_version):
            raise ConflictError(
                "version conflict: expected %s, found %s"
                % (expected_version, current_version)
            )
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        conn.execute(
            "UPDATE entities SET status = ?, version = version + 1, data = ?, "
            "updated_at = ? WHERE id = ? AND version = ?",
            (status, payload, now, entity_id, current_version),
        )
        return current_version

    def append_audit_tx(self, conn, entity_id, actor_id, actor_role, action,
                        from_status, to_status, detail):
        conn.execute(
            "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, "
            "from_status, to_status, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entity_id, actor_id, actor_role, action, from_status,
                to_status,
                json.dumps(detail, ensure_ascii=False, sort_keys=True),
                utcnow(),
            ),
        )

    # ------------------------------------------------------------------
    # 外部登记系统回执与对账任务
    # ------------------------------------------------------------------
    @staticmethod
    def _job_from_row(row):
        return {
            "job_no": row["job_no"],
            "status": row["status"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def insert_job(self, job_no, actor_id, conn=None):
        now = utcnow()
        params = (job_no, "open", actor_id, now, now)
        sql = (
            "INSERT INTO registry_jobs(job_no, status, created_by, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?)"
        )
        if conn is not None:
            conn.execute(sql, params)
        else:
            with self._connect() as connection:
                connection.execute(sql, params)
        return self.get_job(job_no)

    def get_job(self, job_no, conn=None):
        sql = "SELECT * FROM registry_jobs WHERE job_no = ?"
        if conn is not None:
            row = conn.execute(sql, (job_no,)).fetchone()
        else:
            with self._connect() as connection:
                row = connection.execute(sql, (job_no,)).fetchone()
        return self._job_from_row(row) if row else None

    def list_jobs(self, status=None):
        sql = "SELECT * FROM registry_jobs"
        params = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY created_at, job_no"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._job_from_row(row) for row in rows]

    def touch_job(self, job_no, status, conn=None):
        if conn is not None:
            conn.execute(
                "UPDATE registry_jobs SET status = ?, updated_at = ? WHERE job_no = ?",
                (status, utcnow(), job_no),
            )
        else:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE registry_jobs SET status = ?, updated_at = ? WHERE job_no = ?",
                    (status, utcnow(), job_no),
                )
        return self.get_job(job_no)

    @staticmethod
    def _receipt_from_row(row):
        return {
            "receipt_no": row["receipt_no"],
            "job_no": row["job_no"],
            "permit_no": row["permit_no"],
            "occupied": int(row["occupied"]),
            "status": row["status"],
            "sent_at": row["sent_at"],
            "state": row["state"],
            "conflict_reason": row["conflict_reason"],
            "detail": json.loads(row["detail"] or "{}"),
            "received_at": row["received_at"],
            "processed_at": row["processed_at"],
        }

    def get_receipt(self, receipt_no, conn=None):
        sql = "SELECT * FROM registry_receipts WHERE receipt_no = ?"
        if conn is not None:
            row = conn.execute(sql, (receipt_no,)).fetchone()
        else:
            with self._connect() as connection:
                row = connection.execute(sql, (receipt_no,)).fetchone()
        return self._receipt_from_row(row) if row else None

    def list_receipts(self, job_no=None, state=None):
        clauses, params = [], []
        if job_no:
            clauses.append("job_no = ?")
            params.append(job_no)
        if state:
            clauses.append("state = ?")
            params.append(state)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM registry_receipts" + where + " ORDER BY received_at, receipt_no",
                params,
            ).fetchall()
        return [self._receipt_from_row(row) for row in rows]

    def insert_receipt(self, receipt, conn=None):
        now = utcnow()
        params = (
            receipt["receipt_no"], receipt["job_no"], receipt["permit_no"],
            int(receipt["occupied"]), receipt["status"], receipt.get("sent_at"),
            receipt["state"], receipt.get("conflict_reason"),
            json.dumps(receipt.get("detail", {}), ensure_ascii=False, sort_keys=True),
            now, receipt.get("processed_at"),
        )
        sql = (
            "INSERT INTO registry_receipts(receipt_no, job_no, permit_no, "
            "occupied, status, sent_at, state, conflict_reason, detail, "
            "received_at, processed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        if conn is not None:
            conn.execute(sql, params)
        else:
            with self._connect() as connection:
                connection.execute(sql, params)

    def mark_receipt(self, receipt_no, state, conflict_reason=None, detail=None,
                     processed=False, conn=None):
        detail_text = json.dumps(detail or {}, ensure_ascii=False, sort_keys=True)
        processed_at = utcnow() if processed else None
        sql = (
            "UPDATE registry_receipts SET state = ?, conflict_reason = ?, "
            "detail = ?, processed_at = COALESCE(?, processed_at) "
            "WHERE receipt_no = ?"
        )
        params = (state, conflict_reason, detail_text, processed_at, receipt_no)
        if conn is not None:
            conn.execute(sql, params)
        else:
            with self._connect() as connection:
                connection.execute(sql, params)

    def add_job_receipt(self, job_no, receipt_no, duplicate_of=None, conn=None):
        sql = (
            "INSERT INTO registry_job_receipts(job_no, receipt_no, duplicate_of) "
            "VALUES (?, ?, ?) ON CONFLICT(job_no, receipt_no) DO UPDATE SET "
            "duplicate_of = excluded.duplicate_of"
        )
        params = (job_no, receipt_no, duplicate_of)
        if conn is not None:
            conn.execute(sql, params)
        else:
            with self._connect() as connection:
                connection.execute(sql, params)

    def list_job_links(self, job_no, conn=None):
        sql = "SELECT receipt_no, duplicate_of FROM registry_job_receipts WHERE job_no = ?"
        if conn is not None:
            rows = conn.execute(sql, (job_no,)).fetchall()
        else:
            with self._connect() as connection:
                rows = connection.execute(sql, (job_no,)).fetchall()
        return [
            {"receipt_no": row["receipt_no"], "duplicate_of": row["duplicate_of"]}
            for row in rows
        ]

    def receipt_state_counts(self, job_no, conn=None):
        sql = "SELECT state, COUNT(*) AS n FROM registry_receipts WHERE job_no = ? GROUP BY state"
        if conn is not None:
            rows = conn.execute(sql, (job_no,)).fetchall()
        else:
            with self._connect() as connection:
                rows = connection.execute(sql, (job_no,)).fetchall()
        return {row["state"]: int(row["n"]) for row in rows}
