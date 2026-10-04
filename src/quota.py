"""繁育名额 / 配对建议 / 许可证的可恢复配额账。

设计要点：
- 批准占用名额在单条 ``BEGIN IMMEDIATE`` 事务内完成“读现占用—决策—落账”，
  两人同时提交时由 SQLite 写锁串行化：先到者占名额，后来者拿到剩余名额或排队。
- 许可证到期、撤回或下调时重算排队：未执行的批准/排队项按规则自动失效，
  已完成（executed）的结果保留；每一步都写配额流水 quota_entries，账可重放核对。
- 配对批准与运输放行分开记录；放行保存许可证编号快照，许可证变更不影响已完成结果。
- 外部登记系统回执按编号去重对账；晚到回执与现占用冲突时保留为待处理项，
  重试只续做未完成项，所有状态都在 SQLite 中，重启后可接着处理。
"""
from uuid import uuid4

from . import rules
from .domain import (
    ConflictError,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .repository import utcnow


PERMIT_ROLES = ("admin", "registrar")
PAIRING_ROLES = ("admin", "coordinator")
RELEASE_ROLES = ("admin", "registrar")
REGISTRY_ROLES = ("admin", "registry")
RESOLVE_ROLES = ("admin", "registrar")

PAIRING_KINDS = ("pairing", "release")


def _ensure(actor, allowed):
    if actor.role not in allowed:
        raise PermissionDenied("role %s is not allowed here" % actor.role)


def _void_label(reason):
    return rules.VOID_REASON_LABELS.get(reason, reason)


class QuotaService:
    def __init__(self, repository, audit):
        self.repository = repository
        self.audit = audit

    # ------------------------------------------------------------------
    # 许可证
    # ------------------------------------------------------------------
    def issue_permit(self, actor, data):
        _ensure(actor, PERMIT_ROLES)
        permit = rules.validate_permit_payload(data)
        if self.repository.get_permit(permit["permit_no"]):
            raise ConflictError("permit already exists: " + permit["permit_no"])
        clash = self.repository.list_permits(
            status=rules.PERMIT_ACTIVE,
            species=permit["species"], year=permit["year"],
        )
        if clash:
            raise ConflictError(
                "an active permit for %s/%s already exists: %s"
                % (permit["species"], permit["year"], clash[0]["permit_no"])
            )
        saved = self.repository.insert_permit(permit, actor.user_id)
        self.audit.record(
            permit["permit_no"], actor, "issue_permit", None, "active",
            {"species": saved["species"], "year": saved["year"],
             "quota": saved["quota"]},
        )
        return saved

    def amend_permit(self, actor, permit_no, data):
        """许可证变更（不涉及名额/状态）：旧批准仍可继续执行。"""
        _ensure(actor, PERMIT_ROLES)
        permit = self._require_permit(permit_no)
        if permit["status"] != rules.PERMIT_ACTIVE:
            raise InvalidTransition(
                "only active permits can be amended (status=%s)" % permit["status"]
            )
        patch = rules.validate_permit_payload(data, partial=True)
        protected = set(patch) & {"permit_no", "species", "year", "quota", "status"}
        if protected:
            raise ValidationError(
                "amend cannot change: " + ", ".join(sorted(protected))
                + "；名额调整请用 adjust_quota，失效请用 expire/withdraw"
            )
        old = dict(permit)
        permit.update(patch)
        rules.validate_permit_payload(permit)
        permit["version"] += 1
        saved = self.repository.update_permit(permit)
        self.audit.record(
            permit_no, actor, "amend_permit", old["status"], saved["status"],
            {"patch": patch, "old_version": old["version"],
             "new_version": saved["version"]},
        )
        return saved

    def adjust_quota(self, actor, permit_no, new_quota):
        """下调：超出名额的未执行批准自动失效并重算排队；上调：排队项递补。"""
        _ensure(actor, PERMIT_ROLES)
        new_quota = int(new_quota)
        if new_quota < 0:
            raise ValidationError("quota must be >= 0")
        conn = self.repository.transaction()
        try:
            permit = self._require_permit(permit_no, conn)
            if permit["status"] != rules.PERMIT_ACTIVE:
                raise InvalidTransition(
                    "cannot adjust quota of %s permit" % permit["status"]
                )
            old_quota = permit["quota"]
            entries = self.repository.get_open_pairings(permit_no, conn=conn)
            executed = self.repository.count_pairing_states(
                permit_no, conn=conn
            ).get("executed", 0)
            plan = rules.plan_recompute(entries, new_quota, executed=executed)
            changes = self._apply_plan(
                conn, actor, permit_no, plan,
                default_void_reason=rules.REASON_QUOTA_REDUCED,
            )
            permit["quota"] = new_quota
            permit["version"] += 1
            self.repository.update_permit(permit, conn=conn)
            self.repository.append_audit_tx(
                conn, permit_no, actor.user_id, actor.role,
                "adjust_quota", "active", "active",
                {"old_quota": old_quota, "new_quota": new_quota,
                 "changes": changes},
            )
            self.repository.commit(conn)
        except Exception:
            self.repository.rollback(conn)
            raise
        return self.permit_ledger(permit_no)

    def expire_permit(self, actor, permit_no):
        return self._close_permit(
            actor, permit_no, rules.PERMIT_EXPIRED,
            rules.REASON_PERMIT_EXPIRED, "expire_permit",
        )

    def withdraw_permit(self, actor, permit_no):
        return self._close_permit(
            actor, permit_no, rules.PERMIT_WITHDRAWN,
            rules.REASON_PERMIT_WITHDRAWN, "withdraw_permit",
        )

    def _close_permit(self, actor, permit_no, new_status, reason, action):
        _ensure(actor, PERMIT_ROLES)
        conn = self.repository.transaction()
        try:
            permit = self._require_permit(permit_no, conn)
            if permit["status"] == new_status:
                raise InvalidTransition("permit already " + new_status)
            if permit["status"] != rules.PERMIT_ACTIVE:
                raise InvalidTransition(
                    "permit is %s and cannot be %s" % (permit["status"], new_status)
                )
            entries = self.repository.get_open_pairings(permit_no, conn=conn)
            plan = rules.plan_expire_all(entries)
            changes = self._apply_plan(
                conn, actor, permit_no, plan, default_void_reason=reason
            )
            old_status = permit["status"]
            permit["status"] = new_status
            permit["version"] += 1
            self.repository.update_permit(permit, conn=conn)
            self.repository.append_audit_tx(
                conn, permit_no, actor.user_id, actor.role,
                action, old_status, new_status,
                {"reason": reason, "changes": changes},
            )
            self.repository.commit(conn)
        except Exception:
            self.repository.rollback(conn)
            raise
        return self.permit_ledger(permit_no)

    def list_permits(self, **filters):
        return self.repository.list_permits(**filters)

    def permit_ledger(self, permit_no):
        permit = self._require_permit(permit_no)
        counts = self.repository.count_pairing_states(permit_no)
        occupied = counts.get("approved", 0) + counts.get("executed", 0)
        pairings = [
            self._pairing_view(entity)
            for entity in self.repository.list_entities(kind="pairing")
            if entity["data"].get("permit_no") == permit_no
        ]
        pairings.sort(key=lambda item: (item["created_at"], item["id"]))
        return {
            "permit": permit,
            "occupied": occupied,
            "approved": counts.get("approved", 0),
            "queued": counts.get("queued", 0),
            "executed": counts.get("executed", 0),
            "voided": counts.get("voided", 0),
            "remaining": max(0, permit["quota"] - counts.get("approved", 0)),
            "pairings": pairings,
            "entries": self.repository.list_quota_entries(permit_no),
        }

    # ------------------------------------------------------------------
    # 配对建议与名额占用
    # ------------------------------------------------------------------
    def propose_pairing(self, actor, data, idempotency_key=None):
        _ensure(actor, PAIRING_ROLES)
        species = data.get("species")
        permit_no = data.get("permit_no")
        if not species or not permit_no:
            raise ValidationError("species and permit_no are required")
        permit = self._require_permit(permit_no)
        if species != permit["species"]:
            raise ValidationError("pairing species does not match permit species")
        if not rules.permit_is_active(permit):
            raise InvalidTransition(
                "permit %s is not active (status=%s)" % (permit_no, permit["status"])
            )
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        payload = {
            "species": species,
            "permit_no": permit_no,
            "proposed_by": actor.user_id,
        }
        entity_id = str(data.get("id") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        entity = self.repository.create_entity(
            entity_id, "pairing", rules.PAIRING_PROPOSED, payload, actor.user_id
        )
        self.audit.record(
            entity_id, actor, "propose", None, rules.PAIRING_PROPOSED,
            {"permit_no": permit_no, "species": species},
        )
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def approve_pairing(self, actor, pairing_id, data=None, expected_version=None):
        """提交批准：并发安全。有名额则占用，无名额则排队，后提交者不插队。"""
        _ensure(actor, PAIRING_ROLES)
        data = dict(data or {})
        conn = self.repository.transaction()
        try:
            entity = self.repository.get_entity(pairing_id)
            if not entity:
                raise NotFoundError("entity not found: " + pairing_id)
            if entity["kind"] != "pairing":
                raise ValidationError("entity %s is not a pairing" % pairing_id)
            if entity["status"] != rules.PAIRING_PROPOSED:
                raise InvalidTransition(
                    "cannot approve pairing from status %s" % entity["status"]
                )
            permit_no = entity["data"].get("permit_no")
            permit = self._require_permit(permit_no, conn)
            sire = self.repository.get_entity(data.get("sire_id"))
            dam = self.repository.get_entity(data.get("dam_id"))
            rules.validate_pairing_proposal_data(
                {**data, "species": entity["data"]["species"]},
                sire, dam, permit,
            )
            expected = (
                int(expected_version) if expected_version is not None
                else entity["version"]
            )
            entries = self.repository.get_open_pairings(permit_no, conn=conn)
            executed = self.repository.count_pairing_states(
                permit_no, conn=conn
            ).get("executed", 0)
            plan = rules.decide_submit(
                entries, permit["quota"], pairing_id, executed=executed
            )
            mine = plan.pop(pairing_id)
            other_changes = self._apply_plan(
                conn, actor, permit_no, plan,
                default_void_reason=rules.REASON_QUOTA_REDUCED,
            )
            new_status = mine["status"]
            merged = dict(entity["data"])
            merged.update({
                "sire_id": data["sire_id"],
                "dam_id": data["dam_id"],
                "approved_by": actor.user_id,
                "permit_version_at_approval": permit["version"],
                "slot": mine["slot"],
                "queue_position": mine["position"],
                "void_reason": None,
                "void_reason_text": None,
            })
            self.repository.update_entity_tx(
                conn, pairing_id, expected, new_status, merged
            )
            counts = self.repository.count_pairing_states(permit_no, conn=conn)
            balance = counts.get("approved", 0) + counts.get("executed", 0)
            kind = "reserve" if new_status == rules.PAIRING_APPROVED else "enqueue"
            self.repository.insert_quota_entry(
                permit_no, pairing_id, kind, balance, actor.user_id,
                slot=mine["slot"], position=mine["position"], conn=conn,
            )
            self.repository.append_audit_tx(
                conn, pairing_id, actor.user_id, actor.role,
                "approve", rules.PAIRING_PROPOSED, new_status,
                {"slot": mine["slot"], "queue_position": mine["position"],
                 "permit_no": permit_no,
                 "permit_version_at_approval": permit["version"],
                 "queue_changes": other_changes},
            )
            self.repository.commit(conn)
        except Exception:
            self.repository.rollback(conn)
            raise
        return self.repository.get_entity(pairing_id)

    def execute_pairing(self, actor, pairing_id, data=None):
        """完成配对：结果永久保留，占用转为已完成占用，不再受许可证变更影响。"""
        _ensure(actor, PAIRING_ROLES)
        entity = self.repository.get_entity(pairing_id)
        if not entity:
            raise NotFoundError("entity not found: " + pairing_id)
        if entity["status"] != rules.PAIRING_APPROVED:
            raise InvalidTransition(
                "only approved pairings can be executed (current=%s)"
                % entity["status"]
            )
        permit_no = entity["data"].get("permit_no")
        data = dict(data or {})
        merged = dict(entity["data"])
        if data.get("offspring_ids"):
            merged["offspring_ids"] = data["offspring_ids"]
        merged["executed_by"] = actor.user_id
        conn = self.repository.transaction()
        try:
            self.repository.update_entity_tx(
                conn, pairing_id, entity["version"], rules.PAIRING_EXECUTED, merged
            )
            counts = self.repository.count_pairing_states(permit_no, conn=conn)
            balance = counts.get("approved", 0) + counts.get("executed", 0)
            self.repository.insert_quota_entry(
                permit_no, pairing_id, "execute", balance, actor.user_id,
                slot=merged.get("slot"), conn=conn,
            )
            # 完成的结果永久占用该年度名额，不释放、不触发排队递补；
            # 名额空出只发生在“未执行的批准失效”时（见 _void_pairing/重算）。
            self.repository.append_audit_tx(
                conn, pairing_id, actor.user_id, actor.role,
                "execute", rules.PAIRING_APPROVED, rules.PAIRING_EXECUTED,
                {"offspring_ids": merged.get("offspring_ids", [])},
            )
            self.repository.commit(conn)
        except Exception:
            self.repository.rollback(conn)
            raise
        return self.permit_ledger(permit_no)

    def reject_pairing(self, actor, pairing_id, data=None):
        _ensure(actor, PAIRING_ROLES)
        entity = self.repository.get_entity(pairing_id)
        if not entity:
            raise NotFoundError("entity not found: " + pairing_id)
        if entity["status"] not in (
            rules.PAIRING_PROPOSED, rules.PAIRING_QUEUED, rules.PAIRING_APPROVED
        ):
            raise InvalidTransition(
                "cannot reject pairing from status %s" % entity["status"]
            )
        reason = (data or {}).get("reason") or rules.REASON_REJECTED
        # proposed 未占名额，无需重算；approved/queued 失效后按现名额重算排队
        recalc = entity["status"] in (rules.PAIRING_APPROVED, rules.PAIRING_QUEUED)
        self._void_pairing(
            actor, pairing_id, reason,
            extra={"rejection_note": (data or {}).get("note")},
            recalc=recalc,
        )
        return self.repository.get_entity(pairing_id)

    def _void_pairing(self, actor, pairing_id, reason, extra=None, recalc=True):
        entity = self.repository.get_entity(pairing_id)
        if not entity:
            raise NotFoundError("entity not found: " + pairing_id)
        permit_no = entity["data"].get("permit_no")
        conn = self.repository.transaction()
        try:
            merged = dict(entity["data"])
            merged["void_reason"] = reason
            merged["void_reason_text"] = _void_label(reason)
            merged["voided_by"] = actor.user_id
            merged["voided_at"] = utcnow()
            merged["slot"] = None
            merged["queue_position"] = None
            if extra:
                merged.update(extra)
            from_status = entity["status"]
            self.repository.update_entity_tx(
                conn, pairing_id, entity["version"],
                rules.PAIRING_VOIDED, merged,
            )
            counts = self.repository.count_pairing_states(permit_no, conn=conn)
            balance = counts.get("approved", 0) + counts.get("executed", 0)
            self.repository.insert_quota_entry(
                permit_no, pairing_id, "void", balance, actor.user_id,
                reason=reason, conn=conn,
            )
            changes = []
            if recalc:
                entries = self.repository.get_open_pairings(permit_no, conn=conn)
                permit = self._require_permit(permit_no, conn)
                if permit["status"] == rules.PERMIT_ACTIVE and entries:
                    executed = self.repository.count_pairing_states(
                        permit_no, conn=conn
                    ).get("executed", 0)
                    plan = rules.plan_recompute(
                        entries, permit["quota"], executed=executed
                    )
                    changes = self._apply_plan(
                        conn, actor, permit_no, plan,
                        default_void_reason=rules.REASON_QUOTA_REDUCED,
                    )
            self.repository.append_audit_tx(
                conn, pairing_id, actor.user_id, actor.role,
                "void", from_status, rules.PAIRING_VOIDED,
                {"reason": reason, "reason_text": _void_label(reason),
                 "queue_changes": changes},
            )
            self.repository.commit(conn)
        except Exception:
            self.repository.rollback(conn)
            raise

    def _apply_plan(self, conn, actor, permit_no, plan, default_void_reason):
        """把重算/排队决策落账：更新实体状态、slot/position 并写流水。"""
        changes = []
        for pairing_id, decision in sorted(
            plan.items(), key=lambda item: item[0]
        ):
            entity = self.repository.get_entity_tx(conn, pairing_id)
            if not entity:
                continue
            old_status = entity["status"]
            new_status = decision["status"]
            merged = dict(entity["data"])
            merged["slot"] = decision.get("slot")
            merged["queue_position"] = decision.get("position")
            kind = None
            reason = None
            if new_status != old_status:
                if new_status == rules.PAIRING_APPROVED:
                    kind = "promote" if old_status == rules.PAIRING_QUEUED else "reserve"
                elif new_status == rules.PAIRING_VOIDED:
                    kind = "void"
                    reason = default_void_reason
                    merged["void_reason"] = reason
                    merged["void_reason_text"] = _void_label(reason)
                    merged["voided_by"] = actor.user_id
                    merged["voided_at"] = utcnow()
            self.repository.update_entity_tx(
                conn, pairing_id, entity["version"], new_status, merged
            )
            if kind:
                counts = self.repository.count_pairing_states(permit_no, conn=conn)
                balance = counts.get("approved", 0) + counts.get("executed", 0)
                self.repository.insert_quota_entry(
                    permit_no, pairing_id, kind, balance, actor.user_id,
                    slot=decision.get("slot"),
                    position=decision.get("position"), reason=reason, conn=conn,
                )
                self.repository.append_audit_tx(
                    conn, pairing_id, actor.user_id, actor.role,
                    "quota_" + kind, old_status, new_status,
                    {"slot": decision.get("slot"),
                     "position": decision.get("position"),
                     "reason": reason},
                )
                changes.append({
                    "pairing_id": pairing_id,
                    "from": old_status,
                    "to": new_status,
                    "slot": decision.get("slot"),
                    "reason": reason,
                })
            elif old_status == rules.PAIRING_QUEUED and new_status == rules.PAIRING_QUEUED:
                # 排队位次变化也要留痕（不改变占用余额）
                counts = self.repository.count_pairing_states(permit_no, conn=conn)
                balance = counts.get("approved", 0) + counts.get("executed", 0)
                self.repository.insert_quota_entry(
                    permit_no, pairing_id, "requeue", balance, actor.user_id,
                    position=decision.get("position"), conn=conn,
                )
        return changes

    def list_pairings(self, status=None, permit_no=None):
        result = [
            self._pairing_view(entity)
            for entity in self.repository.list_entities(kind="pairing", status=status)
        ]
        if permit_no:
            result = [item for item in result if item["permit_no"] == permit_no]
        return result

    @staticmethod
    def _pairing_view(entity):
        data = entity["data"]
        return {
            "id": entity["id"],
            "kind": entity["kind"],
            "status": entity["status"],
            "version": entity["version"],
            "permit_no": data.get("permit_no"),
            "species": data.get("species"),
            "sire_id": data.get("sire_id"),
            "dam_id": data.get("dam_id"),
            "slot": data.get("slot"),
            "queue_position": data.get("queue_position"),
            "void_reason": data.get("void_reason"),
            "void_reason_text": data.get("void_reason_text"),
            "offspring_ids": data.get("offspring_ids", []),
            "permit_version_at_approval": data.get("permit_version_at_approval"),
            "created_at": entity["created_at"],
            "updated_at": entity["updated_at"],
        }

    # ------------------------------------------------------------------
    # 运输放行（与配对批准分开记）
    # ------------------------------------------------------------------
    def create_release(self, actor, data, idempotency_key=None):
        _ensure(actor, RELEASE_ROLES)
        pairing_id = data.get("pairing_id")
        if not pairing_id:
            raise ValidationError("pairing_id is required")
        pairing = self.repository.get_entity(pairing_id)
        if not pairing or pairing["kind"] != "pairing":
            raise NotFoundError("pairing not found: " + str(pairing_id))
        if pairing["status"] != rules.PAIRING_EXECUTED:
            raise InvalidTransition(
                "transport release requires an executed pairing (current=%s)"
                % pairing["status"]
            )
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        permit_no = pairing["data"].get("permit_no")
        permit = self.repository.get_permit(permit_no)
        payload = {
            "pairing_id": pairing_id,
            "permit_no": permit_no,
            # 快照：放行时记录当时的许可证编号与版本，之后许可证变更/撤回
            # 都不影响这条已完成的放行记录
            "permit_snapshot": {
                "permit_no": permit_no,
                "species": permit["species"] if permit else None,
                "year": permit["year"] if permit else None,
                "version": pairing["data"].get("permit_version_at_approval"),
            },
            "from_institution": data.get("from_institution"),
            "to_institution": data.get("to_institution"),
            "released_by": actor.user_id,
        }
        entity_id = str(data.get("id") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        entity = self.repository.create_entity(
            entity_id, "release", "released", payload, actor.user_id
        )
        self.audit.record(
            entity_id, actor, "release", None, "released",
            {"pairing_id": pairing_id, "permit_no": permit_no},
        )
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def list_releases(self, pairing_id=None):
        items = self.repository.list_entities(kind="release")
        if pairing_id:
            items = [e for e in items if e["data"].get("pairing_id") == pairing_id]
        return items

    # ------------------------------------------------------------------
    # 外部登记系统：回执对账（可恢复）
    # ------------------------------------------------------------------
    def open_registry_job(self, actor, job_no=None):
        _ensure(actor, REGISTRY_ROLES)
        job_no = job_no or ("job-" + uuid4().hex[:12])
        existing = self.repository.get_job(job_no)
        if existing:
            raise ConflictError("job already exists: " + job_no)
        return self.repository.insert_job(job_no, actor.user_id)

    def receive_receipts(self, actor, job_no, receipts):
        """登记一批回执并立即对账。每个编号只处理一次，冲突项留下待处理。"""
        _ensure(actor, REGISTRY_ROLES)
        job = self.repository.get_job(job_no)
        if not job:
            raise NotFoundError("registry job not found: " + job_no)
        if not isinstance(receipts, list) or not receipts:
            raise ValidationError("receipts must be a non-empty list")
        accepted = conflict = duplicate = 0
        for raw in receipts:
            receipt = rules.validate_receipt_payload(raw)
            verdict, item = self._ingest_receipt(actor, job_no, receipt)
            if verdict == rules.RECEIPT_ACCEPTED:
                accepted += 1
            elif verdict == rules.RECEIPT_CONFLICT:
                conflict += 1
            else:
                duplicate += 1
        return self._job_view(job_no, summary={
            "accepted_now": accepted,
            "conflict_now": conflict,
            "duplicate_now": duplicate,
        })

    def _ingest_receipt(self, actor, job_no, receipt):
        receipt_no = receipt["receipt_no"]
        conn = self.repository.transaction()
        try:
            prior = self.repository.get_receipt(receipt_no, conn=conn)
            if prior:
                # 外部系统重复回传同一许可证：按编号对账，只登记不重处理
                self.repository.add_job_receipt(
                    job_no, receipt_no, duplicate_of=receipt_no, conn=conn
                )
                if prior["job_no"] != job_no:
                    self.repository.add_job_receipt(
                        prior["job_no"], receipt_no, conn=conn
                    )
                self.repository.commit(conn)
                self.audit.record(
                    receipt_no, actor, "receipt_duplicate",
                    prior["state"], prior["state"],
                    {"job_no": job_no, "original_job": prior["job_no"]},
                )
                return rules.RECEIPT_DUPLICATE, prior
            self.repository.insert_receipt({
                **receipt,
                "job_no": job_no,
                "state": "received",
            }, conn=conn)
            self.repository.add_job_receipt(job_no, receipt_no, conn=conn)
            permit = self.repository.get_permit(receipt["permit_no"], conn=conn)
            counts = (
                self.repository.count_pairing_states(receipt["permit_no"], conn=conn)
                if permit else {}
            )
            current_occupied = counts.get("approved", 0) + counts.get("executed", 0)
            verdict, reason, detail = rules.classify_receipt(
                receipt, permit, current_occupied
            )
            if verdict == rules.RECEIPT_ACCEPTED:
                self.repository.mark_receipt(
                    receipt_no, "accepted", processed=True, detail=detail, conn=conn
                )
                self.repository.append_audit_tx(
                    conn, receipt_no, actor.user_id, actor.role,
                    "receipt_accept", None, "accepted",
                    {"permit_no": receipt["permit_no"], "job_no": job_no},
                )
            else:
                # 晚到回执与现占用冲突：保留为待处理项，核对到此为止
                self.repository.mark_receipt(
                    receipt_no, "pending", conflict_reason=reason,
                    detail=detail, conn=conn
                )
                self.repository.append_audit_tx(
                    conn, receipt_no, actor.user_id, actor.role,
                    "receipt_conflict", None, "pending",
                    {"permit_no": receipt["permit_no"], "job_no": job_no,
                     "reason": reason},
                )
            self._refresh_job_status(conn, job_no)
            self.repository.commit(conn)
            return verdict, self.repository.get_receipt(receipt_no)
        except Exception:
            self.repository.rollback(conn)
            raise

    def retry_pending(self, actor, job_no):
        """重试只续做未完成项；已 accepted / duplicate 的不重做。"""
        _ensure(actor, REGISTRY_ROLES)
        job = self.repository.get_job(job_no)
        if not job:
            raise NotFoundError("registry job not found: " + job_no)
        retried = accepted = still_pending = 0
        for receipt in self.repository.list_receipts(job_no=job_no, state="pending"):
            retried += 1
            verdict = self._recheck_receipt(actor, receipt)
            if verdict == rules.RECEIPT_ACCEPTED:
                accepted += 1
            else:
                still_pending += 1
        return self._job_view(job_no, summary={
            "retried": retried,
            "accepted_now": accepted,
            "still_pending": still_pending,
        })

    def _recheck_receipt(self, actor, receipt):
        conn = self.repository.transaction()
        try:
            permit = self.repository.get_permit(receipt["permit_no"], conn=conn)
            counts = (
                self.repository.count_pairing_states(receipt["permit_no"], conn=conn)
                if permit else {}
            )
            current_occupied = counts.get("approved", 0) + counts.get("executed", 0)
            fresh = self.repository.get_receipt(receipt["receipt_no"], conn=conn)
            verdict, reason, detail = rules.classify_receipt(
                fresh, permit, current_occupied
            )
            if verdict == rules.RECEIPT_ACCEPTED:
                self.repository.mark_receipt(
                    receipt["receipt_no"], "accepted", conflict_reason=None,
                    detail=detail, processed=True, conn=conn,
                )
                self.repository.append_audit_tx(
                    conn, receipt["receipt_no"], actor.user_id, actor.role,
                    "receipt_retry_accept", "pending", "accepted",
                    {"job_no": receipt["job_no"]},
                )
            else:
                self.repository.mark_receipt(
                    receipt["receipt_no"], "pending", conflict_reason=reason,
                    detail=detail, conn=conn,
                )
            self._refresh_job_status(conn, receipt["job_no"])
            self.repository.commit(conn)
            return verdict
        except Exception:
            self.repository.rollback(conn)
            raise

    def resolve_conflict(self, actor, receipt_no, resolution, note=None):
        """人工处理待处理项：accept=以本地账为准接受回执；exempt=登记豁免/差错备注。"""
        _ensure(actor, RESOLVE_ROLES)
        receipt = self.repository.get_receipt(receipt_no)
        if not receipt:
            raise NotFoundError("receipt not found: " + receipt_no)
        if receipt["state"] != "pending":
            raise InvalidTransition(
                "receipt is %s, only pending items can be resolved" % receipt["state"]
            )
        if resolution not in ("accept", "exempt"):
            raise ValidationError("resolution must be accept or exempt")
        new_state = "resolved_accepted" if resolution == "accept" else "resolved_exempt"
        conn = self.repository.transaction()
        try:
            self.repository.mark_receipt(
                receipt_no, new_state,
                conflict_reason=receipt["conflict_reason"],
                detail={**receipt["detail"], "resolution": resolution,
                        "resolved_by": actor.user_id, "note": note},
                processed=True, conn=conn,
            )
            self.repository.append_audit_tx(
                conn, receipt_no, actor.user_id, actor.role,
                "receipt_resolve", "pending", new_state,
                {"resolution": resolution, "note": note,
                 "original_reason": receipt["conflict_reason"]},
            )
            self._refresh_job_status(conn, receipt["job_no"])
            self.repository.commit(conn)
        except Exception:
            self.repository.rollback(conn)
            raise
        return self.repository.get_receipt(receipt_no)

    def recover_on_startup(self):
        """重启恢复：把所有含未完成回执的任务重新核对一遍（只处理 pending）。"""
        recovered = []
        for job in self.repository.list_jobs():
            pending = self.repository.list_receipts(job_no=job["job_no"], state="pending")
            if not pending:
                continue
            from .domain import Actor
            system = Actor("system-recovery", "registry")
            for receipt in pending:
                self._recheck_receipt(system, receipt)
            recovered.append(job["job_no"])
        return recovered

    def list_pending(self):
        return self.repository.list_receipts(state="pending")

    def get_job_view(self, job_no):
        if not self.repository.get_job(job_no):
            raise NotFoundError("registry job not found: " + job_no)
        return self._job_view(job_no)

    def list_jobs(self):
        return [self._job_view(job["job_no"]) for job in self.repository.list_jobs()]

    def _refresh_job_status(self, conn, job_no):
        counts = self.repository.receipt_state_counts(job_no, conn=conn)
        pending = counts.get("pending", 0)
        if pending:
            status = "has_pending"
        elif counts:
            status = "done"
        else:
            status = "open"
        self.repository.touch_job(job_no, status, conn=conn)

    def _job_view(self, job_no, summary=None):
        job = self.repository.get_job(job_no)
        receipts = self.repository.list_receipts(job_no=job_no)
        links = {
            item["receipt_no"]: item.get("duplicate_of")
            for item in self.repository.list_job_links(job_no)
        }
        counts = self.repository.receipt_state_counts(job_no)
        items = []
        for receipt in receipts:
            view = dict(receipt)
            view["conflict_reason_text"] = (
                rules.CONFLICT_LABELS.get(receipt["conflict_reason"])
                if receipt["conflict_reason"] else None
            )
            items.append(view)
        duplicate_links = [
            {"receipt_no": receipt_no, "duplicate_of": dup_of}
            for receipt_no, dup_of in links.items()
            if dup_of
        ]
        return {
            "job": job,
            "counts": counts,
            "pending": counts.get("pending", 0),
            "receipts": items,
            "duplicates": duplicate_links,
            "summary": summary or {},
        }

    # ------------------------------------------------------------------
    def _require_permit(self, permit_no, conn=None):
        permit = self.repository.get_permit(permit_no, conn=conn)
        if not permit:
            raise NotFoundError("permit not found: " + str(permit_no))
        return permit
