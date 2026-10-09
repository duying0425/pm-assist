"""HTTP 接口、凭证轮换、并发与持久化的回归检查；不使用生产凭证。"""
import asyncio
import importlib
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

fake_config = types.ModuleType("config")
for key, value in {
    "FEISHU_APP_ID": "test-app", "FEISHU_APP_SECRET": "test-app-secret",
    "HUB_CALLBACK_URI": "https://hub.example/hub/callback", "HUB_SECRET": "state-secret",
    "HUB_ADMIN_API_KEY": "admin-test-key", "HUB_TOKEN_DB_PATH": "unused-test-db",
    "ADMIN_OPEN_IDS": {"ou_admin1", "ou_admin2"},
}.items():
    setattr(fake_config, key, value)
fake_feishu = types.ModuleType("feishu")
with patch.dict(sys.modules, {"config": fake_config, "feishu": fake_feishu}):
    import auth_hub as hub
from hub_tokens import HubTokenStore

REAL_ASYNC_CLIENT = httpx.AsyncClient


class HubApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.store = HubTokenStore(Path(self.temp.name) / "tokens.sqlite3")
        self.clients = {
            "scopes": ["offline_access"],
            "clients": [
                {"id": "chatlogger", "secret": "client-one-secret",
                 "redirect_uri": "https://client.example/auth/callback"},
                {"id": "another", "secret": "client-two-secret",
                 "redirect_uri": "https://another.example/auth/callback"},
            ],
        }
        self.addCleanup(patch.stopall)
        patch.dict(sys.modules, {"auth_hub": hub}).start()
        patch.object(hub, "load_hub_config", return_value=self.clients).start()
        patch.object(hub, "_token_store", self.store).start()
        patch.object(hub, "_token_locks", {}).start()
        patch.object(hub, "_auth_codes", {}).start()
        patch.object(hub, "HUB_ADMIN_API_KEY", "admin-test-key").start()
        patch.object(hub, "ADMIN_OPEN_IDS", {"ou_admin1", "ou_admin2"}).start()
        self.events = []
        self.refresh_calls = 0
        self.refresh_error = None
        self.profile_error = False
        self.transport = httpx.MockTransport(self.upstream)
        patch.object(hub.httpx, "AsyncClient",
                     side_effect=lambda **kw: REAL_ASYNC_CLIENT(transport=self.transport, **kw)).start()
        self.app = importlib.import_module("hub_main").app
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer admin-test-key"}

    async def upstream(self, request):
        if request.url.path.endswith("/user_info"):
            if self.profile_error:
                return httpx.Response(200, json={"code": 999, "data": {}})
            return httpx.Response(200, json={"code": 0, "data": {
                "open_id": "ou_admin1", "name": "Admin One",
            }})
        data = json.loads(request.content)
        self.events.append(data)
        if data["grant_type"] == "refresh_token":
            self.refresh_calls += 1
            await asyncio.sleep(0.02)
            if self.refresh_error == "timeout":
                raise httpx.ReadTimeout("temporary upstream timeout", request=request)
            if self.refresh_error:
                return httpx.Response(400, json={"code": self.refresh_error, "msg": "invalid"})
        suffix = str(self.refresh_calls)
        return httpx.Response(200, json={
            "code": 0, "access_token": "user-new-" + suffix,
            "refresh_token": "refresh-new-" + suffix,
            "expires_in": 7200, "refresh_token_expires_in": 604800,
        })

    def grant(self, expires_in=3600, refresh_in=604800, open_id="ou_admin1", client_id="chatlogger"):
        return self.store.save(client_id, {
            "open_id": open_id, "name": "Admin One",
            "access_token": "user-original", "refresh_token": "refresh-original-" + open_id,
            "expires_at": time.time() + expires_in,
            "refresh_expires_at": time.time() + refresh_in,
        })

    def post_user_token(self, body=None):
        return self.client.post("/hub/api/user-token", json=body or {}, headers=self.headers)

    def legacy_refresh(self, refresh_token):
        return self.client.post("/hub/api/refresh", json={
            "client_id": "chatlogger", "client_secret": "client-one-secret",
            "refresh_token": refresh_token,
        })

    def test_admin_authentication_is_required_and_non_ascii_key_is_rejected(self):
        self.grant()
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            r = self.client.post("/hub/api/user-token", json={}, headers=headers)
            self.assertEqual(r.status_code, 401)
            self.assertEqual(r.headers["Cache-Control"], "no-store")
            self.assertNotIn("user-original", r.text)
        with self.assertRaises(Exception) as exc:
            hub._require_admin("Bearer 非ASCII密钥")
        self.assertEqual(exc.exception.status_code, 401)
        self.assertEqual(self.refresh_calls, 0)

    def test_valid_user_token_is_returned_without_refresh_credentials(self):
        self.grant()
        r = self.post_user_token()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["user_access_token"], "user-original")
        self.assertEqual(r.json()["open_id"], "ou_admin1")
        self.assertEqual(r.json()["token_type"], "Bearer")
        self.assertFalse(r.json()["refreshed"])
        self.assertNotIn("refresh_token", r.json())
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertEqual(self.refresh_calls, 0)

    def test_expired_token_is_rotated_and_persisted(self):
        original = self.grant(expires_in=-10)
        r = self.post_user_token()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["refreshed"])
        self.assertEqual(self.events[0]["refresh_token"], original["refresh_token"])
        self.assertEqual(self.store.get("chatlogger", "ou_admin1")["refresh_token"], "refresh-new-1")
        self.assertEqual(self.store.find_refresh("chatlogger", original["refresh_token"])["access_token"],
                         r.json()["user_access_token"])

    def test_forced_refresh_and_old_client_reuse_current_credentials(self):
        original = self.grant()
        r = self.post_user_token({"force_refresh": True})
        old_client = self.legacy_refresh(original["refresh_token"])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(old_client.status_code, 200)
        self.assertEqual(old_client.json()["access_token"], r.json()["user_access_token"])
        self.assertEqual(old_client.json()["refresh_token"], "refresh-new-1")
        self.assertEqual(self.refresh_calls, 1)

    def test_concurrent_admin_and_legacy_refresh_use_upstream_credential_once(self):
        original = self.grant(expires_in=-10)

        async def run_requests():
            async with REAL_ASYNC_CLIENT(transport=httpx.ASGITransport(app=self.app),
                                         base_url="https://hub.example") as client:
                tasks = [client.post("/hub/api/user-token", json={}, headers=self.headers)
                         for _ in range(8)]
                tasks.append(client.post("/hub/api/refresh", json={
                    "client_id": "chatlogger", "client_secret": "client-one-secret",
                    "refresh_token": original["refresh_token"],
                }))
                return await asyncio.gather(*tasks)

        results = asyncio.run(run_requests())
        self.assertTrue(all(r.status_code == 200 for r in results))
        self.assertEqual({r.json()["user_access_token"] for r in results[:-1]}, {"user-new-1"})
        self.assertEqual(results[-1].json()["access_token"], "user-new-1")
        self.assertEqual(self.refresh_calls, 1)

    def test_restart_retains_tokens_and_old_refresh_alias(self):
        original = self.grant()
        r = self.post_user_token({"force_refresh": True})
        hub._token_store = HubTokenStore(self.store.path)
        hub._token_locks.clear()
        after_restart = self.legacy_refresh(original["refresh_token"])
        self.assertEqual(after_restart.status_code, 200)
        self.assertEqual(after_restart.json()["access_token"], r.json()["user_access_token"])
        self.assertEqual(self.refresh_calls, 1)

    def test_unmanaged_legacy_refresh_is_enrolled_and_repeated_old_token_is_safe(self):
        first = self.legacy_refresh("old-unmanaged-refresh")
        second = self.legacy_refresh("old-unmanaged-refresh")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["access_token"], second.json()["access_token"])
        self.assertEqual(self.refresh_calls, 1)
        self.assertEqual(self.post_user_token().json()["user_access_token"], first.json()["access_token"])

    def test_multiple_accounts_require_selection_and_non_admin_accounts_are_hidden(self):
        self.grant()
        self.grant(open_id="ou_admin2")
        self.grant(open_id="ou_member")
        ambiguous = self.post_user_token()
        self.assertEqual(ambiguous.status_code, 409)
        self.assertEqual(ambiguous.json()["detail"]["code"], "account_required")
        accounts = self.client.post("/hub/api/user-token/accounts", headers=self.headers).json()["accounts"]
        self.assertEqual({a["open_id"] for a in accounts}, {"ou_admin1", "ou_admin2"})
        self.assertEqual(self.post_user_token({"open_id": "ou_admin2"}).status_code, 200)
        self.assertEqual(self.post_user_token({"open_id": "ou_member"}).status_code, 403)
        self.assertNotIn("refresh_token", json.dumps(accounts))

    def test_client_revocation_takes_effect_without_restart(self):
        self.grant()
        self.assertEqual(self.post_user_token().status_code, 200)
        self.clients["clients"] = [self.clients["clients"][1]]
        self.assertEqual(self.post_user_token({"client_id": "chatlogger"}).status_code, 404)
        self.assertEqual(self.post_user_token().status_code, 409)
        self.assertIsNone(self.store.find_refresh("another", "refresh-original-ou_admin1"))

    def test_missing_or_expired_authorization_returns_login_instruction(self):
        for prepare in (lambda: None, lambda: self.grant(expires_in=-10, refresh_in=-10)):
            prepare()
            r = self.post_user_token()
            self.assertEqual(r.status_code, 409)
            self.assertEqual(r.json()["detail"]["code"], "authorization_required")
            self.assertEqual(r.json()["detail"]["authorize_url"],
                             "https://hub.example/hub/authorize?client_id=chatlogger")
        self.assertEqual(self.refresh_calls, 0)

    def test_invalid_refresh_requires_reauthorization_and_preserves_client_error_code(self):
        original = self.grant(expires_in=-10)
        self.refresh_error = 20073
        r = self.post_user_token()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["code"], "authorization_required")
        client = self.legacy_refresh(original["refresh_token"])
        self.assertEqual(client.status_code, 400)
        self.assertEqual(client.json()["detail"]["code"], 20073)
        self.assertEqual(self.refresh_calls, 1)

    def test_temporary_upstream_failure_does_not_destroy_refresh_credentials(self):
        original = self.grant(expires_in=-10)
        self.refresh_error = "timeout"
        r = self.post_user_token()
        self.assertEqual(r.status_code, 502)
        current = self.store.get("chatlogger", "ou_admin1")
        self.assertEqual(current["refresh_token"], original["refresh_token"])
        self.assertEqual(current["refresh_expires_at"], original["refresh_expires_at"])

    def test_oauth_delivery_is_persisted_and_wrong_client_does_not_consume_code(self):
        r = self.client.get("/hub/callback", params={
            "code": "feishu-code", "state": hub._make_state("chatlogger", "client-state"),
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 307)
        import urllib.parse
        callback_params = urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["Location"]).query)
        code = callback_params["auth_code"][0]
        wrong = self.client.post("/hub/api/token", json={
            "client_id": "another", "client_secret": "client-two-secret", "auth_code": code,
        })
        self.assertEqual(wrong.status_code, 400)
        body = {"client_id": "chatlogger", "client_secret": "client-one-secret", "auth_code": code}
        correct = self.client.post("/hub/api/token", json=body)
        self.assertEqual(correct.status_code, 200)
        self.assertEqual(self.post_user_token().json()["user_access_token"], correct.json()["access_token"])
        self.assertEqual(self.client.post("/hub/api/token", json=body).status_code, 400)

    def test_failed_identity_does_not_register_blank_user_and_legacy_still_receives_rotated_tokens(self):
        self.profile_error = True
        r = self.client.get("/hub/callback", params={
            "code": "feishu-code", "state": hub._make_state("chatlogger", ""),
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 502)
        legacy = self.legacy_refresh("unmanaged-refresh")
        self.assertEqual(legacy.status_code, 200)
        self.assertTrue(legacy.json()["refresh_token"])
        self.assertEqual(self.store.list_accounts(), [])

    def test_disabled_api_and_strict_force_refresh(self):
        self.grant()
        self.assertEqual(self.post_user_token({"force_refresh": "false"}).status_code, 422)
        hub.HUB_ADMIN_API_KEY = ""
        self.assertEqual(self.post_user_token().status_code, 503)
        self.assertEqual(self.refresh_calls, 0)

    def test_token_values_and_admin_key_are_not_written_to_audit_log(self):
        self.grant()
        with self.assertLogs("auth_hub", level="INFO") as captured:
            self.post_user_token({"force_refresh": True})
        text = "\n".join(captured.output)
        for value in ("user-new-1", "refresh-new-1", "admin-test-key", "test-app-secret"):
            self.assertNotIn(value, text)

    @unittest.skipUnless(os.name == "posix", "Unix file permissions are validated on the deployment host")
    def test_token_file_is_private(self):
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
