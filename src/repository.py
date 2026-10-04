import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError, ValidationError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
                CREATE TABLE IF NOT EXISTS quota_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    license_id TEXT NOT NULL,
                    license_no TEXT NOT NULL,
                    species TEXT NOT NULL,
                    year INTEGER NOT NULL,
                    pairing_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    occupied_at TEXT NOT NULL,
                    executed_at TEXT,
                    voided_at TEXT,
                    void_reason TEXT,
                    UNIQUE(pairing_id)
                );
                CREATE INDEX IF NOT EXISTS idx_ledger_license
                    ON quota_ledger(license_id, status);
                CREATE INDEX IF NOT EXISTS idx_ledger_pairing
                    ON quota_ledger(pairing_id);
            """)

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
    # 配额账（quota ledger）
    # ------------------------------------------------------------------
    def find_license_by_no(self, license_no):
        rows = self.find_entities("license", "license_no", str(license_no))
        return rows[0] if rows else None

    def list_active_licenses(self, species, year):
        result = []
        for lic in self.list_entities(kind="license", status="active"):
            data = lic["data"]
            if data.get("species") == species and int(data.get("year", 0)) == int(year):
                result.append(lic)
        result.sort(key=lambda item: (item["created_at"], item["id"]))
        return result

    def list_queued_pairings(self, species, year):
        result = []
        for pairing in self.list_entities(kind="pairing", status="queued"):
            data = pairing["data"]
            if data.get("species") == species and int(data.get("year", 0)) == int(year):
                result.append(pairing)
        result.sort(key=lambda item: int(item["data"].get("queue_position", 0)))
        return result

    def count_used(self, license_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT "
                "COALESCE(SUM(CASE WHEN status = 'occupied' THEN 1 ELSE 0 END), 0) AS occupied, "
                "COALESCE(SUM(CASE WHEN status = 'executed' THEN 1 ELSE 0 END), 0) AS executed "
                "FROM quota_ledger WHERE license_id = ?",
                (license_id,),
            ).fetchone()
        return {"occupied": int(row["occupied"]), "executed": int(row["executed"])}

    def list_ledger(self, license_id=None, status=None):
        clauses = []
        params = []
        if license_id:
            clauses.append("license_id = ?")
            params.append(license_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM quota_ledger" + where + " ORDER BY id", params
            ).fetchall()
        return [dict(row) for row in rows]

    def get_occupation(self, pairing_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM quota_ledger WHERE pairing_id = ?", (pairing_id,)
            ).fetchone()
        return dict(row) if row else None

    def occupy_pairing(self, pairing_id, expected_version, license_id, pairing_data, queue_if_full):
        """Atomically check the pairing version, check the license is active,
        occupy a slot if one is free, and update the pairing status.

        Returns (entity, info). When no slot is free and queue_if_full is true
        the pairing is moved to 'queued'; otherwise it is left unchanged.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (pairing_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + pairing_id)
            pairing = self._entity_from_row(row)
            if expected_version is not None and pairing["version"] != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, pairing["version"])
                )
            lic_row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (license_id,)
            ).fetchone()
            if not lic_row:
                raise ValidationError("license not found: " + license_id)
            lic = self._entity_from_row(lic_row)
            if lic["kind"] != "license" or lic["status"] != "active":
                raise ValidationError("license is not active: " + lic["id"])
            quota = int(lic["data"].get("quota", 0))
            used = int(
                connection.execute(
                    "SELECT COUNT(*) AS c FROM quota_ledger "
                    "WHERE license_id = ? AND status IN ('occupied', 'executed')",
                    (license_id,),
                ).fetchone()["c"]
            )
            remaining = quota - used
            now = utcnow()
            info = {
                "remaining": remaining,
                "queued": False,
                "license_id": license_id,
                "license_no": lic["data"].get("license_no"),
                "species": lic["data"].get("species"),
                "year": lic["data"].get("year"),
            }
            next_status = "approved"
            payload_data = dict(pairing_data)
            if remaining > 0:
                connection.execute(
                    "INSERT INTO quota_ledger(license_id, license_no, species, year, pairing_id, "
                    "status, occupied_at) VALUES (?, ?, ?, ?, ?, 'occupied', ?)",
                    (
                        license_id,
                        info["license_no"],
                        info["species"],
                        info["year"],
                        pairing_id,
                        now,
                    ),
                )
            else:
                if not queue_if_full:
                    connection.rollback()
                    return pairing, info
                next_status = "queued"
                info["queued"] = True
                queued_count = int(
                    connection.execute(
                        "SELECT COUNT(*) AS c FROM entities WHERE kind = 'pairing' AND status = 'queued'"
                    ).fetchone()["c"]
                )
                payload_data["queue_position"] = queued_count + 1
            payload = json.dumps(payload_data, ensure_ascii=False, sort_keys=True)
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (next_status, payload, now, pairing_id, pairing["version"]),
            )
            connection.commit()
            updated = self.get_entity(pairing_id)
            return updated, info
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def void_occupations(self, license_id, limit, reason):
        """Release up to `limit` occupied (unexecuted) slots for a license,
        most recent first. Returns the pairing ids that were released."""
        connection = self._connect()
        voided = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            if limit is None:
                rows = connection.execute(
                    "SELECT id, pairing_id FROM quota_ledger "
                    "WHERE license_id = ? AND status = 'occupied' ORDER BY id DESC",
                    (license_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT id, pairing_id FROM quota_ledger "
                    "WHERE license_id = ? AND status = 'occupied' ORDER BY id DESC LIMIT ?",
                    (license_id, int(limit)),
                ).fetchall()
            now = utcnow()
            for row in rows:
                connection.execute(
                    "UPDATE quota_ledger SET status = 'voided', voided_at = ?, void_reason = ? "
                    "WHERE id = ? AND status = 'occupied'",
                    (now, reason, row["id"]),
                )
                voided.append(row["pairing_id"])
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return voided

    def void_occupation_for_pairing(self, pairing_id, reason):
        with self._connect() as connection:
            connection.execute(
                "UPDATE quota_ledger SET status = 'voided', voided_at = ?, void_reason = ? "
                "WHERE pairing_id = ? AND status = 'occupied'",
                (utcnow(), reason, pairing_id),
            )

    def mark_executed(self, pairing_id):
        with self._connect() as connection:
            connection.execute(
                "UPDATE quota_ledger SET status = 'executed', executed_at = ? "
                "WHERE pairing_id = ? AND status = 'occupied'",
                (utcnow(), pairing_id),
            )

    def void_pairing(self, pairing_id, reason):
        """Mark an unexecuted pairing as voided (失效) with a visible reason."""
        entity = self.get_entity(pairing_id)
        if not entity:
            return None
        data = dict(entity["data"])
        data["void_reason"] = reason
        data["voided_at"] = utcnow()
        return self.update_entity(pairing_id, entity["version"], "voided", data)
