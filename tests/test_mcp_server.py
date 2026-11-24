"""Integration tests for HiDL remote Streamable HTTP MCP server and OAuth 2.1 flow."""

from __future__ import annotations

import base64
import datetime
import gzip
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

from aiohttp.test_utils import TestClient, TestServer

from config import MCPSettings
from db import repositories as repo
from db.database import connect, init_db
from mcp_server.app import create_mcp_app
from scheduler.reminder import ReminderScheduler
from utils.today import render_today


def _make_pkce_pair(verifier: str = "test_verifier_string_1234567890_abcdef_xyz") -> tuple[str, str]:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class MCPServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="hidl_mcp_test_")
        db_file = Path(self._tmpdir.name) / "mcp_test.db"
        self.db_conn = await connect(f"sqlite:///{db_file.as_posix()}")
        await init_db(self.db_conn)

        self.owner_tg_id = 550001
        self.owner_id = await repo.create_user(
            self.db_conn,
            telegram_id=self.owner_tg_id,
            name="Владелец",
            timezone="Europe/Moscow",
            wake_up_time="08:00",
            sleep_time="23:30",
            gender="male",
        )
        await repo.ensure_user_routines(self.db_conn, self.owner_id)
        await repo.upsert_wellness(self.db_conn, self.owner_id, meal_profile="vegan")

        # Secondary user whose private data must remain strictly isolated
        self.other_tg_id = 550099
        self.other_id = await repo.create_user(
            self.db_conn,
            telegram_id=self.other_tg_id,
            name="Чужой",
            timezone="UTC",
            wake_up_time="08:00",
            sleep_time="23:00",
            gender="female",
        )
        self.other_task_id = await repo.add_day_plan_item(
            self.db_conn, self.other_id, "2026-11-19", "Секретная чужая задача"
        )
        self.other_diary_id = await repo.create_journal_entry(
            self.db_conn,
            user_id=self.other_id,
            entry_type="diary",
            entry_date="2026-11-19",
            source="text",
            user_text="Секретная чужая запись дневника",
            title="Секрет чужого",
        )

        self.settings = MCPSettings(
            host="127.0.0.1",
            port=8765,
            public_base_url="https://mcp.example.com",
            owner_telegram_id=self.owner_tg_id,
            access_password="super-secret-pass-42",
            bearer_token="static-test-token-99",
            allow_diary=True,
            readonly=False,
            access_token_ttl_sec=3600,
            refresh_token_ttl_sec=86400,
        )
        self._clients: list[TestClient] = []

    async def asyncTearDown(self):
        for cl in self._clients:
            await cl.close()
        await self.db_conn.close()
        self._tmpdir.cleanup()

    async def _start_client(self, settings: MCPSettings | None = None) -> TestClient:
        app = create_mcp_app(self.db_conn, settings or self.settings)
        client = TestClient(TestServer(app))
        await client.start_server()
        self._clients.append(client)
        return client

    async def _rpc(
        self,
        client: TestClient,
        token: str,
        method: str,
        params: dict | None = None,
        *,
        req_id: int | None = 1,
        session_id: str | None = None,
    ):
        headers = {"Authorization": f"Bearer {token}"}
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        body: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if req_id is not None:
            body["id"] = req_id
        resp = await client.post("/mcp", json=body, headers=headers)
        data = None
        if resp.status == 200:
            data = await resp.json()
        return resp, data

    async def test_unauthenticated_401_and_discovery_metadata(self):
        client = await self._start_client()

        health = await client.get("/health")
        self.assertEqual(health.status, 200)
        health_json = await health.json()
        self.assertTrue(health_json["ok"])
        self.assertTrue(health_json["diary_enabled"])
        self.assertFalse(health_json["readonly"])

        unauth = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(unauth.status, 401)
        www_auth = unauth.headers.get("WWW-Authenticate", "")
        self.assertIn("Bearer", www_auth)
        self.assertIn("https://mcp.example.com/.well-known/oauth-protected-resource", www_auth)

        for path in (
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp",
        ):
            r = await client.get(path)
            self.assertEqual(r.status, 200)
            j = await r.json()
            self.assertEqual(j["resource"], "https://mcp.example.com/mcp")
            self.assertEqual(j["authorization_servers"], ["https://mcp.example.com"])

        for path in (
            "/.well-known/oauth-authorization-server",
            "/.well-known/openid-configuration",
        ):
            r = await client.get(path)
            self.assertEqual(r.status, 200)
            j = await r.json()
            self.assertEqual(j["issuer"], "https://mcp.example.com")
            self.assertEqual(j["registration_endpoint"], "https://mcp.example.com/oauth/register")
            self.assertIn("S256", j["code_challenge_methods_supported"])

    async def test_full_oauth21_dcr_pkce_and_refresh_flow(self):
        client = await self._start_client()

        # 1. Dynamic Client Registration
        reg_resp = await client.post(
            "/oauth/register",
            json={
                "client_name": "ChatGPT Test",
                "redirect_uris": ["https://chatgpt.com/connector_platform_oauth_redirect"],
                "token_endpoint_auth_method": "client_secret_post",
            },
        )
        self.assertEqual(reg_resp.status, 201)
        reg = await reg_resp.json()
        client_id = reg["client_id"]
        client_secret = reg["client_secret"]

        verifier, challenge = _make_pkce_pair()
        auth_query = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://chatgpt.com/connector_platform_oauth_redirect",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "state_xyz_123",
            "scope": "hidl:read hidl:write offline_access",
            "resource": "https://mcp.example.com/mcp",
        }

        # 2. Authorize GET -> consent HTML + CSRF token
        get_auth = await client.get("/oauth/authorize", params=auth_query)
        self.assertEqual(get_auth.status, 200)
        html_body = await get_auth.text()
        self.assertIn("ChatGPT Test", html_body)
        m_csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', html_body)
        self.assertIsNotNone(m_csrf)
        csrf_token = m_csrf.group(1)

        # 3. Wrong password -> 403
        bad_post = await client.post(
            "/oauth/authorize",
            data={**auth_query, "csrf_token": csrf_token, "password": "wrong-password"},
            allow_redirects=False,
        )
        self.assertEqual(bad_post.status, 403)

        # 4. Good password -> 302 redirect with auth code & state
        good_post = await client.post(
            "/oauth/authorize",
            data={**auth_query, "csrf_token": csrf_token, "password": "super-secret-pass-42"},
            allow_redirects=False,
        )
        self.assertEqual(good_post.status, 302)
        loc = good_post.headers["Location"]
        parsed_loc = urlparse(loc)
        qs = parse_qs(parsed_loc.query)
        self.assertEqual(qs.get("state"), ["state_xyz_123"])
        auth_code = qs["code"][0]

        # 5. PKCE bad verifier rejected
        bad_tok = await client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "code": auth_code,
                "redirect_uri": auth_query["redirect_uri"],
                "code_verifier": "wrong_verifier_value_00000000000000000000",
            },
        )
        self.assertEqual(bad_tok.status, 400)

        # 6. Good PKCE verifier -> access & refresh tokens
        tok_resp = await client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "code": auth_code,
                "redirect_uri": auth_query["redirect_uri"],
                "code_verifier": verifier,
            },
        )
        self.assertEqual(tok_resp.status, 200)
        tok = await tok_resp.json()
        access_token = tok["access_token"]
        refresh_token = tok["refresh_token"]

        # Code replay rejected
        replay_resp = await client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "code": auth_code,
                "redirect_uri": auth_query["redirect_uri"],
                "code_verifier": verifier,
            },
        )
        self.assertEqual(replay_resp.status, 400)

        # 7. Issued access token authenticates /mcp
        init_resp, init_data = await self._rpc(
            client, access_token, "initialize", {"protocolVersion": "2025-03-26"}
        )
        self.assertEqual(init_resp.status, 200)
        self.assertEqual(init_data["result"]["serverInfo"]["name"], "hidl-mcp")
        self.assertTrue(init_resp.headers.get("Mcp-Session-Id"))

        # 8. Refresh token rotation
        ref_resp = await client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            },
        )
        self.assertEqual(ref_resp.status, 200)
        ref_tok = await ref_resp.json()
        self.assertNotEqual(ref_tok["access_token"], access_token)
        self.assertNotEqual(ref_tok["refresh_token"], refresh_token)

        # Replaying old refresh token fails
        old_ref = await client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            },
        )
        self.assertEqual(old_ref.status, 400)

        # New rotated token works on /mcp
        ping_resp, ping_data = await self._rpc(client, ref_tok["access_token"], "ping")
        self.assertEqual(ping_resp.status, 200)
        self.assertEqual(ping_data["result"], {})

    async def test_mcp_bidirectional_tools_resources_and_telegram_sync(self):
        client = await self._start_client()
        tok = self.settings.bearer_token

        init_resp, _ = await self._rpc(client, tok, "initialize", {"protocolVersion": "2025-03-26"})
        sid = init_resp.headers["Mcp-Session-Id"]

        notif_resp, _ = await self._rpc(client, tok, "notifications/initialized", req_id=None, session_id=sid)
        self.assertEqual(notif_resp.status, 202)

        _, list_res = await self._rpc(client, tok, "tools/list", session_id=sid)
        tool_names = {t["name"] for t in list_res["result"]["tools"]}
        self.assertTrue(
            {
                "hidl_get_today",
                "hidl_list_tasks",
                "hidl_add_task",
                "hidl_set_task_done",
                "hidl_add_reminder",
                "hidl_manage_shopping",
                "hidl_add_pantry_item",
                "hidl_get_kitchen",
                "hidl_add_expense",
                "hidl_get_finance",
                "hidl_add_diary_entry",
                "hidl_get_diary",
                "hidl_search_diary",
                "search",
                "fetch",
            }.issubset(tool_names)
        )

        # Add task via MCP -> visible in hidl_list_tasks AND Telegram render_today
        _, add_task_r = await self._rpc(
            client,
            tok,
            "tools/call",
            {"name": "hidl_add_task", "arguments": {"title": "Забрать заказ из ПВЗ", "is_important": True}},
            session_id=sid,
        )
        task_payload = json.loads(add_task_r["result"]["content"][0]["text"])
        self.assertTrue(task_payload["ok"])
        task_id = task_payload["task_id"]

        today_txt, _ = await render_today(self.db_conn, self.owner_id and dict(await repo.get_user(self.db_conn, self.owner_id)))
        self.assertIn("Забрать заказ из ПВЗ", today_txt)

        await self._rpc(
            client,
            tok,
            "tools/call",
            {"name": "hidl_set_task_done", "arguments": {"task_id": task_id, "done": True}},
            session_id=sid,
        )
        _, tasks_r = await self._rpc(client, tok, "tools/call", {"name": "hidl_list_tasks", "arguments": {}}, session_id=sid)
        tasks_data = json.loads(tasks_r["result"]["content"][0]["text"])
        matched_t = [t for t in tasks_data["day_plan_tasks"] if t["id"] == task_id]
        self.assertEqual(len(matched_t), 1)
        self.assertTrue(matched_t[0]["done"])

        # Add reminder
        _, rem_r = await self._rpc(
            client,
            tok,
            "tools/call",
            {"name": "hidl_add_reminder", "arguments": {"title": "Выпить витамин", "reminder_time": "10:15", "date": "2099-12-01"}},
            session_id=sid,
        )
        self.assertTrue(json.loads(rem_r["result"]["content"][0]["text"])["ok"])

        # Shopping add -> mark_bought -> remove
        await self._rpc(
            client,
            tok,
            "tools/call",
            {"name": "hidl_manage_shopping", "arguments": {"action": "add", "item_name": "Тофу", "quantity": 2, "unit": "уп"}},
            session_id=sid,
        )
        await self._rpc(
            client,
            tok,
            "tools/call",
            {"name": "hidl_manage_shopping", "arguments": {"action": "mark_bought", "item_name": "Тофу"}},
            session_id=sid,
        )
        _, shop_r = await self._rpc(
            client, tok, "tools/call", {"name": "hidl_list_shopping", "arguments": {"scope": "household"}}, session_id=sid
        )
        shop_items = json.loads(shop_r["result"]["content"][0]["text"])["household"]
        self.assertTrue(any(i["item_name"] == "Тофу" and i["is_bought"] for i in shop_items))

        await self._rpc(
            client,
            tok,
            "tools/call",
            {"name": "hidl_manage_shopping", "arguments": {"action": "remove", "item_name": "Тофу"}},
            session_id=sid,
        )

        # Pantry + kitchen vegan filtering
        await self._rpc(
            client,
            tok,
            "tools/call",
            {"name": "hidl_add_pantry_item", "arguments": {"name": "Нут", "amount": 2, "unit": "шт", "category": "бобовые"}},
            session_id=sid,
        )
        _, kitchen_r = await self._rpc(
            client, tok, "tools/call", {"name": "hidl_get_kitchen", "arguments": {"servings": 2}}, session_id=sid
        )
        kitchen_data = json.loads(kitchen_r["result"]["content"][0]["text"])
        self.assertEqual(kitchen_data["meal_profile"], "vegan")
        self.assertTrue(any(p["name"] == "Нут" for p in kitchen_data["pantry_items"]))
        self.assertTrue(all("vegan" in r["tags"] for r in kitchen_data["recipes"]))

        # Finance add + get
        await self._rpc(
            client, tok, "tools/call", {"name": "hidl_add_expense", "arguments": {"amount": 320, "category": "кофе"}}, session_id=sid
        )
        _, fin_r = await self._rpc(client, tok, "tools/call", {"name": "hidl_get_finance", "arguments": {"days": 7}}, session_id=sid)
        fin_data = json.loads(fin_r["result"]["content"][0]["text"])
        self.assertGreaterEqual(fin_data["window_total"], 320.0)

        # Diary add + stem search + connector search/fetch
        _, diary_add_r = await self._rpc(
            client,
            tok,
            "tools/call",
            {
                "name": "hidl_add_diary_entry",
                "arguments": {
                    "entry_type": "diary",
                    "title": "Позвонить папе",
                    "text": "Надо обсудить поздравление папе на выходные",
                },
            },
            session_id=sid,
        )
        diary_add_data = json.loads(diary_add_r["result"]["content"][0]["text"])
        self.assertEqual(diary_add_data["source"], "mcp_chatgpt")

        _, diary_search_r = await self._rpc(
            client, tok, "tools/call", {"name": "hidl_search_diary", "arguments": {"query": "папа"}}, session_id=sid
        )
        self.assertTrue(json.loads(diary_search_r["result"]["content"][0]["text"])["results"])

        _, search_r = await self._rpc(
            client, tok, "tools/call", {"name": "search", "arguments": {"query": "папа"}}, session_id=sid
        )
        search_results = json.loads(search_r["result"]["content"][0]["text"])["results"]
        self.assertTrue(any(item["id"].startswith("diary:") for item in search_results))

        diary_cid = next(item["id"] for item in search_results if item["id"].startswith("diary:"))
        _, fetch_r = await self._rpc(
            client, tok, "tools/call", {"name": "fetch", "arguments": {"id": diary_cid}}, session_id=sid
        )
        fetch_data = json.loads(fetch_r["result"]["content"][0]["text"])
        self.assertIn("поздравление папе", fetch_data["text"])

        # Resources list/read
        _, res_list = await self._rpc(client, tok, "resources/list", session_id=sid)
        uris = {r["uri"] for r in res_list["result"]["resources"]}
        self.assertEqual(
            uris,
            {
                "hidl://today",
                "hidl://household",
                "hidl://shopping",
                "hidl://pantry",
                "hidl://karma",
                "hidl://calendar",
                "hidl://diary/recent",
            },
        )
        _, res_read = await self._rpc(client, tok, "resources/read", {"uri": "hidl://today"}, session_id=sid)
        self.assertIn("Забрать заказ из ПВЗ", res_read["result"]["contents"][0]["text"])

    async def test_privacy_toggles_and_owner_isolation(self):
        # 1. Verify secondary user records cannot be fetched or seen
        client = await self._start_client()
        tok = self.settings.bearer_token
        _, sec_task = await self._rpc(
            client, tok, "tools/call", {"name": "fetch", "arguments": {"id": f"task:{self.other_task_id}"}}
        )
        self.assertTrue(sec_task["result"]["isError"])
        _, sec_diary = await self._rpc(
            client, tok, "tools/call", {"name": "fetch", "arguments": {"id": f"diary:{self.other_diary_id}"}}
        )
        self.assertTrue(sec_diary["result"]["isError"])
        _, search_other = await self._rpc(
            client, tok, "tools/call", {"name": "search", "arguments": {"query": "Секретная чужая"}}
        )
        self.assertEqual(json.loads(search_other["result"]["content"][0]["text"])["results"], [])

        # 2. MCP_ALLOW_DIARY=0 hides & blocks diary tools/resources
        no_diary_cfg = MCPSettings(
            **{**self.settings.__dict__, "allow_diary": False}
        )
        nd_client = await self._start_client(no_diary_cfg)
        _, nd_tools = await self._rpc(nd_client, tok, "tools/list")
        nd_names = {t["name"] for t in nd_tools["result"]["tools"]}
        self.assertNotIn("hidl_get_diary", nd_names)
        self.assertNotIn("hidl_search_diary", nd_names)
        self.assertNotIn("hidl_add_diary_entry", nd_names)

        _, nd_call = await self._rpc(nd_client, tok, "tools/call", {"name": "hidl_get_diary", "arguments": {}})
        self.assertTrue(nd_call["result"]["isError"])

        _, nd_res = await self._rpc(nd_client, tok, "resources/list")
        nd_uris = {r["uri"] for r in nd_res["result"]["resources"]}
        self.assertNotIn("hidl://diary/recent", nd_uris)

        # 3. MCP_READONLY=1 hides & blocks all write tools
        ro_cfg = MCPSettings(
            **{**self.settings.__dict__, "readonly": True}
        )
        ro_client = await self._start_client(ro_cfg)
        _, ro_tools = await self._rpc(ro_client, tok, "tools/list")
        ro_names = {t["name"] for t in ro_tools["result"]["tools"]}
        for write_name in (
            "hidl_add_task",
            "hidl_set_task_done",
            "hidl_add_reminder",
            "hidl_manage_shopping",
            "hidl_add_pantry_item",
            "hidl_add_expense",
            "hidl_add_diary_entry",
            "hidl_add_calendar_event",
            "hidl_set_meal_plan",
            "hidl_nudge_partner",
        ):
            self.assertNotIn(write_name, ro_names)

        _, ro_call = await self._rpc(
            ro_client, tok, "tools/call", {"name": "hidl_add_task", "arguments": {"title": "Нельзя"}}
        )
        self.assertTrue(ro_call["result"]["isError"])

    async def test_internal_ops_status_and_backup_endpoints(self):
        client = await self._start_client()
        tok = self.settings.bearer_token

        # 1. Unauthenticated / wrong bearer must be rejected (401)
        for path in ("/internal/status", "/internal/backup"):
            r_noauth = await client.get(path)
            self.assertEqual(r_noauth.status, 401)
            r_bad = await client.get(path, headers={"Authorization": "Bearer wrong-token"})
            self.assertEqual(r_bad.status, 401)

        # 2. Missing heartbeat initially -> bot_alive=False, db_ok=True
        r_st1 = await client.get("/internal/status", headers={"Authorization": f"Bearer {tok}"})
        self.assertEqual(r_st1.status, 200)
        d1 = await r_st1.json()
        self.assertTrue(d1["ok"])
        self.assertTrue(d1["db_ok"])
        self.assertGreaterEqual(d1["users_count"], 2)
        self.assertFalse(d1["bot_alive"])
        self.assertIsNone(d1["bot_heartbeat_age_sec"])

        # 3. Fresh heartbeat written by ReminderScheduler -> bot_alive=True
        sched = ReminderScheduler(SimpleNamespace(send_message=AsyncMock()), self.db_conn)
        await sched.write_bot_heartbeat()
        r_st2 = await client.get("/internal/status", headers={"Authorization": f"Bearer {tok}"})
        self.assertEqual(r_st2.status, 200)
        d2 = await r_st2.json()
        self.assertTrue(d2["bot_alive"])
        self.assertIsNotNone(d2["bot_heartbeat_age_sec"])
        self.assertLess(d2["bot_heartbeat_age_sec"], 10.0)

        # 4. Stale heartbeat -> bot_alive=False
        hb_file = Path(self._tmpdir.name) / "bot_heartbeat.json"
        hb_file.write_text(json.dumps({"ts": 1000.0, "iso": "1970-01-01T00:16:40Z"}), encoding="utf-8")
        r_st3 = await client.get("/internal/status", headers={"Authorization": f"Bearer {tok}"})
        d3 = await r_st3.json()
        self.assertFalse(d3["bot_alive"])
        self.assertGreater(d3["bot_heartbeat_age_sec"], 180.0)

        # 5. Verified gzipped SQLite snapshot from /internal/backup
        r_bk = await client.get("/internal/backup", headers={"Authorization": f"Bearer {tok}"})
        self.assertEqual(r_bk.status, 200)
        gz_bytes = await r_bk.read()
        raw_db = gzip.decompress(gz_bytes)
        check_path = Path(self._tmpdir.name) / "restored_check.db"
        check_path.write_bytes(raw_db)
        chk_conn = sqlite3.connect(str(check_path))
        try:
            self.assertEqual(chk_conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            cnt = chk_conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            self.assertGreaterEqual(cnt, 2)
        finally:
            chk_conn.close()


if __name__ == "__main__":
    unittest.main()
