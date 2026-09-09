from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.db import AliasDB


class HomePoolStatsTests(unittest.TestCase):
    def test_counts_total_available_and_cdk_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = AliasDB(Path(temp_dir) / "aliases.db")
            rows = [
                ("plain@icloud.com", "Service", True),
                ("cdk-active@icloud.com", "CDK_ABCDEF12", True),
                ("cdk-inactive@icloud.com", "CDK_12345678", False),
                ("cdk-spaced@icloud.com", "  cdk_deadbeef  ", True),
            ]
            for hme, label, active in rows:
                db.upsert_alias(
                    account="owner@icloud.com",
                    hme=hme,
                    label=label,
                    is_active=active,
                )

            self.assertEqual(
                db.get_alias_pool_stats(),
                {"total": 4, "available": 3, "cdk_total": 3, "cdk_available": 2, "claimed": 0},
            )

    def test_empty_pool_returns_zeroes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stats = AliasDB(Path(temp_dir) / "aliases.db").get_alias_pool_stats()
            self.assertEqual(
                stats,
                {"total": 0, "available": 0, "cdk_total": 0, "cdk_available": 0, "claimed": 0},
            )


if __name__ == "__main__":
    unittest.main()
