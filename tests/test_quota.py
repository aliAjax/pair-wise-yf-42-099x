import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class QuotaLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.repo = SQLiteRepository(self.db_path)
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.coord = Actor("coord", "coordinator")

    def tearDown(self):
        self.tmp.cleanup()

    def _animals(self):
        sire = self.service.create(self.admin, "animal", {"name": "M", "sex": "male"})
        dam = self.service.create(self.admin, "animal", {"name": "F", "sex": "female"})
        return sire, dam

    def _license(self, license_no="L1", species="tiger", year=2026, quota=2):
        return self.service.create(
            self.admin,
            "license",
            {"license_no": license_no, "species": species, "year": year, "quota": quota},
        )

    def _approve(self, sire, dam, license_no, actor=None):
        pairing = self.service.create(actor or self.admin, "pairing", {"proposed_by": "c"})
        return self.service.transition(
            actor or self.admin,
            pairing["id"],
            "approve",
            {
                "sire_id": sire["id"],
                "dam_id": dam["id"],
                "approvals": ["vet-1"],
                "license_no": license_no,
            },
        )

    def test_license_creation_and_uniqueness(self):
        lic = self._license(quota=3)
        self.assertEqual(lic["status"], "active")
        self.assertEqual(lic["data"]["quota"], 3)
        with self.assertRaises(ConflictError):
            self._license(license_no="L1", quota=5)

    def test_approval_occupies_quota_and_full_queues(self):
        sire, dam = self._animals()
        lic = self._license(quota=2)
        first = self._approve(sire, dam, "L1")
        second = self._approve(sire, dam, "L1")
        self.assertEqual(first["status"], "approved")
        self.assertEqual(second["status"], "approved")
        third = self._approve(sire, dam, "L1")
        self.assertEqual(third["status"], "queued")
        self.assertEqual(third["data"]["queue_position"], 1)
        used = self.repo.count_used(lic["id"])
        self.assertEqual(used["occupied"], 2)
        self.assertEqual(used["executed"], 0)

    def test_concurrent_approvals_only_one_per_slot(self):
        sire, dam = self._animals()
        self._license(quota=1)
        barrier = threading.Barrier(6)
        results = []

        def worker():
            pairing = self.service.create(self.coord, "pairing", {"proposed_by": "c"})
            barrier.wait()
            entity = self.service.transition(
                self.coord,
                pairing["id"],
                "approve",
                {
                    "sire_id": sire["id"],
                    "dam_id": dam["id"],
                    "approvals": ["vet-1"],
                    "license_no": "L1",
                },
            )
            results.append(entity["status"])

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count("approved"), 1)
        self.assertEqual(results.count("queued"), 5)
        lic = self.repo.find_license_by_no("L1")
        self.assertEqual(self.repo.count_used(lic["id"])["occupied"], 1)

    def test_downgrade_voids_unexecuted_and_retains_completed(self):
        sire, dam = self._animals()
        lic = self._license(quota=3)
        p1 = self._approve(sire, dam, "L1")
        p2 = self._approve(sire, dam, "L1")
        self.service.transition(self.admin, p1["id"], "complete", {"offspring_ids": ["o1"]})
        self.service.transition(self.admin, lic["id"], "adjust", {"quota": 1})
        self.assertEqual(self.service.get(p1["id"])["status"], "completed")
        self.assertEqual(self.service.get(p2["id"])["status"], "voided")
        self.assertIn("downgraded", self.service.get(p2["id"])["data"]["void_reason"])
        ledger = {e["pairing_id"]: e["status"] for e in self.repo.list_ledger(lic["id"])}
        self.assertEqual(ledger[p1["id"]], "executed")
        self.assertEqual(ledger[p2["id"]], "voided")

    def test_withdraw_voids_unexecuted_approvals(self):
        sire, dam = self._animals()
        lic = self._license(quota=2)
        p1 = self._approve(sire, dam, "L1")
        p2 = self._approve(sire, dam, "L1")
        self.service.transition(self.admin, lic["id"], "withdraw", {})
        self.assertEqual(self.service.get(p1["id"])["status"], "voided")
        self.assertEqual(self.service.get(p2["id"])["status"], "voided")
        self.assertEqual(self.service.get(lic["id"])["status"], "withdrawn")

    def test_expire_voids_unexecuted_approvals(self):
        sire, dam = self._animals()
        lic = self._license(quota=2)
        p1 = self._approve(sire, dam, "L1")
        self.service.transition(self.admin, lic["id"], "expire", {})
        self.assertEqual(self.service.get(p1["id"])["status"], "voided")
        self.assertEqual(self.service.get(lic["id"])["status"], "expired")

    def test_queue_recalculates_when_slot_frees(self):
        sire, dam = self._animals()
        lic = self._license(quota=1)
        p1 = self._approve(sire, dam, "L1")
        queued = self._approve(sire, dam, "L1")
        self.assertEqual(queued["status"], "queued")
        # rejecting the occupied pairing frees a slot and promotes the queued one
        self.service.transition(self.admin, p1["id"], "reject", {"reason": "cancelled"})
        promoted = self.service.get(queued["id"])
        self.assertEqual(promoted["status"], "approved")

    def test_reconcile_duplicate_is_idempotent(self):
        self._license(quota=5)
        result = self.service.reconcile(
            self.admin,
            [{"license_no": "L1", "species": "tiger", "year": 2026, "quota": 5}],
        )
        self.assertEqual(result["results"][0]["result"], "duplicate")
        self.assertEqual(len(self.service.list("license")), 1)

    def test_reconcile_new_license_is_created(self):
        result = self.service.reconcile(
            self.admin,
            [{"license_no": "L2", "species": "lion", "year": 2026, "quota": 8}],
        )
        self.assertEqual(result["results"][0]["result"], "created")
        self.assertIsNotNone(self.repo.find_license_by_no("L2"))

    def test_reconcile_conflict_leaves_pending_item(self):
        sire, dam = self._animals()
        lic = self._license(quota=5)
        for _ in range(4):
            self._approve(sire, dam, "L1")
        result = self.service.reconcile(
            self.admin,
            [{"license_no": "L1", "species": "tiger", "year": 2026, "quota": 3}],
        )
        self.assertEqual(result["results"][0]["result"], "pending")
        pending = self.service.list("pending_item")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["status"], "pending")
        self.assertIn("conflicts", pending[0]["data"]["reason"])

    def test_reconcile_failure_keeps_unfinished_and_retries_only_them(self):
        result = self.service.reconcile(
            self.admin,
            [
                {"license_no": "L2", "species": "lion", "year": 2026, "quota": 8},
                {"license_no": "L3", "species": "lion", "year": "bad", "quota": 8},
                {"license_no": "L4", "species": "lion", "year": 2026, "quota": 8},
            ],
        )
        self.assertEqual(result["results"][0]["result"], "created")
        self.assertEqual(result["results"][1]["result"], "failed")
        # L4 was interrupted by the failure and persisted as a pending item
        pending_after = self.service.list("pending_item")
        self.assertEqual(len(pending_after), 2)
        self.assertEqual(
            {p["data"]["license_no"] for p in pending_after}, {"L3", "L4"}
        )
        # retry: L3 still fails, L4 gets created; resolved L2 is not re-processed
        retry = self.service.retry_unfinished(self.admin)
        outcomes = {r["result"] for r in retry["results"]}
        self.assertIn("failed", outcomes)
        self.assertIn("created", outcomes)
        licenses = {l["data"]["license_no"] for l in self.service.list("license")}
        self.assertEqual(licenses, {"L2", "L4"})

    def test_reconcile_survives_restart(self):
        self.service.reconcile(
            self.admin,
            [
                {"license_no": "L2", "species": "lion", "year": 2026, "quota": 8},
                {"license_no": "L3", "species": "lion", "year": "bad", "quota": 8},
            ],
        )
        # simulate restart: new repository/service on the same database file
        restarted = DomainService(SQLiteRepository(self.db_path), RuleEngine())
        pending = restarted.list("pending_item")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["data"]["license_no"], "L3")
        self.assertEqual(pending[0]["status"], "failed")

    def test_resolve_pending_applies_license(self):
        sire, dam = self._animals()
        lic = self._license(quota=5)
        for _ in range(4):
            self._approve(sire, dam, "L1")
        self.service.reconcile(
            self.admin,
            [{"license_no": "L1", "species": "tiger", "year": 2026, "quota": 3}],
        )
        pending = self.service.list("pending_item")[0]
        resolved = self.service.transition(self.admin, pending["id"], "resolve", {})
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(self.repo.count_used(lic["id"])["occupied"], 3)


if __name__ == "__main__":
    unittest.main()
