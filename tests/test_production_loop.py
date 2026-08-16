from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from tools.db import AliasDB
from tools.production import ProductionResult
from tools.rate_limit import HMECreateRateLimitError
from web.jobs import get_production_job
from web.production_loop import ProductionLoopController
from web.production_service import submit_account_production


class OneRoundController(ProductionLoopController):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.waits: list[int] = []
        super().__init__(*args, **kwargs)

    def _wait(self, seconds: int) -> bool:
        self.waits.append(seconds)
        return False


class ProductionLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.db = AliasDB(self.base_dir / "aliases.db")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def account(self, name: str, *, ok: bool = True) -> SimpleNamespace:
        source = self.base_dir / f"{name}.yaml"
        source.touch(exist_ok=True)
        return SimpleNamespace(
            name=name,
            mail=name,
            ok=ok,
            source=str(source),
        )

    @staticmethod
    def done_job(job_id: str, _: AliasDB) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "status": "done",
            "result": {
                "created": 1,
                "items": [{"hme": f"{job_id}@icloud.com"}],
                "errors": [],
            },
            "error": "",
        }

    @staticmethod
    def wait_for_thread(controller: ProductionLoopController) -> None:
        thread = controller._thread
        if thread is not None:
            thread.join(timeout=3)
        if thread is not None and thread.is_alive():
            controller.shutdown()
            raise AssertionError("轮询测试线程未按预期结束")

    def controller(
        self,
        accounts: list[Any],
        *,
        submitter: Any,
        job_getter: Any | None = None,
        one_round: bool = True,
        clock: Any | None = None,
    ) -> ProductionLoopController:
        cls = OneRoundController if one_round else ProductionLoopController
        kwargs: dict[str, Any] = {
            "accounts_loader": lambda: accounts,
            "settings_loader": lambda: SimpleNamespace(),
            "submitter": submitter,
            "job_getter": job_getter or self.done_job,
            "poll_interval_sec": 0.01,
        }
        if clock is not None:
            kwargs["clock"] = clock
        return cls(self.db, **kwargs)

    def configure(
        self,
        controller: ProductionLoopController,
        selected: list[str],
        *,
        mode: str = "forever",
        duration_minutes: int = 0,
        interval_sec: int = 2,
    ) -> None:
        controller.configure(
            selected_accounts=selected,
            interface="legacy",
            mode=mode,
            duration_minutes=duration_minutes,
            interval_sec=interval_sec,
        )

    def test_database_defaults_and_progress_are_persistent(self) -> None:
        state = self.db.get_production_loop_state()
        self.assertFalse(state["enabled"])
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(state["selected_accounts"], [])

        self.db.update_production_loop_state(
            selected_accounts=["a", "b"],
            submitted=7,
            progress=[f"line-{index}" for index in range(250)],
        )
        reopened = AliasDB(self.base_dir / "aliases.db").get_production_loop_state()
        self.assertEqual(reopened["selected_accounts"], ["a", "b"])
        self.assertEqual(reopened["submitted"], 7)
        self.assertEqual(len(reopened["progress"]), 200)
        self.assertEqual(reopened["progress"][0], "line-50")

    def test_config_validation_and_active_config_lock(self) -> None:
        account = self.account("a@icloud.com")
        controller = self.controller([account], submitter=lambda *args, **kwargs: None)

        with self.assertRaisesRegex(ValueError, "未知账户"):
            self.configure(controller, ["missing@icloud.com"])
        with self.assertRaisesRegex(ValueError, "1-60"):
            self.configure(controller, [account.name], interval_sec=0)
        with self.assertRaisesRegex(ValueError, "至少勾选"):
            controller.start()

        self.configure(controller, [account.name])
        self.db.update_production_loop_state(enabled=True, status="running")
        self.configure(controller, [])
        self.assertEqual(self.db.get_production_loop_state()["selected_accounts"], [])
        with self.assertRaisesRegex(ValueError, "只允许修改参与账号"):
            self.configure(controller, [], interval_sec=3)

    def test_accounts_are_submitted_sequentially_and_counted(self) -> None:
        accounts = [self.account("b@icloud.com"), self.account("a@icloud.com")]
        submitted: list[str] = []

        def submitter(account: Any, **_: Any) -> SimpleNamespace:
            submitted.append(account.name)
            return SimpleNamespace(job_id=f"job-{account.name[0]}")

        controller = self.controller(accounts, submitter=submitter)
        self.configure(controller, [accounts[0].name, accounts[1].name])
        controller.start()
        self.wait_for_thread(controller)

        state = controller.snapshot()
        self.assertEqual(submitted, ["b@icloud.com", "a@icloud.com"])
        self.assertEqual(state["submitted"], 2)
        self.assertEqual(state["created"], 2)
        self.assertEqual(state["failed"], 0)
        self.assertEqual(state["status"], "completed")

    def test_rate_limits_continue_and_use_shortest_round_wait(self) -> None:
        accounts = [self.account(f"{name}@icloud.com") for name in ("a", "b", "c")]
        submitted: list[str] = []

        def submitter(account: Any, **_: Any) -> SimpleNamespace:
            submitted.append(account.name)
            if account.name.startswith("a"):
                raise HMECreateRateLimitError(account.name, 1, 5, 9, "interval")
            if account.name.startswith("b"):
                raise HMECreateRateLimitError(account.name, 1, 5, 4, "interval")
            return SimpleNamespace(job_id="job-c")

        controller = self.controller(accounts, submitter=submitter)
        self.configure(controller, [item.name for item in accounts])
        controller.start()
        self.wait_for_thread(controller)

        state = controller.snapshot()
        self.assertEqual(submitted, [item.name for item in accounts])
        self.assertEqual(state["failed"], 2)
        self.assertEqual(state["created"], 1)
        self.assertEqual(controller.waits, [4])
        self.assertTrue(any("最短风控 4s" in line for line in state["progress"]))

    def test_cookie_invalid_account_is_skipped(self) -> None:
        blocked = self.account("blocked@icloud.com")
        ready = self.account("ready@icloud.com")
        self.db.mark_cookie_invalid(blocked.name)
        submitted: list[str] = []

        def submitter(account: Any, **_: Any) -> SimpleNamespace:
            submitted.append(account.name)
            return SimpleNamespace(job_id="job-ready")

        controller = self.controller([blocked, ready], submitter=submitter)
        self.configure(controller, [blocked.name, ready.name])
        controller.start()
        self.wait_for_thread(controller)

        state = controller.snapshot()
        self.assertEqual(submitted, [ready.name])
        self.assertEqual(state["submitted"], 1)
        self.assertEqual(state["created"], 1)
        self.assertEqual(state["skipped"], 1)

    def test_stop_waits_for_current_job_then_finishes(self) -> None:
        account = self.account("a@icloud.com")
        submitted = threading.Event()
        finished = threading.Event()

        def submitter(*_: Any, **__: Any) -> SimpleNamespace:
            submitted.set()
            return SimpleNamespace(job_id="job-a")

        def job_getter(job_id: str, _: AliasDB) -> dict[str, Any]:
            if not finished.is_set():
                return {"job_id": job_id, "status": "running", "result": {}}
            return self.done_job(job_id, self.db)

        controller = self.controller(
            [account],
            submitter=submitter,
            job_getter=job_getter,
            one_round=False,
        )
        self.configure(controller, [account.name])
        controller.start()
        self.assertTrue(submitted.wait(timeout=1))
        stopping = controller.stop()
        self.assertEqual(stopping["status"], "stopping")
        finished.set()
        self.wait_for_thread(controller)
        self.assertEqual(controller.snapshot()["status"], "stopped")

    def test_timed_deadline_and_startup_restore(self) -> None:
        account = self.account("a@icloud.com")
        self.db.create_production_job("interrupted", account.name, "legacy", 1)
        self.db.update_production_loop_state(
            enabled=True,
            status="running",
            selected_accounts=[account.name],
            interface="legacy",
            mode="timed",
            duration_minutes=1,
            deadline_at=99,
        )
        expired = self.controller(
            [account], submitter=lambda *args, **kwargs: None, clock=lambda: 100
        )
        state = expired.startup()
        self.assertEqual(state["status"], "completed")
        self.assertFalse(state["enabled"])
        self.assertEqual(self.db.get_production_job("interrupted")["status"], "error")

        restored_calls: list[str] = []

        def submitter(restored_account: Any, **_: Any) -> SimpleNamespace:
            restored_calls.append(restored_account.name)
            return SimpleNamespace(job_id="restored")

        self.db.update_production_loop_state(
            enabled=True,
            status="running",
            mode="forever",
            deadline_at=0,
        )
        restored = self.controller([account], submitter=submitter)
        restored.startup()
        self.wait_for_thread(restored)
        self.assertEqual(restored_calls, [account.name])
        self.assertEqual(restored.snapshot()["status"], "completed")

    def test_shared_submitter_rejects_invalid_shape(self) -> None:
        account = self.account("a@icloud.com")
        common = {"account": account, "settings": None, "db": None}
        with self.assertRaisesRegex(ValueError, "未知生产接口"):
            submit_account_production(
                **common, interface="new", count=1, threads=1
            )
        with self.assertRaisesRegex(ValueError, "单次只能生产 1 个"):
            submit_account_production(
                **common, interface="legacy", count=2, threads=1
            )
        with self.assertRaisesRegex(ValueError, "并发线程范围"):
            submit_account_production(
                **common, interface="legacy", count=1, threads=0
            )

    def test_shared_submitter_runs_and_persists_successful_job(self) -> None:
        account = self.account("pipeline@icloud.com")
        result = ProductionResult(
            requested=1,
            threads=1,
            created=1,
            items=[{"hme": "generated@icloud.com"}],
            errors=[],
        )
        with patch("web.production_service.produce_aliases", return_value=result) as produce:
            job = submit_account_production(
                account,
                interface="legacy",
                count=1,
                threads=1,
                settings=SimpleNamespace(),
                db=self.db,
            )
            deadline = time.monotonic() + 2
            current: Any = job
            while current.status in {"pending", "running"} and time.monotonic() < deadline:
                time.sleep(0.01)
                current = get_production_job(job.job_id, self.db)

        data = current.to_dict() if hasattr(current, "to_dict") else current
        persisted = self.db.get_production_job(job.job_id)
        self.assertEqual(data["status"], "done")
        self.assertEqual(data["result"]["created"], 1)
        self.assertEqual(persisted["status"], "done")
        self.assertEqual(persisted["created"], 1)
        produce.assert_called_once()


if __name__ == "__main__":
    unittest.main()
