import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from src.quota import QuotaService
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class QuotaCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        rules = RuleEngine()
        self.svc = DomainService(self.repo, rules)
        self.q = QuotaService(self.repo, self.svc.audit)
        self.admin = Actor("admin", "admin")
        self.registrar = Actor("reg", "registrar")
        self.coordinator = Actor("coord", "coordinator")
        self.registry = Actor("ext", "registry")

    def tearDown(self):
        self.tmp.cleanup()

    def _permit(self, permit_no="P-1", species="tiger", year=2026, quota=1,
                valid_from="2026-01-01", valid_to="2026-12-31"):
        return self.q.issue_permit(self.admin, {
            "permit_no": permit_no, "species": species, "year": year,
            "quota": quota, "valid_from": valid_from, "valid_to": valid_to,
        })

    def _animals(self):
        sire = self.svc.create(
            self.registrar, "animal", {"name": "sire", "sex": "male"}
        )
        dam = self.svc.create(
            self.registrar, "animal", {"name": "dam", "sex": "female"}
        )
        return sire["id"], dam["id"]

    def _propose_and_approve(self, permit_no, sire_id, dam_id, actor=None):
        actor = actor or self.coordinator
        pairing = self.q.propose_pairing(actor, {
            "permit_no": permit_no, "species": "tiger",
        })
        return self.q.approve_pairing(actor, pairing["id"], {
            "sire_id": sire_id, "dam_id": dam_id,
        })

    # ------------------------------------------------------------------
    def test_permit_corresponds_to_species_and_year(self):
        permit = self._permit()
        self.assertEqual(permit["status"], "active")
        self.assertEqual(permit["version"], 1)
        with self.assertRaises(ConflictError):
            self._permit()  # 同一年度同一物种不能重复发证
        with self.assertRaises(PermissionDenied):
            self.q.issue_permit(Actor("v", "viewer"), {
                "permit_no": "P-X", "species": "lion", "year": 2026,
                "quota": 1, "valid_from": "2026-01-01",
                "valid_to": "2026-12-31",
            })

    def test_concurrent_approvals_only_one_takes_slot_rest_queue(self):
        self._permit(quota=1)
        sire_id, dam_id = self._animals()
        pairings = [
            self.q.propose_pairing(self.coordinator, {
                "permit_no": "P-1", "species": "tiger",
            }) for _ in range(4)
        ]
        results = [None] * len(pairings)
        errors = []

        def worker(index):
            try:
                results[index] = self.q.approve_pairing(
                    self.coordinator, pairings[index]["id"],
                    {"sire_id": sire_id, "dam_id": dam_id},
                )
            except Exception as exc:  # pragma: no cover - 并发不应产生异常
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertFalse(errors)
        statuses = sorted(r["status"] for r in results)
        self.assertEqual(statuses, ["approved", "queued", "queued", "queued"])
        approved = [r for r in results if r["status"] == "approved"]
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["data"]["slot"], 1)
        ledger = self.q.permit_ledger("P-1")
        self.assertEqual(ledger["approved"], 1)
        self.assertEqual(ledger["queued"], 3)
        self.assertEqual(ledger["remaining"], 0)
        # 排队位次从 1 开始，后来者不插队
        positions = sorted(
            r["data"]["queue_position"]
            for r in results if r["status"] == "queued"
        )
        self.assertEqual(positions, [1, 2, 3])

    def test_completed_keeps_slot_queue_promotes_when_unexecuted_voided(self):
        self._permit(quota=2)
        sire_id, dam_id = self._animals()
        first = self._propose_and_approve("P-1", sire_id, dam_id)
        second = self._propose_and_approve("P-1", sire_id, dam_id)
        third = self._propose_and_approve("P-1", sire_id, dam_id)
        self.assertEqual(third["status"], "queued")
        # 完成的结果永久占名额，不释放
        self.q.execute_pairing(self.coordinator, first["id"],
                               {"offspring_ids": ["baby-1"]})
        self.assertEqual(self.repo.get_entity(third["id"])["status"], "queued")
        ledger = self.q.permit_ledger("P-1")
        self.assertEqual((ledger["executed"], ledger["approved"], ledger["queued"]),
                         (1, 1, 1))
        # 未执行的批准被取消后空出名额，排队队首自动递补
        self.q.reject_pairing(self.coordinator, second["id"], {"reason": "cancel"})
        promoted = self.repo.get_entity(third["id"])
        self.assertEqual(promoted["status"], "approved")
        self.assertEqual(promoted["data"]["slot"], 2)
        self.assertEqual(promoted["data"]["void_reason"], None)

    def test_old_approvals_remain_after_permit_amendment(self):
        self._permit(quota=2)
        sire_id, dam_id = self._animals()
        pairing = self._propose_and_approve("P-1", sire_id, dam_id)
        amended = self.q.amend_permit(self.admin, "P-1", {"notes": "paperwork renewed"})
        self.assertEqual(amended["version"], 2)
        # 旧批准依旧可以完成，不需要重新批准
        ledger = self.q.execute_pairing(
            self.coordinator, pairing["id"], {"offspring_ids": ["baby"]}
        )
        self.assertEqual(ledger["executed"], 1)
        # 变更不能夹带名额/状态修改
        with self.assertRaises(ValidationError):
            self.q.amend_permit(self.admin, "P-1", {"quota": 9})

    def test_quota_reduction_voids_unexecuted_and_recomputes_queue(self):
        self._permit(quota=3)
        sire_id, dam_id = self._animals()
        ids = [
            self._propose_and_approve("P-1", sire_id, dam_id)["id"]
            for _ in range(5)
        ]
        ledger = self.q.permit_ledger("P-1")
        self.assertEqual((ledger["approved"], ledger["queued"]), (3, 2))
        ledger = self.q.adjust_quota(self.admin, "P-1", 1)
        statuses = {item["id"]: item["status"] for item in ledger["pairings"]}
        self.assertEqual(sum(1 for s in statuses.values() if s == "approved"), 1)
        self.assertEqual(sum(1 for s in statuses.values() if s == "voided"), 2)
        self.assertEqual(sum(1 for s in statuses.values() if s == "queued"), 2)
        # 被挤掉的批准页面能看清失效原因
        voided = [p for p in ledger["pairings"] if p["status"] == "voided"]
        for item in voided:
            self.assertEqual(item["void_reason"], "quota_reduced")
            self.assertTrue(item["void_reason_text"])
        # 上调后排队项递补，已失效的不恢复
        ledger = self.q.adjust_quota(self.admin, "P-1", 3)
        self.assertEqual(ledger["approved"], 3)
        self.assertEqual(ledger["queued"], 0)
        self.assertEqual(ledger["voided"], 2)

    def test_expiry_and_withdrawal_void_pending_but_keep_completed(self):
        self._permit(quota=3)
        sire_id, dam_id = self._animals()
        done = self._propose_and_approve("P-1", sire_id, dam_id)
        open_pairing = self._propose_and_approve("P-1", sire_id, dam_id)
        waiting = self._propose_and_approve("P-1", sire_id, dam_id)
        self.q.execute_pairing(self.coordinator, done["id"],
                               {"offspring_ids": ["x"]})
        release = self.q.create_release(self.registrar, {
            "pairing_id": done["id"],
            "from_institution": "Zoo-A", "to_institution": "Zoo-B",
        })

        ledger = self.q.withdraw_permit(self.admin, "P-1")
        self.assertEqual(ledger["permit"]["status"], "withdrawn")
        self.assertEqual(ledger["executed"], 1)
        self.assertEqual(ledger["approved"], 0)
        self.assertEqual(ledger["queued"], 0)
        self.assertEqual(ledger["voided"], 2)
        for item in ledger["pairings"]:
            if item["status"] == "voided":
                self.assertEqual(item["void_reason"], "permit_withdrawn")
        # 已完成结果和运输放行都保留
        self.assertEqual(self.repo.get_entity(done["id"])["status"], "executed")
        self.assertEqual(
            self.repo.get_entity(release["id"])["status"], "released"
        )
        # 撤回后不能再批准新配对
        new_pairing = self.q.propose_pairing
        with self.assertRaises(InvalidTransition):
            proposal = self.q.propose_pairing(self.coordinator, {
                "permit_no": "P-1", "species": "tiger",
            })
        # 重复撤回报错
        with self.assertRaises(InvalidTransition):
            self.q.withdraw_permit(self.admin, "P-1")

    def test_expired_by_date_range_rejects_new_approval(self):
        self._permit(valid_from="2020-01-01", valid_to="2020-12-31")
        sire_id, dam_id = self._animals()
        with self.assertRaises(InvalidTransition):
            self._propose_and_approve("P-1", sire_id, dam_id)

    def test_transport_release_is_separate_from_approval(self):
        self._permit(quota=1)
        sire_id, dam_id = self._animals()
        pairing = self._propose_and_approve("P-1", sire_id, dam_id)
        # 仅批准未完成不能放行
        with self.assertRaises(InvalidTransition):
            self.q.create_release(self.registrar, {
                "pairing_id": pairing["id"],
                "from_institution": "A", "to_institution": "B",
            })
        self.q.execute_pairing(self.coordinator, pairing["id"], {})
        release = self.q.create_release(self.registrar, {
            "pairing_id": pairing["id"],
            "from_institution": "A", "to_institution": "B",
        })
        self.assertEqual(release["kind"], "release")
        self.assertEqual(release["data"]["permit_snapshot"]["permit_no"], "P-1")
        with self.assertRaises(PermissionDenied):
            self.q.create_release(Actor("c", "coordinator"), {
                "pairing_id": pairing["id"],
                "from_institution": "A", "to_institution": "B",
            })

    def test_receipts_reconcile_by_number_and_keep_pending_conflicts(self):
        self._permit(quota=2)
        sire_id, dam_id = self._animals()
        pairing = self._propose_and_approve("P-1", sire_id, dam_id)
        self.q.execute_pairing(self.coordinator, pairing["id"], {})
        job = self.q.open_registry_job(self.registry)["job_no"]
        view = self.q.receive_receipts(self.registry, job, [
            {"receipt_no": "R-OK", "permit_no": "P-1", "occupied": 1,
             "status": "active"},
            {"receipt_no": "R-MISSING", "permit_no": "P-NOPE", "occupied": 1,
             "status": "active"},
            {"receipt_no": "R-LATE", "permit_no": "P-1", "occupied": 9,
             "status": "active"},
        ])
        self.assertEqual(view["job"]["status"], "has_pending")
        by_no = {r["receipt_no"]: r for r in view["receipts"]}
        self.assertEqual(by_no["R-OK"]["state"], "accepted")
        self.assertEqual(by_no["R-MISSING"]["state"], "pending")
        self.assertEqual(by_no["R-MISSING"]["conflict_reason"], "permit_unknown")
        self.assertEqual(by_no["R-LATE"]["conflict_reason"],
                         "occupied_exceeds_quota")
        self.assertTrue(by_no["R-LATE"]["conflict_reason_text"])

        # 外部系统重复回传同一编号：按编号对账，不重新处理，只记重复关联
        again = self.q.receive_receipts(self.registry, job, [
            {"receipt_no": "R-OK", "permit_no": "P-1", "occupied": 1,
             "status": "active"},
        ])
        self.assertEqual(again["counts"].get("accepted"), 1)
        self.assertEqual(len(again["duplicates"]), 1)

    def test_retry_only_unfinished_and_survives_restart(self):
        self._permit(quota=1)
        job = self.q.open_registry_job(self.registry)["job_no"]
        self.q.receive_receipts(self.registry, job, [
            {"receipt_no": "R-1", "permit_no": "P-1", "occupied": 5,
             "status": "active"},
        ])
        # 重试时冲突仍在：保留未完成项
        view = self.q.retry_pending(self.registry, job)
        self.assertEqual(view["summary"]["still_pending"], 1)
        self.assertEqual(view["pending"], 1)

        # 模拟“重启”：新建 service/repository 指向同一个库
        repo2 = SQLiteRepository(self.repo.path)
        svc2 = DomainService(repo2, RuleEngine())
        q2 = QuotaService(repo2, svc2.audit)
        recovered = q2.recover_on_startup()
        self.assertEqual(recovered, [job])

        # 冲突解除后（把名额调到足够大）再重试，只续做未完成项
        q2.adjust_quota(self.admin, "P-1", 10)
        view = q2.retry_pending(self.registry, job)
        self.assertEqual(view["summary"]["retried"], 1)
        self.assertEqual(view["summary"]["accepted_now"], 1)
        self.assertEqual(view["job"]["status"], "done")

    def test_conflict_resolution_modes(self):
        self._permit(quota=1)
        job = self.q.open_registry_job(self.registry)["job_no"]
        self.q.receive_receipts(self.registry, job, [
            {"receipt_no": "R-1", "permit_no": "P-1", "occupied": 2,
             "status": "active"},
        ])
        with self.assertRaises(PermissionDenied):
            self.q.resolve_conflict(Actor("v", "viewer"), "R-1", "accept")
        resolved = self.q.resolve_conflict(
            self.admin, "R-1", "exempt", note="外部口径含历史占用"
        )
        self.assertEqual(resolved["state"], "resolved_exempt")
        self.assertEqual(self.q.get_job_view(job)["job"]["status"], "done")
        with self.assertRaises(InvalidTransition):
            self.q.resolve_conflict(self.admin, "R-1", "accept")

    def test_quota_entries_form_replayable_ledger(self):
        self._permit(quota=2)
        sire_id, dam_id = self._animals()
        first = self._propose_and_approve("P-1", sire_id, dam_id)
        second = self._propose_and_approve("P-1", sire_id, dam_id)
        third = self._propose_and_approve("P-1", sire_id, dam_id)
        self.q.execute_pairing(self.coordinator, first["id"], {})
        self.q.reject_pairing(self.coordinator, second["id"], {"reason": "cancel"})
        # 第三笔排队项因第二笔取消而递补
        self.assertEqual(self.repo.get_entity(third["id"])["status"], "approved")
        self.q.adjust_quota(self.admin, "P-1", 0)
        entries = self.q.permit_ledger("P-1")["entries"]
        kinds = [e["kind"] for e in entries]
        self.assertIn("reserve", kinds)
        self.assertIn("enqueue", kinds)
        self.assertIn("execute", kinds)
        self.assertIn("promote", kinds)
        self.assertIn("void", kinds)
        # 每笔流水都有记账后余额
        for entry in entries:
            self.assertGreaterEqual(entry["balance_after"], 0)


if __name__ == "__main__":
    unittest.main()
