from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        kind = self.rules.normalize_kind(entity["kind"])

        if kind == "pairing" and action == "approve" and data and data.get("license_no"):
            return self._approve_with_quota(actor, entity, data, expected)

        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )

        if kind == "pairing" and action == "complete":
            self.repository.mark_executed(entity_id)
        if kind == "pairing" and action == "reject" and entity["status"] in ("queued", "approved"):
            self.repository.void_occupation_for_pairing(entity_id, "pairing rejected")
            species = entity["data"].get("species")
            year = entity["data"].get("year")
            if species and year:
                self._recalc_queue(actor, species, year)
        if kind == "license" and action in ("expire", "withdraw", "adjust"):
            self._on_license_changed(actor, updated, action, data.get("quota"))
        if kind == "pending_item" and action == "resolve":
            self._apply_pending_resolution(actor, entity)

        return updated

    def _approve_with_quota(self, actor, entity, data, expected):
        license_no = data.get("license_no")
        lic = self.repository.find_license_by_no(license_no)
        if not lic:
            raise ValidationError("unknown license_no: " + str(license_no))
        next_status, patch = self.rules.validate_transition(
            actor, entity, "approve", dict(data), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        merged["license_no"] = lic["data"].get("license_no")
        merged["species"] = lic["data"].get("species")
        merged["year"] = lic["data"].get("year")
        updated, info = self.repository.occupy_pairing(
            entity["id"], expected, lic["id"], merged, queue_if_full=True
        )
        self.audit.record(
            entity["id"],
            actor,
            "approve",
            entity["status"],
            updated["status"],
            {
                "patch": patch,
                "license_no": info["license_no"],
                "queued": info["queued"],
                "remaining": info["remaining"],
            },
        )
        return updated

    def _on_license_changed(self, actor, license, action, new_quota):
        license_id = license["id"]
        species = license["data"].get("species")
        year = license["data"].get("year")
        voided_ids = []
        if action in ("expire", "withdraw"):
            reason = "license %s: %s" % (action, license["data"].get("license_no"))
            voided_ids = self.repository.void_occupations(license_id, None, reason)
        elif action == "adjust":
            target_quota = int(new_quota)
            used = self.repository.count_used(license_id)
            excess = (used["occupied"] + used["executed"]) - target_quota
            if excess > 0:
                reason = "license quota downgraded to %s" % target_quota
                voided_ids = self.repository.void_occupations(license_id, excess, reason)
        for pairing_id in voided_ids:
            self.repository.void_pairing(pairing_id, reason)
            self.audit.record(
                pairing_id, actor, "void", "approved", "voided", {"reason": reason}
            )
        if species and year is not None:
            self._recalc_queue(actor, species, year)

    def _recalc_queue(self, actor, species, year):
        queued = self.repository.list_queued_pairings(species, year)
        licenses = self.repository.list_active_licenses(species, year)
        for pairing in queued:
            for lic in licenses:
                updated, info = self.repository.occupy_pairing(
                    pairing["id"], None, lic["id"], pairing["data"], queue_if_full=False
                )
                if info["remaining"] > 0:
                    self.audit.record(
                        pairing["id"],
                        actor,
                        "approve",
                        "queued",
                        "approved",
                        {
                            "reason": "queue recalculated",
                            "license_no": info["license_no"],
                        },
                    )
                    break

    def _apply_pending_resolution(self, actor, pending):
        returned = pending["data"].get("returned") or {}
        license_no = returned.get("license_no")
        if not license_no:
            raise ValidationError("pending item has no returned license_no")
        lic = self.repository.find_license_by_no(license_no)
        if not lic:
            lic = self.create(
                actor,
                "license",
                {
                    "license_no": license_no,
                    "species": returned.get("species"),
                    "year": returned.get("year"),
                    "quota": returned.get("quota"),
                },
            )
            return
        new_data = dict(lic["data"])
        new_data["species"] = returned.get("species", new_data.get("species"))
        new_data["year"] = returned.get("year", new_data.get("year"))
        new_data["quota"] = returned.get("quota", new_data.get("quota"))
        updated = self.repository.update_entity(lic["id"], lic["version"], "active", new_data)
        self._on_license_changed(actor, updated, "adjust", int(new_data["quota"]))

    # ------------------------------------------------------------------
    # 外部登记系统对账
    # ------------------------------------------------------------------
    def reconcile(self, actor, items):
        results = []
        for index, item in enumerate(items or []):
            try:
                result = self._reconcile_one(actor, item)
                results.append({"license_no": str(item.get("license_no")), "result": result})
            except ConflictError as exc:
                pending = self._create_pending(actor, item, str(exc), "pending")
                results.append(
                    {
                        "license_no": str(item.get("license_no")),
                        "result": "pending",
                        "pending_id": pending["id"],
                        "reason": str(exc),
                    }
                )
            except Exception as exc:
                pending = self._create_pending(actor, item, str(exc), "failed")
                results.append(
                    {
                        "license_no": str(item.get("license_no")),
                        "result": "failed",
                        "pending_id": pending["id"],
                        "reason": str(exc),
                    }
                )
                for rest in (items or [])[index + 1:]:
                    self._create_pending(actor, rest, "batch interrupted by previous failure", "pending")
                break
        return {"results": results}

    def _reconcile_one(self, actor, item):
        license_no = item.get("license_no")
        if license_no is None or str(license_no).strip() == "":
            raise ValidationError("license_no is required")
        license_no = str(license_no).strip()
        species = item.get("species")
        if species is None or str(species).strip() == "":
            raise ValidationError("species is required")
        species = str(species).strip()
        try:
            year = int(item.get("year"))
        except (TypeError, ValueError):
            raise ValidationError("year must be an integer")
        try:
            quota = int(item.get("quota"))
        except (TypeError, ValueError):
            raise ValidationError("quota must be an integer")
        if quota < 0:
            raise ValidationError("quota must be non-negative")

        existing = self.repository.find_license_by_no(license_no)
        if not existing:
            self.create(
                actor,
                "license",
                {"license_no": license_no, "species": species, "year": year, "quota": quota},
            )
            return "created"

        same_species = existing["data"].get("species") == species
        same_year = int(existing["data"].get("year", 0)) == year
        same_quota = int(existing["data"].get("quota", 0)) == quota
        if same_species and same_year and same_quota:
            return "duplicate"

        if not same_species or not same_year:
            raise ConflictError(
                "license identity mismatch for %s: returned %s/%s, existing %s/%s"
                % (
                    license_no,
                    species,
                    year,
                    existing["data"].get("species"),
                    existing["data"].get("year"),
                )
            )

        used = self.repository.count_used(existing["id"])
        used_total = used["occupied"] + used["executed"]
        if quota < used_total:
            raise ConflictError(
                "late receipt conflicts with current occupation: returned quota %s < used %s"
                % (quota, used_total)
            )

        new_data = dict(existing["data"])
        new_data["quota"] = quota
        updated = self.repository.update_entity(
            existing["id"], existing["version"], existing["status"], new_data
        )
        self._on_license_changed(actor, updated, "adjust", quota)
        return "updated"

    def _create_pending(self, actor, item, reason, status):
        payload = item if isinstance(item, dict) else {}
        data = {
            "license_no": str(payload.get("license_no", "")),
            "species": payload.get("species"),
            "year": payload.get("year"),
            "quota": payload.get("quota"),
            "reason": reason,
            "returned": payload,
        }
        entity = self.repository.create_entity(
            str(uuid4()), "pending_item", status, data, actor.user_id
        )
        self.audit.record(
            entity["id"],
            actor,
            "reconcile",
            None,
            status,
            {"reason": reason, "license_no": data["license_no"]},
        )
        return entity

    def retry_unfinished(self, actor):
        unfinished = [
            item
            for item in self.repository.list_entities(kind="pending_item")
            if item["status"] in ("pending", "failed")
        ]
        results = []
        for pending in unfinished:
            try:
                result = self._reconcile_one(actor, pending["data"].get("returned") or {})
                self.repository.update_entity(
                    pending["id"], pending["version"], "resolved", pending["data"]
                )
                self.audit.record(
                    pending["id"], actor, "retry", pending["status"], "resolved",
                    {"result": result},
                )
                results.append({"pending_id": pending["id"], "result": result})
            except ConflictError as exc:
                new_data = dict(pending["data"])
                new_data["reason"] = str(exc)
                self.repository.update_entity(
                    pending["id"], pending["version"], "pending", new_data
                )
                results.append(
                    {"pending_id": pending["id"], "result": "pending", "reason": str(exc)}
                )
            except Exception as exc:
                new_data = dict(pending["data"])
                new_data["reason"] = str(exc)
                self.repository.update_entity(
                    pending["id"], pending["version"], "failed", new_data
                )
                results.append(
                    {"pending_id": pending["id"], "result": "failed", "reason": str(exc)}
                )
        return {"results": results}

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def license_usage(self, license_id):
        lic = self.repository.get_entity(license_id)
        if not lic or lic["kind"] != "license":
            raise NotFoundError("license not found: " + license_id)
        used = self.repository.count_used(license_id)
        quota = int(lic["data"].get("quota", 0))
        result = dict(lic)
        result["used"] = used
        result["remaining"] = quota - used["occupied"] - used["executed"]
        return result

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
