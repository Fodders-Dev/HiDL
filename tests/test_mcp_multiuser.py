"""Tests for Block 4 Multi-user MCP (per-user OAuth codes, PATs, privacy isolation, household & recipe tools, /mcp UI)."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

from aiohttp.test_utils import TestClient, TestServer

from config import MCPSettings
from db import repositories as repo
from db.database import connect, init_db
from handlers import mcp_connect
from mcp_server.app import create_mcp_app


def _make_pkce_pair(verifier: str = "multiuser_pkce_verifier_1234567890_abcdef_xyz") -> tuple[str, str]:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class MultiUserMCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="hidl_mcp_mu_")
        db_file = Path(self._tmpdir.name) / "mcp_multiuser.db"
        self.db_conn = await connect(f"sqlite:///{db_file.as_posix()}")
        await init_db(self.db_conn)

        self.a_tg = 660001
        self.a_id = await repo.create_user(
            self.db_conn,
            telegram_id=self.a_tg,
            name="Артем",
            timezone="Europe/Moscow",
            wake_up_time="08:00",
            sleep_time="23:30",
            gender="male",
        )
        await repo.upsert_wellness(self.db_conn, self.a_id, meal_profile="vegan")

        self.b_tg = 660002
        self.b_id = await repo.create_user(
            self.db_conn,
            telegram_id=self.b_tg,
            name="Саша",
            timezone="Europe/Moscow",
            wake_up_time="08:30",
            sleep_time="23:30",
            gender="female",
        )
        await repo.upsert_wellness(self.db_conn, self.b_id, meal_profile="vegan")

        self.hid = await repo.get_or_create_household(self.db_conn, self.a_id)
        await repo.set_user_household(self.db_conn, self.b_id, self.hid)

        self.stranger_tg = 660099
        self.stranger_id = await repo.create_user(
            self.db_conn,
            telegram_id=self.stranger_tg,
            name="Чужой",
            timezone="UTC",
            wake_up_time="09:00",
            sleep_time="23:00",
        )

        # Notice access_password="" proves /mcp connect codes work even without shared password
        self.settings_no_pw = MCPSettings(
            host="127.0.0.1",
            port=8766,
            public_base_url="https://mcp.example.com",
            owner_telegram_id=self.a_tg,
            access_password="",
            bearer_token="static-ops-token-44",
            allow_diary=True,
            readonly=False,
            access_token_ttl_sec=3600,
            refresh_token_ttl_sec=86400,
        )
        self._clients: list[TestClient] = []

    async def asyncTearDown(self) -> None:
        for cl in self._clients:
            await cl.close()
        await self.db_conn.close()
        self._tmpdir.cleanup()

    async def _start_client(
        self, settings: MCPSettings | None = None, *, telegram_bot=None
    ) -> TestClient:
        app = create_mcp_app(
            self.db_conn, settings or self.settings_no_pw, telegram_bot=telegram_bot
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        self._clients.append(client)
        return client

    async def _rpc(self, client: TestClient, token: str, method: str, params: dict | None = None):
        body: dict = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            body["params"] = params
        resp = await client.post(
            "/mcp", json=body, headers={"Authorization": f"Bearer {token}"}
        )
        data = await resp.json() if resp.status == 200 else None
        return resp, data

    async def _oauth_connect_with_code(
        self, client: TestClient, connect_code: str, *, client_name: str = "ChatGPT Partner"
    ) -> tuple[str, str, str, str]:
        reg_r = await client.post(
            "/oauth/register",
            json={
                "client_name": client_name,
                "redirect_uris": ["https://chatgpt.com/connector_platform_oauth_redirect"],
                "token_endpoint_auth_method": "client_secret_post",
            },
        )
        self.assertEqual(reg_r.status, 201)
        reg = await reg_r.json()
        cid, csec = reg["client_id"], reg["client_secret"]

        verifier, challenge = _make_pkce_pair(f"verifier_{client_name}_0123456789_abcdef_xyz")
        q = {
            "response_type": "code",
            "client_id": cid,
            "redirect_uri": "https://chatgpt.com/connector_platform_oauth_redirect",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": f"st_{client_name}",
            "scope": "hidl:read hidl:write offline_access",
            "resource": "https://mcp.example.com/mcp",
        }
        get_r = await client.get("/oauth/authorize", params=q)
        self.assertEqual(get_r.status, 200)
        body_html = await get_r.text()
        self.assertIn("/mcp", body_html)
        csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', body_html).group(1)

        post_r = await client.post(
            "/oauth/authorize",
            data={**q, "csrf_token": csrf, "password": connect_code},
            allow_redirects=False,
        )
        self.assertEqual(post_r.status, 302)
        auth_code = parse_qs(urlparse(post_r.headers["Location"]).query)["code"][0]

        tok_r = await client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": cid,
                "client_secret": csec,
                "code": auth_code,
                "redirect_uri": q["redirect_uri"],
                "code_verifier": verifier,
            },
        )
        self.assertEqual(tok_r.status, 200)
        tok = await tok_r.json()
        return cid, csec, tok["access_token"], tok["refresh_token"]

    async def test_1_two_partners_oauth_via_connect_codes_and_strict_isolation(self) -> None:
        client = await self._start_client()

        code_a = (await repo.create_mcp_connect_code(self.db_conn, self.a_id))["code"]
        code_b = (await repo.create_mcp_connect_code(self.db_conn, self.b_id))["code"]

        # Both partners onboard cleanly via one-time /mcp codes
        _, _, tok_a, _ = await self._oauth_connect_with_code(client, code_a, client_name="Artem ChatGPT")
        cid_b, csec_b, tok_b, rt_b = await self._oauth_connect_with_code(
            client, code_b.lower(), client_name="Sasha Claude"
        )

        # Replay of consumed /mcp connect code must be rejected with 403
        q_replay = {
            "response_type": "code",
            "client_id": cid_b,
            "redirect_uri": "https://chatgpt.com/connector_platform_oauth_redirect",
            "code_challenge": _make_pkce_pair()[1],
            "code_challenge_method": "S256",
            "state": "replay",
        }
        r_get = await client.get("/oauth/authorize", params=q_replay)
        csrf2 = re.search(r'name="csrf_token"\s+value="([^"]+)"', await r_get.text()).group(1)
        r_replay = await client.post(
            "/oauth/authorize",
            data={**q_replay, "csrf_token": csrf2, "password": code_b},
            allow_redirects=False,
        )
        self.assertEqual(r_replay.status, 403)

        # Partner A creates private diary, private task, and shared household shopping item
        _, d_res = await self._rpc(
            client,
            tok_a,
            "tools/call",
            {
                "name": "hidl_add_diary_entry",
                "arguments": {"title": "Секрет Артема", "text": "Личная заметка только для Артема"},
            },
        )
        a_diary_id = json.loads(d_res["result"]["content"][0]["text"])["entry_id"]

        _, t_res = await self._rpc(
            client,
            tok_a,
            "tools/call",
            {"name": "hidl_add_task", "arguments": {"title": "Личная задача Артема"}},
        )
        a_task_id = json.loads(t_res["result"]["content"][0]["text"])["task_id"]

        await self._rpc(
            client,
            tok_a,
            "tools/call",
            {"name": "hidl_manage_shopping", "arguments": {"action": "add", "item_name": "Тофу шелковый"}},
        )

        # Partner B sees own identity + shared shopping, NEVER sees Partner A's diary or private tasks
        _, hh_b = await self._rpc(client, tok_b, "tools/call", {"name": "hidl_get_household", "arguments": {}})
        hh_b_data = json.loads(hh_b["result"]["content"][0]["text"])
        self.assertEqual(hh_b_data["acting_user"]["id"], self.b_id)
        self.assertEqual(hh_b_data["acting_user"]["name"], "Саша")

        _, shop_b = await self._rpc(
            client, tok_b, "tools/call", {"name": "hidl_list_shopping", "arguments": {"scope": "household"}}
        )
        shop_items = json.loads(shop_b["result"]["content"][0]["text"])["household"]
        self.assertTrue(any(i["item_name"] == "Тофу шелковый" for i in shop_items))

        _, tasks_b = await self._rpc(client, tok_b, "tools/call", {"name": "hidl_list_tasks", "arguments": {}})
        tasks_b_data = json.loads(tasks_b["result"]["content"][0]["text"])["day_plan_tasks"]
        self.assertFalse(any("Личная задача Артема" in t["title"] for t in tasks_b_data))

        _, diary_b = await self._rpc(client, tok_b, "tools/call", {"name": "hidl_get_diary", "arguments": {}})
        self.assertEqual(json.loads(diary_b["result"]["content"][0]["text"])["entries"], [])

        _, search_b = await self._rpc(client, tok_b, "tools/call", {"name": "search", "arguments": {"query": "Артема"}})
        self.assertEqual(json.loads(search_b["result"]["content"][0]["text"])["results"], [])

        _, fetch_t = await self._rpc(client, tok_b, "tools/call", {"name": "fetch", "arguments": {"id": f"task:{a_task_id}"}})
        self.assertTrue(fetch_t["result"]["isError"])
        _, fetch_d = await self._rpc(client, tok_b, "tools/call", {"name": "fetch", "arguments": {"id": f"diary:{a_diary_id}"}})
        self.assertTrue(fetch_d["result"]["isError"])

        # Refresh token rotation must preserve user binding (still Sasha!)
        ref_r = await client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": cid_b,
                "client_secret": csec_b,
                "refresh_token": rt_b,
            },
        )
        self.assertEqual(ref_r.status, 200)
        tok_b_rotated = (await ref_r.json())["access_token"]
        _, hh_b2 = await self._rpc(
            client, tok_b_rotated, "tools/call", {"name": "hidl_get_household", "arguments": {}}
        )
        self.assertEqual(json.loads(hh_b2["result"]["content"][0]["text"])["acting_user"]["id"], self.b_id)

    async def test_2_personal_bearer_token_and_scoped_revocation(self) -> None:
        client = await self._start_client()

        pat_a = (await repo.create_mcp_personal_token(self.db_conn, self.a_id))["token"]
        pat_b = (await repo.create_mcp_personal_token(self.db_conn, self.b_id))["token"]
        self.assertTrue(pat_a.startswith("hidl_pat_"))

        r_a, d_a = await self._rpc(client, pat_a, "tools/call", {"name": "hidl_get_household", "arguments": {}})
        self.assertEqual(r_a.status, 200)
        self.assertEqual(json.loads(d_a["result"]["content"][0]["text"])["acting_user"]["id"], self.a_id)

        r_b, d_b = await self._rpc(client, pat_b, "tools/call", {"name": "hidl_get_household", "arguments": {}})
        self.assertEqual(r_b.status, 200)
        self.assertEqual(json.loads(d_b["result"]["content"][0]["text"])["acting_user"]["id"], self.b_id)

        conns_a = await repo.list_user_mcp_connections(self.db_conn, self.a_id)
        self.assertGreaterEqual(len(conns_a), 1)

        # Revoke only Partner A -> Partner A gets 401, Partner B remains unaffected
        revoked = await repo.revoke_user_mcp_tokens(self.db_conn, self.a_id)
        self.assertGreaterEqual(revoked, 1)
        r_a_after, _ = await self._rpc(client, pat_a, "ping")
        self.assertEqual(r_a_after.status, 401)
        r_b_after, _ = await self._rpc(client, pat_b, "ping")
        self.assertEqual(r_b_after.status, 200)

    async def test_3_legacy_password_and_static_bearer_backward_compatibility(self) -> None:
        legacy_settings = MCPSettings(
            **{**self.settings_no_pw.__dict__, "access_password": "legacy-shared-password"}
        )
        client = await self._start_client(legacy_settings)

        # 1. Static bearer resolves to MCP_OWNER_TELEGRAM_ID
        r_st, d_st = await self._rpc(
            client, legacy_settings.bearer_token, "tools/call", {"name": "hidl_get_household", "arguments": {}}
        )
        self.assertEqual(r_st.status, 200)
        self.assertEqual(json.loads(d_st["result"]["content"][0]["text"])["acting_user"]["id"], self.a_id)

        # 2. Legacy OAuth password works when code isn't a /mcp code
        _, _, tok_leg, _ = await self._oauth_connect_with_code(
            client, "legacy-shared-password", client_name="Legacy Client"
        )
        r_leg, d_leg = await self._rpc(
            client, tok_leg, "tools/call", {"name": "hidl_get_household", "arguments": {}}
        )
        self.assertEqual(r_leg.status, 200)
        self.assertEqual(json.loads(d_leg["result"]["content"][0]["text"])["acting_user"]["id"], self.a_id)

        # 3. Legacy token row with user_id IS NULL also resolves to owner_telegram_id
        legacy_raw = "hidl_at_legacy_null_user_token"
        await self.db_conn.execute(
            """
            INSERT INTO mcp_oauth_tokens
            (token_hash, token_type, client_id, resource, scope, user_id, expires_at, revoked, created_at)
            VALUES (?, 'access', 'legacy_cid', 'https://mcp.example.com/mcp', 'hidl:read', NULL, '2099-01-01T00:00:00+00:00', 0, '2026-01-01T00:00:00+00:00')
            """,
            (hashlib.sha256(legacy_raw.encode("utf-8")).hexdigest(),),
        )
        await self.db_conn.commit()
        r_null, d_null = await self._rpc(
            client, legacy_raw, "tools/call", {"name": "hidl_get_household", "arguments": {}}
        )
        self.assertEqual(r_null.status, 200)
        self.assertEqual(json.loads(d_null["result"]["content"][0]["text"])["acting_user"]["id"], self.a_id)

    async def test_4_new_mcp_tools_household_recipes_and_nudge_partner(self) -> None:
        mock_bot = SimpleNamespace(send_message=AsyncMock(return_value=True))
        client = await self._start_client(telegram_bot=mock_bot)
        pat_b = (await repo.create_mcp_personal_token(self.db_conn, self.b_id))["token"]

        # Seed pantry, recipe (1 missing), due home task, and low supply
        await repo.create_pantry_item(self.db_conn, self.a_id, "Тофу", 400, "g", None, "белок")
        await repo.upsert_user_recipe(
            self.db_conn,
            self.a_id,
            title="Тофу с брокколи из теста",
            tags=["vegan", "lunch"],
            base_servings=2,
            time_minutes=15,
            ingredients=[
                {"name": "Тофу", "qty": 200, "unit": "g"},
                {"name": "Брокколи", "qty": 200, "unit": "g"},
            ],
            steps=["Обжарь тофу.", "Добавь брокколи."],
        )
        sup_id = await repo.add_supply_item(self.db_conn, self.a_id, "Таблетки для посудомойки", "кухня")
        await repo.update_supply_status(self.db_conn, self.a_id, sup_id, "low")

        # 1. hidl_list_recipes (query + almost_ready_only + include_steps)
        _, rec_r = await self._rpc(
            client,
            pat_b,
            "tools/call",
            {
                "name": "hidl_list_recipes",
                "arguments": {"query": "брокколи", "almost_ready_only": True, "include_steps": True},
            },
        )
        rec_data = json.loads(rec_r["result"]["content"][0]["text"])
        self.assertTrue(rec_data["almost_ready_only"])
        self.assertGreaterEqual(rec_data["total_matched"], 1)
        matched = rec_data["recipes"][0]
        self.assertEqual(matched["title"], "Тофу с брокколи из теста")
        self.assertEqual(matched["missing_count"], 1)
        self.assertEqual(len(matched["steps"]), 2)

        # 2. hidl_nudge_partner request + care thanks (+5 karma to partner + Telegram push + 2h cooldown)
        karma_a_before = int((await repo.get_or_create_karma_state(self.db_conn, self.a_id)).get("score") or 70)
        with patch("services.household_service.is_partner_quiet_or_paused", return_value=False):
            _, req_r = await self._rpc(
                client,
                pat_b,
                "tools/call",
                {"name": "hidl_nudge_partner", "arguments": {"action": "request", "text": "Захвати овсяное молоко"}},
            )
            req_data = json.loads(req_r["result"]["content"][0]["text"])
            self.assertTrue(req_data["ok"])
            self.assertEqual(req_data["sent"], 1)

            _, care_r = await self._rpc(
                client,
                pat_b,
                "tools/call",
                {"name": "hidl_nudge_partner", "arguments": {"action": "care", "nudge_key": "thanks"}},
            )
            care_data = json.loads(care_r["result"]["content"][0]["text"])
            self.assertTrue(care_data["ok"])
            self.assertEqual(care_data["karma_given"], 5)

            # Second identical "thanks" within 2 hours hits cooldown
            _, cool_r = await self._rpc(
                client,
                pat_b,
                "tools/call",
                {"name": "hidl_nudge_partner", "arguments": {"action": "care", "nudge_key": "thanks"}},
            )
            cool_data = json.loads(cool_r["result"]["content"][0]["text"])
            self.assertFalse(cool_data["ok"])
            self.assertTrue(cool_data["cooldown"])

        karma_a_after = int((await repo.get_or_create_karma_state(self.db_conn, self.a_id)).get("score") or 70)
        self.assertEqual(karma_a_after, karma_a_before + 5)
        self.assertEqual(mock_bot.send_message.await_count, 2)
        self.assertEqual(mock_bot.send_message.call_args.args[0], self.a_tg)

        # 3. hidl_get_household & hidl://household resource show open request + low supply
        _, hh_r = await self._rpc(client, pat_b, "tools/call", {"name": "hidl_get_household", "arguments": {}})
        hh_data = json.loads(hh_r["result"]["content"][0]["text"])
        self.assertEqual(len(hh_data["household"]["members"]), 2)
        self.assertTrue(any("Захвати овсяное молоко" in r["text"] for r in hh_data["open_requests"]))
        self.assertTrue(any(s["name"] == "Таблетки для посудомойки" for s in hh_data["low_supplies"]))

        _, res_hh = await self._rpc(client, pat_b, "resources/read", {"uri": "hidl://household"})
        self.assertIn("Захвати овсяное молоко", res_hh["result"]["contents"][0]["text"])

    async def test_5_telegram_mcp_handlers_and_student_guard(self) -> None:
        # Student-mode guard
        stu_tg = 660500
        stu_id = await repo.create_user(
            self.db_conn,
            telegram_id=stu_tg,
            name="Ученик",
            timezone="Europe/Moscow",
            wake_up_time="08:00",
            sleep_time="23:00",
        )
        await repo.update_user_profile_mode(self.db_conn, stu_id, "student")
        stu_msg = SimpleNamespace(
            from_user=SimpleNamespace(id=stu_tg, full_name="Ученик"),
            answer=AsyncMock(),
        )
        await mcp_connect.mcp_cmd(stu_msg, self.db_conn)
        self.assertIn("взрослом режиме", stu_msg.answer.call_args.args[0])

        # Adult partner flow: /mcp -> code -> PAT -> revoke ask -> revoke confirm
        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=self.b_tg, full_name="Саша"),
            answer=AsyncMock(),
            edit_text=AsyncMock(),
        )
        await mcp_connect.mcp_cmd(msg, self.db_conn)
        hub_text = msg.answer.call_args.args[0]
        self.assertIn("/mcp", hub_text)

        cb_code = SimpleNamespace(
            data="mcp:code:new",
            from_user=SimpleNamespace(id=self.b_tg, full_name="Саша"),
            message=msg,
            answer=AsyncMock(),
        )
        await mcp_connect.mcp_new_connect_code(cb_code, self.db_conn)
        code_html = msg.edit_text.call_args.args[0]
        self.assertIn("HIDL-", code_html)

        cb_pat = SimpleNamespace(
            data="mcp:pat:new",
            from_user=SimpleNamespace(id=self.b_tg, full_name="Саша"),
            message=msg,
            answer=AsyncMock(),
        )
        await mcp_connect.mcp_new_personal_token(cb_pat, self.db_conn)
        pat_html = msg.edit_text.call_args.args[0]
        self.assertIn("hidl_pat_", pat_html)
        self.assertEqual(len(await repo.list_user_mcp_connections(self.db_conn, self.b_id)), 1)

        cb_ask = SimpleNamespace(
            data="mcp:revoke:ask",
            from_user=SimpleNamespace(id=self.b_tg, full_name="Саша"),
            message=msg,
            answer=AsyncMock(),
        )
        await mcp_connect.mcp_revoke_ask(cb_ask, self.db_conn)
        self.assertIn("Отозвать все", msg.edit_text.call_args.args[0])

        cb_rev = SimpleNamespace(
            data="mcp:revoke:confirm",
            from_user=SimpleNamespace(id=self.b_tg, full_name="Саша"),
            message=msg,
            answer=AsyncMock(),
        )
        await mcp_connect.mcp_revoke_confirm(cb_rev, self.db_conn)
        self.assertIn("Отозвано токенов:", msg.edit_text.call_args.args[0])
        self.assertEqual(await repo.list_user_mcp_connections(self.db_conn, self.b_id), [])


if __name__ == "__main__":
    unittest.main()
