from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools.rate_limit import HMEAccountAliasLimitError
from web.errors import register_error_handlers


class AccountLimitAPITests(unittest.TestCase):
    def test_account_limit_returns_409_without_retry_after(self) -> None:
        app = FastAPI()
        register_error_handlers(app)

        @app.get("/limit")
        def account_limit() -> None:
            raise HMEAccountAliasLimitError("owner@icloud.com", 740)

        with TestClient(app) as client:
            response = client.get("/limit")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "HME_ACCOUNT_LIMIT")
        self.assertIn("740/740", response.json()["error"]["message"])
        self.assertNotIn("retry-after", response.headers)


if __name__ == "__main__":
    unittest.main()
