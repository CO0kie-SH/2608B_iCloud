from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.client import ICloudError
from tools.db import AliasDB, utc_now
from tools.hme import HMEService
from tools.rate_limit import HME_ACCOUNT_ALIAS_LIMIT, HMEAccountAliasLimitError


class FakeClient:
    def __init__(self, *, fail_generate: bool = False) -> None:
        self.fail_generate = fail_generate

    def call_api(self, path: str, method: str = "GET", payload: dict | None = None) -> dict:
        if path.endswith("/generate"):
            if self.fail_generate:
                raise ICloudError("network error")
            return {"success": True, "result": {"hme": "generated@icloud.com"}}
        if path.endswith("/reserve"):
            return {
                "success": True,
                "result": {
                    "hme": {
                        "hme": "generated@icloud.com",
                        "label": str((payload or {}).get("label") or ""),
                        "isActive": True,
                        "anonymousId": "anonymous-id",
                    }
                },
            }
        raise AssertionError(f"unexpected path: {path}")


class HMECooldownTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = AliasDB(Path(self.temp_dir.name) / "aliases.db")
        self.account = "owner@icloud.com"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def assert_no_cooldown(self) -> None:
        quota = self.db.get_create_quota(self.account)
        self.assertEqual(quota.used, 0)
        self.assertEqual(quota.remaining, quota.limit)
        self.assertEqual(quota.retry_after_sec, 0)
        claim_id = self.db.claim_create_slot(self.account)
        self.db.release_create_claim(claim_id)

    def insert_aliases(self, count: int) -> None:
        now = utc_now()
        rows = [
            (self.account, self.account, f"alias-{index}@icloud.com", index % 2, now, now)
            for index in range(count)
        ]
        with self.db._connect() as conn:
            conn.executemany(
                """
                INSERT INTO aliases (
                    parent_mail, account, hme, is_active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()

    def test_network_failure_releases_claim_without_cooldown(self) -> None:
        service = HMEService(FakeClient(fail_generate=True), db=self.db)

        with self.assertRaisesRegex(ICloudError, "network error"):
            service.create_alias(self.account)

        self.assert_no_cooldown()

    def test_local_record_failure_releases_claim_without_cooldown(self) -> None:
        service = HMEService(FakeClient(), db=self.db)

        with patch.object(self.db, "record_create_event", side_effect=RuntimeError("db error")):
            with self.assertRaisesRegex(RuntimeError, "db error"):
                service.create_alias(self.account)

        self.assert_no_cooldown()

    def test_alias_persistence_failure_does_not_create_cooldown(self) -> None:
        service = HMEService(FakeClient(), db=self.db)

        with patch.object(self.db, "upsert_alias", side_effect=RuntimeError("write error")):
            with self.assertRaisesRegex(RuntimeError, "write error"):
                service.create_alias(self.account)

        self.assert_no_cooldown()

    def test_startup_cleanup_releases_abandoned_claims(self) -> None:
        self.db.claim_create_slot(self.account)

        self.assertEqual(self.db.release_all_create_claims(), 1)

        self.assert_no_cooldown()

    def test_740_aliases_reject_before_upstream_creation(self) -> None:
        self.insert_aliases(HME_ACCOUNT_ALIAS_LIMIT)
        service = HMEService(FakeClient(), db=self.db)

        with self.assertRaises(HMEAccountAliasLimitError) as raised:
            service.create_alias(self.account)

        self.assertEqual(raised.exception.code, "HME_ACCOUNT_LIMIT")
        self.assertEqual(raised.exception.alias_count, HME_ACCOUNT_ALIAS_LIMIT)
        self.assertEqual(self.db.get_create_quota(self.account).used, 0)

    def test_pending_claim_prevents_739_alias_concurrency_overflow(self) -> None:
        self.insert_aliases(HME_ACCOUNT_ALIAS_LIMIT - 1)
        claim_id = self.db.claim_create_slot(self.account)

        with self.assertRaises(HMEAccountAliasLimitError) as raised:
            self.db.claim_create_slot(self.account)

        self.assertEqual(raised.exception.alias_count, HME_ACCOUNT_ALIAS_LIMIT - 1)
        self.assertEqual(raised.exception.pending, 1)
        capacity = self.db.get_alias_capacity(self.account)
        self.assertFalse(capacity.allowed)
        self.assertEqual(capacity.remaining, 0)

        self.db.release_create_claim(claim_id)
        recovered = self.db.get_alias_capacity(self.account)
        self.assertTrue(recovered.allowed)
        self.assertEqual(recovered.remaining, 1)
        replacement = self.db.claim_create_slot(self.account)
        self.db.release_create_claim(replacement)


if __name__ == "__main__":
    unittest.main()
