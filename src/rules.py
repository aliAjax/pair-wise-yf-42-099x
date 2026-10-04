from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_animal(actor, data, lookup):
    if data.get("sex") not in ("male", "female", "unknown"):
        raise ValidationError("sex must be male, female or unknown")


def inbreeding_coefficient(sire, dam):
    if not sire or not dam:
        return 1.0
    sire_id = sire.get("id")
    dam_id = dam.get("id")
    if sire_id is None or dam_id is None:
        return 0.0
    if sire_id == dam_id:
        return 0.5
    if sire.get("sire_id") == dam_id or dam.get("sire_id") == sire_id:
        return 0.25
    return 0.0


def _validate_pairing(actor, entity, data, lookup):
    sire = _find_one(lookup, "animal", "id", data.get("sire_id"))
    dam = _find_one(lookup, "animal", "id", data.get("dam_id"))
    if not sire or not dam:
        raise ValidationError("pairing requires two existing animals")
    if sire["status"] != "active" or dam["status"] != "active":
        raise ValidationError("pairing animals must be active")
    if inbreeding_coefficient(sire["data"], dam["data"]) > 0.125:
        raise ValidationError("pairing exceeds inbreeding threshold")
    return {"approved_by": actor.user_id}


CUSTOM_CREATE = {'animal': _validate_animal}
CUSTOM_TRANSITIONS = {('pairing', 'approve'): _validate_pairing}


class RuleEngine:
    ALIASES = {'animals': 'animal', 'pairings': 'pairing', 'transfers': 'transfer'}
    INITIAL_STATUS = {'animal': 'active', 'pairing': 'proposed', 'transfer': 'planned'}
    TRANSITIONS = {'animal': {'mark_deceased': (('active',), 'deceased'), 'quarantine_animal': (('active',), 'quarantined'), 'release_quarantine': (('quarantined',), 'active')}, 'pairing': {'approve': (('proposed',), 'approved'), 'reject': (('proposed',), 'rejected'), 'complete': (('approved',), 'completed')}, 'transfer': {'authorize': (('planned',), 'authorized'), 'ship': (('authorized',), 'in_transit'), 'arrive': (('in_transit',), 'completed')}}
    CREATE_REQUIRED = {'animal': ('name', 'sex'), 'pairing': ('proposed_by',), 'transfer': ('animal_id', 'from_institution', 'to_institution')}
    ACTION_REQUIRED = {('animal', 'mark_deceased'): ('cause',), ('animal', 'quarantine_animal'): ('reason',), ('pairing', 'approve'): ('sire_id', 'dam_id', 'approvals'), ('pairing', 'reject'): ('reason',), ('pairing', 'complete'): ('offspring_ids',), ('transfer', 'authorize'): ('permit_id',), ('transfer', 'ship'): ('transport_id',), ('transfer', 'arrive'): ('arrival_date',)}
    CREATE_ROLES = {'animal': ('admin', 'registrar'), 'pairing': ('admin', 'coordinator'), 'transfer': ('admin', 'registrar')}
    ROLE_ACTIONS = {'mark_deceased': ('admin', 'veterinarian'), 'quarantine_animal': ('admin', 'veterinarian'), 'release_quarantine': ('admin', 'veterinarian'), 'approve': ('admin', 'coordinator'), 'reject': ('admin', 'coordinator'), 'complete': ('admin', 'coordinator'), 'authorize': ('admin', 'registrar'), 'ship': ('admin', 'registrar'), 'arrive': ('admin', 'registrar')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()


# ---------------------------------------------------------------------------
# 繁育配额账（许可证 / 配对 / 对账）
#
# 约定：
# - 一条许可证对应一个物种、一个年度，名额按年度核定。
# - 配对是可恢复的配额占用：approved 占名额，queued 排队等名额，
#   executed 已完成（结果保留），voided 失效（带原因，释放名额）。
# - 批准与运输放行分开记录；许可证失效不影响已完成的结果。
# ---------------------------------------------------------------------------

PERMIT_ACTIVE = "active"
PERMIT_EXPIRED = "expired"
PERMIT_WITHDRAWN = "withdrawn"

PAIRING_PROPOSED = "proposed"
PAIRING_APPROVED = "approved"
PAIRING_QUEUED = "queued"
PAIRING_EXECUTED = "executed"
PAIRING_VOIDED = "voided"

# 失效原因
REASON_QUOTA_REDUCED = "quota_reduced"        # 许可证名额下调，超出部分自动失效
REASON_PERMIT_EXPIRED = "permit_expired"      # 许可证到期
REASON_PERMIT_WITHDRAWN = "permit_withdrawn"  # 许可证撤回
REASON_REJECTED = "rejected"                  # 人工拒绝

VOID_REASON_LABELS = {
    REASON_QUOTA_REDUCED: "许可证名额下调，未执行的批准超出剩余名额",
    REASON_PERMIT_EXPIRED: "许可证已到期，未执行的批准自动失效",
    REASON_PERMIT_WITHDRAWN: "许可证已撤回，未执行的批准自动失效",
    REASON_REJECTED: "配对建议被拒绝",
}


def today_ordinal(value=None):
    if value is None:
        return datetime.now(timezone.utc).date().toordinal()
    return _date_ordinal(value)


def validate_permit_payload(data, partial=False):
    payload = dict(data or {})
    required = ("permit_no", "species", "year", "quota", "valid_from", "valid_to")
    if not partial:
        _RuleStaticRequire(payload, required)
    if "permit_no" in payload and not str(payload["permit_no"]).strip():
        raise ValidationError("permit_no must not be empty")
    if "species" in payload and not str(payload["species"]).strip():
        raise ValidationError("species must not be empty")
    year = payload.get("year")
    if year is not None:
        year = int(year)
        if year < 1900 or year > 2999:
            raise ValidationError("year out of range")
        payload["year"] = year
    quota = payload.get("quota")
    if quota is not None:
        quota = int(quota)
        if quota < 0:
            raise ValidationError("quota must be >= 0")
        payload["quota"] = quota
    if "valid_from" in payload or "valid_to" in payload:
        start = _date_ordinal(payload["valid_from"])
        end = _date_ordinal(payload["valid_to"])
        if end < start:
            raise ValidationError("valid_to must not be before valid_from")
    return payload


class _RuleStaticRequire:
    def __new__(cls, data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)


def permit_is_active(permit, today=None):
    """许可证有效：状态为 active 且今天在有效期内（到期日含当天）。"""
    if not permit or permit["status"] != PERMIT_ACTIVE:
        return False
    day = today_ordinal(today)
    return _date_ordinal(permit["valid_from"]) <= day <= _date_ordinal(permit["valid_to"])


def validate_pairing_proposal_data(data, sire, dam, permit, today=None):
    """校验配对建议/批准所需数据，并核对物种、许可证有效性和亲缘阈值。"""
    _RuleStaticRequire(data, ("sire_id", "dam_id", "species"))
    if not sire or not dam:
        raise ValidationError("pairing requires two existing animals")
    if sire["status"] != "active" or dam["status"] != "active":
        raise ValidationError("pairing animals must be active")
    if inbreeding_coefficient(sire["data"], dam["data"]) > 0.125:
        raise ValidationError("pairing exceeds inbreeding threshold")
    if not permit:
        raise ValidationError("no permit for this species/year")
    if data["species"] != permit["species"]:
        raise ValidationError("pairing species does not match permit species")
    if not permit_is_active(permit, today):
        raise InvalidTransition(
            "permit %s is not active (status=%s)" % (permit["permit_no"], permit["status"])
        )
    return {"permit_no": permit["permit_no"], "species": permit["species"]}


def _ordered_open(entries):
    """按排队序号（提交先后）返回当前占名额与排队中的配对。"""
    open_entries = [
        e for e in entries
        if e["status"] in (PAIRING_APPROVED, PAIRING_QUEUED)
    ]
    return sorted(open_entries, key=lambda e: (e["seq"], e["pairing_id"]))


def decide_submit(entries, quota, new_pairing_id, executed=0):
    """提交一笔新批准时的占用决策（在持有写锁后调用）。

    已批准者按提交先后保留名额；若还有剩余名额，新配对占用下一个名额，
    否则进入排队。已排队者保持原顺序，不会被后来者插队。
    executed 个名额已被完成结果永久占用，批准序号从其后开始编号。
    返回 {pairing_id: {"status": ..., "slot": 序号或None, "position": 排队位次或None}}。
    """
    ordered = _ordered_open(entries)
    approved = [e for e in ordered if e["status"] == PAIRING_APPROVED]
    queued = [e for e in ordered if e["status"] == PAIRING_QUEUED]
    plan = {}
    if len(approved) + executed < quota:
        for slot, entry in enumerate(approved, 1):
            plan[entry["pairing_id"]] = {
                "status": PAIRING_APPROVED, "slot": entry.get("slot") or slot + executed,
                "position": None,
            }
        plan[new_pairing_id] = {
            "status": PAIRING_APPROVED, "slot": len(approved) + 1 + executed,
            "position": None,
        }
        for position, entry in enumerate(queued, 1):
            plan[entry["pairing_id"]] = {
                "status": PAIRING_QUEUED, "slot": None, "position": position,
            }
    else:
        for slot, entry in enumerate(approved, 1):
            plan[entry["pairing_id"]] = {
                "status": PAIRING_APPROVED, "slot": entry.get("slot") or slot,
                "position": None,
            }
        for position, entry in enumerate(queued, 1):
            plan[entry["pairing_id"]] = {
                "status": PAIRING_QUEUED, "slot": None, "position": position,
            }
        plan[new_pairing_id] = {
            "status": PAIRING_QUEUED, "slot": None, "position": len(queued) + 1,
        }
    return plan


def plan_recompute(entries, quota, executed=0):
    """名额调整后的统一重排。

    占名额者与排队者按提交先后合并排队，executed 个名额已被完成结果永久占用，
    取剩余容量内的配对继续占用（超出的已批准者自动失效，由调用方标注失效原因）；
    从未占用过名额的排队者若排进容量则递补。
    """
    capacity = max(0, quota - executed)
    combined = _ordered_open(entries)
    keep, drop = combined[:capacity], combined[capacity:]
    plan = {}
    for slot, entry in enumerate(keep, 1):
        promoted = entry["status"] == PAIRING_QUEUED
        plan[entry["pairing_id"]] = {
            "status": PAIRING_APPROVED,
            "slot": slot + executed,
            "position": None,
            "promoted": promoted,
        }
    for entry in drop:
        plan[entry["pairing_id"]] = {
            "status": PAIRING_VOIDED if entry["status"] == PAIRING_APPROVED
            else PAIRING_QUEUED,
            "slot": None,
            "demoted_from_approved": entry["status"] == PAIRING_APPROVED,
        }
    # 仍在排队的项重新编号
    queued_again = sorted(
        (item for item in drop if item["status"] == PAIRING_QUEUED),
        key=lambda e: (e["seq"], e["pairing_id"]),
    )
    for position, entry in enumerate(queued_again, 1):
        plan[entry["pairing_id"]]["position"] = position
    return plan


def plan_expire_all(entries):
    """许可证到期/撤回：所有未执行的批准与排队项全部失效，已完成结果保留。"""
    return {
        entry["pairing_id"]: {"status": PAIRING_VOIDED, "slot": None}
        for entry in entries
        if entry["status"] in (PAIRING_APPROVED, PAIRING_QUEUED)
    }


# 外部登记系统回执对账
RECEIPT_DUPLICATE = "duplicate"
RECEIPT_ACCEPTED = "accepted"
RECEIPT_CONFLICT = "conflict"

CONFLICT_PERMIT_UNKNOWN = "permit_unknown"
CONFLICT_STATUS = "status_conflict"
CONFLICT_OCCUPIED = "occupied_exceeds_quota"

CONFLICT_LABELS = {
    CONFLICT_PERMIT_UNKNOWN: "回执中的许可证编号在本地不存在",
    CONFLICT_STATUS: "晚到回执的许可证状态与当前状态冲突",
    CONFLICT_OCCUPIED: "回执占用数超过当前许可证名额",
}


def validate_receipt_payload(data):
    payload = dict(data or {})
    _RuleStaticRequire(payload, ("receipt_no", "permit_no", "occupied", "status"))
    occupied = int(payload["occupied"])
    if occupied < 0:
        raise ValidationError("occupied must be >= 0")
    payload["occupied"] = occupied
    if payload["status"] not in (PERMIT_ACTIVE, PERMIT_EXPIRED, PERMIT_WITHDRAWN):
        raise ValidationError("receipt status must be active, expired or withdrawn")
    return payload


def classify_receipt(receipt, permit, current_occupied):
    """按许可证编号对账。返回 (verdict, reason_or_None, detail)。

    晚到回执与现占用冲突（许可证不存在、状态不一致、占用数超名额）时，
    不落账，留给人工处理；重复编号由调用方先按 receipt_no 去重。
    """
    if permit is None:
        return RECEIPT_CONFLICT, CONFLICT_PERMIT_UNKNOWN, {
            "permit_no": receipt["permit_no"],
        }
    if receipt["status"] != permit["status"]:
        return RECEIPT_CONFLICT, CONFLICT_STATUS, {
            "permit_no": permit["permit_no"],
            "receipt_status": receipt["status"],
            "current_status": permit["status"],
        }
    if receipt["occupied"] > permit["quota"]:
        return RECEIPT_CONFLICT, CONFLICT_OCCUPIED, {
            "permit_no": permit["permit_no"],
            "receipt_occupied": receipt["occupied"],
            "quota": permit["quota"],
        }
    return RECEIPT_ACCEPTED, None, {"occupied": current_occupied}
