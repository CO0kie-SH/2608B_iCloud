from __future__ import annotations

from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from main import cmd_plan
from tools.client import ICloudError, ICloudHMEClient
from tools.db import AliasDB
from tools.hme import HMEService
from tools.icloud_plan import FREE_STORAGE_BYTES, PLAN_SOURCES, ICloudFreePlanError, ICloudPlan
from tools.rate_limit import HMEAccountAliasLimitError
from tools.production import produce_aliases
from web.errors import register_error_handlers
from web.production_service import production_options_data, submit_account_production
from tests.test_production_loop import OneRoundController


def storage_response(size: int = FREE_STORAGE_BYTES, paid: bool = False) -> dict:
    return {
        "storageUsageInfo": {
            "totalStorageInBytes": size,
            "commerceStorageInBytes": size if paid else 0,
            "compStorageInBytes": 0,
        },
        "quotaStatus": {"paidQuota": paid},
    }


def plan_response(size: int = 5, source: str | None = None) -> dict:
    return {
        "featureKey": "cloud.storage",
        "summary": {"includedInPlan": True, "limit": size, "limitUnits": "GIB"},
        **{key: {"includedInPlan": key == source} for key in PLAN_SOURCES},
    }


def response(status: int, data: dict | None = None) -> Mock:
    return Mock(status_code=status, text=json.dumps(data) if data is not None else "", json=lambda: data)


def validation(host: str = "p218-setup-china.icloud.com") -> dict:
    return {
        "dsInfo": {"dsid": "12345", "isHideMyEmailFeatureAvailable": True},
        "webservices": {
            "account": {"url": f"https://{host}:443"},
            "premiummailsettings": {"url": "https://p218-maildomainws-china.icloud.com"},
        },
    }


class ICloudPlanParsingTests(unittest.TestCase):
    def test_free_plan_requires_complete_consistent_evidence(self) -> None:
        plan = ICloudPlan.from_responses(storage_response(), plan_response())
        self.assertTrue(plan.free_5gb)
        self.assertEqual(plan.storage_bytes, FREE_STORAGE_BYTES)

    def test_paid_shared_comped_and_other_capacities_are_retained(self) -> None:
        cases = [
            (storage_response(50 * 1024**3, True), plan_response(50, PLAN_SOURCES[0])),
            (storage_response(200 * 1024**3), plan_response(200, PLAN_SOURCES[2])),
            (storage_response(), plan_response(5, PLAN_SOURCES[3])),
            (storage_response(paid=True), plan_response()),
        ]
        for storage, summary in cases:
            with self.subTest(storage=storage, summary=summary):
                self.assertFalse(ICloudPlan.from_responses(storage, summary).free_5gb)

    def test_incomplete_or_malformed_responses_remain_unknown(self) -> None:
        cases = [(None, None), ({}, {}), (storage_response(), {})]
        for key in PLAN_SOURCES:
            summary = plan_response()
            summary.pop(key)
            cases.append((storage_response(), summary))
        for value in (None, "false", 0):
            storage = storage_response()
            storage["quotaStatus"]["paidQuota"] = value
            cases.append((storage, plan_response()))
        for value in (True, "5368709120", -1, 0):
            storage = storage_response()
            storage["storageUsageInfo"]["totalStorageInBytes"] = value
            cases.append((storage, plan_response()))
        cases.append((storage_response(), plan_response(50)))
        for storage, summary in cases:
            with self.subTest(storage=storage, summary=summary):
                with self.assertRaises(ValueError):
                    ICloudPlan.from_responses(storage, summary)


class ICloudPlanProductionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "aliases.db"
        self.db = AliasDB(self.path)
        self.account = SimpleNamespace(
            name="owner@icloud.com", mail="owner@icloud.com", cookies="TOKEN",
            ok=True, source="", missing=[], format_errors=[],
        )
        self.settings = SimpleNamespace(
            origin="https://www.icloud.com", setup_host="https://setup.icloud.com",
            client_build="BUILD", client_id="CLIENT", hme_proxy="none", domain="icloud.com",
        )
        self.free = ICloudPlan.from_responses(storage_response(), plan_response())
        self.paid = ICloudPlan.from_responses(
            storage_response(50 * 1024**3, True), plan_response(50, PLAN_SOURCES[0])
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def client(self) -> ICloudHMEClient:
        return ICloudHMEClient(self.settings, self.account.cookies)

    def test_403_checks_plan_removes_selection_and_persists_without_cookie_invalidation(self) -> None:
        self.db.update_production_loop_state(selected_accounts=[self.account.name, "other"])
        with self.client() as client, patch.object(client.session, "request", side_effect=[
            response(200, validation()), response(403),
            response(200, storage_response()), response(200, plan_response()),
        ]) as request:
            with self.assertRaises(ICloudFreePlanError):
                HMEService(client, self.db).create_alias(self.account.name)
            self.assertEqual(request.call_count, 4)
            self.assertIn("/v1/hme/generate", request.call_args_list[1].kwargs["url"])
            self.assertIn("/storageUsageInfo", request.call_args_list[2].kwargs["url"])
            self.assertIn("gatewayws.icloud.com.cn", request.call_args_list[3].kwargs["url"])

        reopened = AliasDB(self.path)
        flag = reopened.get_account_flag(self.account.name)
        self.assertTrue(flag["free_plan"])
        self.assertFalse(flag["cookie_invalid"])
        self.assertGreater(flag["plan_checked_at"], 0)
        self.assertEqual(reopened.get_production_loop_state()["selected_accounts"], ["other"])
        self.assertEqual(reopened.get_create_quota(self.account.name).used, 0)
        with self.assertRaises(ICloudFreePlanError):
            reopened.claim_create_slot(self.account.name)
        reopened.assert_cookie_ready(self.account.name)

    def test_403_paid_or_unknown_plan_keeps_production_eligibility(self) -> None:
        for plan in (self.paid, ValueError("invalid response"), ICloudError("HTTP 421", status_code=421)):
            with self.subTest(plan=plan):
                client = Mock()
                client.call_api.side_effect = ICloudError("HTTP 403: ", status_code=403)
                if isinstance(plan, Exception):
                    client.get_current_plan.side_effect = plan
                else:
                    client.get_current_plan.return_value = plan
                with self.assertRaises(ICloudError) as raised:
                    HMEService(client, self.db).create_alias(self.account.name)
                self.assertEqual(raised.exception.status_code, 403)
                self.assertFalse((self.db.get_account_flag(self.account.name) or {}).get("free_plan"))
                self.assertFalse(self.db.is_cookie_invalid(self.account.name))
                self.assertEqual(self.db.get_create_quota(self.account.name).used, 0)
                claim = self.db.claim_create_slot(self.account.name)
                self.db.release_create_claim(claim)

    def test_non_403_and_incidental_403_text_do_not_query_plan(self) -> None:
        for error in (ICloudError("HTTP 500: 403", status_code=500), ICloudError("network 403")):
            client = Mock()
            client.call_api.side_effect = error
            with self.assertRaises(ICloudError) as raised:
                HMEService(client, self.db).create_alias(self.account.name)
            self.assertIs(raised.exception, error)
            client.get_current_plan.assert_not_called()

    def test_reserve_403_also_checks_plan(self) -> None:
        client = Mock()
        client.call_api.side_effect = [
            {"success": True, "result": {"hme": "generated@icloud.com"}},
            ICloudError("HTTP 403", status_code=403),
        ]
        client.get_current_plan.return_value = self.free
        with self.assertRaises(ICloudFreePlanError):
            HMEService(client, self.db).create_alias(self.account.name)
        self.assertTrue(self.db.get_account_flag(self.account.name)["free_plan"])
        self.assertEqual(self.db.count_aliases(), 0)

    def test_current_plan_does_not_require_an_hme_service_and_routes_by_discovery(self) -> None:
        for host, gateway in (("p218-setup-china.icloud.com", "gatewayws.icloud.com.cn"),
                              ("p01-setup.icloud.com", "gatewayws.icloud.com")):
            with self.subTest(host=host), self.client() as client:
                data = validation(host)
                data["webservices"].pop("premiummailsettings")
                with patch.object(client, "_request", side_effect=[data, storage_response(), plan_response()]) as req:
                    self.assertTrue(client.get_current_plan().free_5gb)
                self.assertIn(gateway, req.call_args_list[-1].args[1])

    def test_cookie_updates_do_not_clear_plan_and_paid_recheck_does_not_auto_select(self) -> None:
        self.db.save_account_plan(self.account.name, self.free)
        self.db.mark_cookie_invalid(self.account.name)
        self.db.clear_cookie_invalid(self.account.name)
        self.assertTrue(self.db.get_account_flag(self.account.name)["free_plan"])
        self.db.update_production_loop_state(selected_accounts=[self.account.name])
        self.assertEqual(self.db.get_production_loop_state()["selected_accounts"], [])
        self.db.save_account_plan(self.account.name, self.paid)
        self.db.assert_production_ready(self.account.name)
        self.assertEqual(self.db.get_production_loop_state()["selected_accounts"], [])

    def test_manual_free_plan_unlock_clears_flag_without_auto_selection(self) -> None:
        self.db.save_account_plan(self.account.name, self.free)
        self.db.update_production_loop_state(selected_accounts=[self.account.name])
        flag = self.db.clear_account_free_plan(self.account.name)
        self.assertFalse(flag["free_plan"])
        self.db.assert_production_ready(self.account.name)
        self.assertEqual(self.db.get_production_loop_state()["selected_accounts"], [])

    def test_pool_options_and_submission_block_free_plan_before_network(self) -> None:
        self.db.save_account_plan(self.account.name, self.free)
        item = production_options_data([self.account], settings=self.settings, db=self.db)["accounts"][0]
        self.assertFalse(item["hme_ok"])
        self.assertFalse(item["cookie_invalid"])
        self.assertTrue(item["free_plan"])
        with patch("web.production_service.submit_production") as submit:
            with self.assertRaises(ICloudFreePlanError):
                submit_account_production(self.account, interface="legacy", count=1, threads=1,
                                          settings=self.settings, db=self.db)
            submit.assert_not_called()

    def test_upstream_41012_marks_account_at_alias_limit(self) -> None:
        self.db.update_production_loop_state(selected_accounts=[self.account.name, "other"])
        flag = self.db.mark_account_alias_limit_reached(self.account.name)
        self.assertTrue(flag["alias_limit_reached"])
        self.assertEqual(self.db.get_production_loop_state()["selected_accounts"], ["other"])
        item = production_options_data([self.account], settings=self.settings, db=self.db)["accounts"][0]
        self.assertTrue(item["alias_limit_reached"])
        self.assertFalse(item["hme_ok"])
        with self.assertRaises(HMEAccountAliasLimitError):
            submit_account_production(self.account, interface="legacy", count=1, threads=1,
                                      settings=self.settings, db=self.db)

    def test_pipeline_records_plan_failure_and_followup_network_attempts(self) -> None:
        with self.client() as client, patch.object(client.session, "request", side_effect=[
            response(200, validation()), response(403),
            response(200, storage_response()), response(200, plan_response()),
        ]), patch("tools.production.ICloudHMEClient", return_value=client):
            result = produce_aliases(self.account, settings=self.settings, db=self.db, count=1)
        self.assertEqual(result.created, 0)
        self.assertIn("ICLOUD_FREE_PLAN", result.errors[0])
        self.assertEqual(result.network[0]["successful_attempts"], 3)
        self.assertEqual(result.network[0]["failed_attempts"], 1)

    def test_loop_removes_free_account_and_stops_when_pool_is_empty(self) -> None:
        def submitter(account, **kwargs):
            self.db.save_account_plan(account.name, self.free)
            return SimpleNamespace(job_id="free-job")

        controller = OneRoundController(
            self.db, accounts_loader=lambda: [self.account], settings_loader=lambda: self.settings,
            submitter=submitter,
            job_getter=lambda *_: {"status": "error", "error": "ICLOUD_FREE_PLAN", "result": {}},
            poll_interval_sec=0.01,
        )
        controller.configure(selected_accounts=[self.account.name], interface="legacy", mode="forever",
                             duration_minutes=0, interval_sec=2)
        controller.start()
        controller._thread.join(timeout=3)
        self.assertFalse(controller._thread.is_alive())
        state = controller.snapshot()
        self.assertEqual(state["selected_accounts"], [])
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["skipped"], 1)
        self.assertEqual(controller.waits, [])

    def test_plan_error_http_contract(self) -> None:
        app = FastAPI()
        register_error_handlers(app)

        @app.get("/blocked")
        def blocked():
            raise ICloudFreePlanError(self.account.name)

        with TestClient(app) as client:
            result = client.get("/blocked")
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()["error"]["code"], "ICLOUD_FREE_PLAN")
        self.assertNotIn("retry-after", result.headers)

    def test_cli_plan_recovery_and_failed_query_preserves_flag(self) -> None:
        self.db.save_account_plan(self.account.name, self.free)
        args = SimpleNamespace(account=self.account.name)
        for result, expected in ((ICloudError("HTTP 403", status_code=403), 1), (self.paid, 0)):
            with patch("main._pick_account", return_value=(self.settings, self.account)), \
                    patch("main.get_db", return_value=self.db), \
                    patch("main.ICloudHMEClient") as factory:
                client = factory.return_value.__enter__.return_value
                if isinstance(result, Exception):
                    client.get_current_plan.side_effect = result
                else:
                    client.get_current_plan.return_value = result
                self.assertEqual(cmd_plan(args), expected)
                self.assertEqual(self.db.get_account_flag(self.account.name)["free_plan"], bool(expected))

    def test_existing_database_migration_preserves_cookie_flags(self) -> None:
        path = Path(self.temp.name) / "legacy.db"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("""CREATE TABLE account_flags (
                account TEXT PRIMARY KEY, cookie_invalid INTEGER NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT '', marked_at INTEGER NOT NULL DEFAULT 0,
                cleared_at INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)""")
            conn.execute("INSERT INTO account_flags (account,cookie_invalid,reason,updated_at) VALUES (?,1,'http_421','2026-09-06')", (self.account.name,))
        flag = AliasDB(path).get_account_flag(self.account.name)
        self.assertTrue(flag["cookie_invalid"])
        self.assertFalse(flag["free_plan"])
        self.assertEqual(flag["reason"], "http_421")


if __name__ == "__main__":
    unittest.main()
