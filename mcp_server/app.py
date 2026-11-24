"""Streamable HTTP aiohttp application for HiDL remote MCP server."""

from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import hashlib
import hmac
import html
import json
import os
import secrets
import sqlite3
import tempfile
import time
from typing import Any, Optional

from aiohttp import web
import aiosqlite

from config import MCPSettings, get_mcp_settings, DeskIntegrationSettings, get_desk_settings
from db.database import connect
from services import desk_service
from services import calendar_service
from mcp_server.oauth import (
    handle_authorization_server_metadata,
    handle_oauth_authorize_get,
    handle_oauth_authorize_post,
    handle_oauth_register,
    handle_oauth_token,
    handle_protected_resource_metadata,
    unauthorized_response,
    verify_bearer_request,
)
from mcp_server.tools import (
    ToolError,
    call_mcp_tool,
    list_enabled_resources,
    list_enabled_tools,
    read_mcp_resource,
)

_SUPPORTED_PROTOCOL_VERSIONS = {"2025-06-18", "2025-03-26", "2024-11-05"}
_DEFAULT_PROTOCOL_VERSION = "2025-03-26"


async def _desk_auth(request: web.Request) -> tuple[bool, bytes, web.Response | None]:
    cfg: DeskIntegrationSettings = request.app["desk_settings"]
    if not cfg.webhook_secret:
        return False, b"", web.json_response({"ok": False, "error": "integration_not_configured"}, status=503)
    raw = await request.read()
    stamp = request.headers.get("X-Desk-Timestamp", "")
    signature = request.headers.get("X-Desk-Signature", "")
    if not stamp or len(stamp) > 12 or not stamp.isascii() or not stamp.isdigit():
        return False, raw, web.json_response({"ok": False, "error": "invalid_signature"}, status=401)
    stamp_num = int(stamp)
    if abs(int(time.time()) - stamp_num) > 300:
        return False, raw, web.json_response({"ok": False, "error": "invalid_signature"}, status=401)
    expected = "sha256=" + hmac.new(cfg.webhook_secret.encode("utf-8"), stamp.encode("ascii") + b"." + raw, hashlib.sha256).hexdigest()
    if not signature.isascii() or not hmac.compare_digest(expected, signature):
        return False, raw, web.json_response({"ok": False, "error": "invalid_signature"}, status=401)
    return True, raw, None


async def _desk_connection(request: web.Request):
    path = request.app.get("desk_database_path")
    if path is None:
        rows = await request.app["db_conn"].execute_fetchall("PRAGMA database_list")
        path = (rows[0]["file"] if rows else "") or ":memory:"
        request.app["desk_database_path"] = path
    if path == ":memory:":
        return request.app["db_conn"], False
    return await connect(path), True


async def handle_desk_link_tokens(request: web.Request) -> web.Response:
    ok, raw, failure = await _desk_auth(request)
    if not ok:
        return failure
    if not request.app["desk_settings"].bot_username:
        return web.json_response({"ok": False, "error": "integration_not_configured"}, status=503)
    try:
        body = json.loads(raw.decode("utf-8"))
        if not isinstance(body, dict):
            raise ValueError("invalid_body")
        conn, owned = await _desk_connection(request)
        try:
            result = await desk_service.create_link_token(
                conn, desk_user_id=body.get("deskUserId"), role=body.get("role"),
                display_name=body.get("displayName"), timezone=body.get("timezone"),
                settings=request.app["desk_settings"],
            )
        finally:
            if owned:
                await conn.close()
        return web.json_response({"ok": True, **result})
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return web.json_response({"ok": False, "error": str(exc) or "invalid_body"}, status=400)


async def handle_desk_link_get(request: web.Request) -> web.Response:
    ok, _, failure = await _desk_auth(request)
    if not ok:
        return failure
    conn, owned = await _desk_connection(request)
    try:
        link = await desk_service.get_link_for_desk_user(conn, request.match_info["deskUserId"])
    finally:
        if owned:
            await conn.close()
    if not link:
        return web.json_response({"linked": False})
    return web.json_response({"linked": True, "role": link["role"], "linkedAt": link["linked_at"]})


async def handle_desk_link_delete(request: web.Request) -> web.Response:
    ok, _, failure = await _desk_auth(request)
    if not ok:
        return failure
    conn, owned = await _desk_connection(request)
    try:
        unlinked = await desk_service.unlink_desk_user(conn, request.match_info["deskUserId"])
    finally:
        if owned:
            await conn.close()
    return web.json_response({"ok": True, "unlinked": unlinked})


async def handle_desk_webhook(request: web.Request) -> web.Response:
    ok, raw, failure = await _desk_auth(request)
    if not ok:
        return failure
    event_header = request.headers.get("X-Desk-Event-Id", "")
    try:
        payload = json.loads(raw.decode("utf-8"))
        desk_service.validate_envelope(payload, event_header)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return web.json_response({"ok": False, "error": str(exc) or "invalid_envelope"}, status=400)
    conn, owned = await _desk_connection(request)
    try:
        result = await desk_service.ingest_event(conn, payload, settings=request.app["desk_settings"])
    finally:
        if owned:
            await conn.close()
    if result.get("not_linked"):
        return web.json_response({"ok": False, "error": "user_not_linked"}, status=404)
    return web.json_response({"ok": True, "duplicate": bool(result.get("duplicate"))})


def _jsonrpc_result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


async def _dispatch_jsonrpc(
    payload: Any,
    db: aiosqlite.Connection,
    settings: MCPSettings,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Handle one JSON-RPC 2.0 object. Returns (response_obj_or_None, optional_new_session_id)."""
    if not isinstance(payload, dict):
        return _jsonrpc_error(None, -32600, "Invalid Request"), None

    method = payload.get("method")
    req_id = payload.get("id")
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}

    # Notifications carry no id and expect no JSON-RPC response body.
    if req_id is None:
        return None, None

    if not isinstance(method, str) or not method:
        return _jsonrpc_error(req_id, -32600, "Invalid Request: missing method"), None

    if method == "initialize":
        req_ver = str(params.get("protocolVersion") or "").strip()
        negotiated = req_ver if req_ver in _SUPPORTED_PROTOCOL_VERSIONS else _DEFAULT_PROTOCOL_VERSION
        new_session_id = f"hidl_{secrets.token_hex(12)}"
        return (
            _jsonrpc_result(
                req_id,
                {
                    "protocolVersion": negotiated,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"listChanged": False},
                    },
                    "serverInfo": {"name": "hidl-mcp", "version": "2.0.0"},
                    "instructions": (
                        "Персональный ассистент HiDL 2.0: можно читать и обновлять задачи на день, "
                        "напоминания, расписание и Google Calendar, меню на 3–4 дня, карму и портрет привычек, "
                        "список покупок, холодильник/рецепты, расходы и (если разрешено) дневник."
                    ),
                },
            ),
            new_session_id,
        )

    if method == "ping":
        return _jsonrpc_result(req_id, {}), None

    if method == "tools/list":
        return _jsonrpc_result(req_id, {"tools": list_enabled_tools(settings)}), None

    if method == "tools/call":
        name = str(params.get("name") or "").strip()
        if not name:
            return _jsonrpc_error(req_id, -32602, "Missing tool name"), None
        arguments = params.get("arguments")
        result = await call_mcp_tool(db, settings, name, arguments)
        return _jsonrpc_result(req_id, result), None

    if method == "resources/list":
        return _jsonrpc_result(req_id, {"resources": list_enabled_resources(settings)}), None

    if method == "resources/read":
        uri = str(params.get("uri") or "").strip()
        if not uri:
            return _jsonrpc_error(req_id, -32602, "Missing resource uri"), None
        try:
            res = await read_mcp_resource(db, settings, uri)
            return _jsonrpc_result(req_id, res), None
        except ToolError as exc:
            return _jsonrpc_error(req_id, -32002, str(exc)), None

    if method == "prompts/list":
        return _jsonrpc_result(req_id, {"prompts": []}), None

    return _jsonrpc_error(req_id, -32601, f"Method not found: {method}"), None


@web.middleware
async def cors_middleware(request: web.Request, handler) -> web.StreamResponse:
    if request.method == "OPTIONS" and request.path.startswith("/integrations/desk/"):
        valid, _, failure = await _desk_auth(request)
        resp: web.StreamResponse = web.Response(status=204) if valid else failure
    elif request.method == "OPTIONS":
        resp: web.StreamResponse = web.Response(status=204)
    else:
        resp = await handler(request)
    origin = request.headers.get("Origin") or "*"
    resp.headers["Access-Control-Allow-Origin"] = origin
    resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = (
        "Authorization, Content-Type, Accept, Mcp-Session-Id, MCP-Protocol-Version, X-Desk-Timestamp, X-Desk-Signature, X-Desk-Event-Id"
    )
    resp.headers["Access-Control-Expose-Headers"] = "Mcp-Session-Id, WWW-Authenticate"
    return resp


async def handle_index(request: web.Request) -> web.Response:
    body = (
        "<!doctype html><html lang='ru'><meta charset='utf-8'>"
        "<title>HiDL — Personal Assistant</title>"
        "<body style='font-family:system-ui,sans-serif;max-width:680px;margin:48px auto;padding:0 20px;line-height:1.6'>"
        "<h1>HiDL — персональный ассистент</h1>"
        "<p>Помощник для распорядка дня, задач, умной кухни и синхронизации с Google Calendar (Telegram + MCP).</p>"
        "<p><a href='/privacy'>Политика конфиденциальности / Privacy Policy</a> · "
        "<a href='/health'>Статус сервиса</a></p>"
        "</body></html>"
    )
    return web.Response(text=body, content_type="text/html", status=200)


async def handle_privacy(request: web.Request) -> web.Response:
    body = (
        "<!doctype html><html lang='ru'><meta charset='utf-8'>"
        "<title>HiDL Privacy Policy</title>"
        "<body style='font-family:system-ui,sans-serif;max-width:720px;margin:48px auto;padding:0 20px;line-height:1.6'>"
        "<h1>Политика конфиденциальности / Privacy Policy — HiDL</h1>"
        "<p><strong>Данные Google Calendar:</strong> при подключении Google OAuth 2.0 HiDL запрашивает доступ "
        "к событиям вашего календаря исключительно для их отображения в боте и создания новых событий по вашему прямому запросу.</p>"
        "<p><strong>Хранение и передача:</strong> токены доступа и кэш событий хранятся локально в защищённой базе данных "
        "сервера, не передаются третьим лицам и не используются для рекламы.</p>"
        "<p><strong>Удаление и отзыв доступа:</strong> вы можете отключить интеграцию в любой момент командой <code>/gcal</code> "
        "(кнопка «Отключить» удаляет токены и кэш событий из БД) либо в настройках безопасности вашего аккаунта Google.</p>"
        "<hr>"
        "<p><strong>English summary:</strong> HiDL accesses Google Calendar data solely to display upcoming events, "
        "protect busy meeting slots from notifications, and create events requested by the user. Data is never sold or shared "
        "and can be disconnected at any time via Telegram (<code>/gcal</code>) or Google Account permissions.</p>"
        "</body></html>"
    )
    return web.Response(text=body, content_type="text/html", status=200)


async def handle_health(request: web.Request) -> web.Response:
    settings: MCPSettings = request.app["mcp_settings"]
    return web.json_response(
        {
            "ok": True,
            "service": "hidl-mcp",
            "diary_enabled": settings.allow_diary,
            "readonly": settings.readonly,
        }
    )


async def _resolve_app_db_path(app: web.Application) -> str:
    path = app.get("desk_database_path")
    if path is None:
        rows = await app["db_conn"].execute_fetchall("PRAGMA database_list")
        path = (rows[0]["file"] if rows else "") or ":memory:"
        app["desk_database_path"] = path
    return str(path)


def _verify_static_ops_bearer(request: web.Request) -> Optional[web.Response]:
    cfg: MCPSettings = request.app["mcp_settings"]
    expected = (cfg.bearer_token or "").strip()
    if not expected:
        return web.json_response({"ok": False, "error": "ops_token_not_configured"}, status=503)
    auth = (request.headers.get("Authorization") or "").strip()
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    token = parts[1].strip()
    if not token or not hmac.compare_digest(token, expected):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    return None


def _read_bot_heartbeat(db_path: str) -> tuple[Optional[float], bool]:
    if not db_path or db_path == ":memory:" or db_path.startswith("file::memory:"):
        return None, False
    hb_path = os.path.join(os.path.dirname(db_path) or ".", "bot_heartbeat.json")
    if not os.path.exists(hb_path):
        return None, False
    try:
        with open(hb_path, encoding="utf-8") as f:
            payload = json.load(f)
        ts = float(payload.get("ts") or 0.0)
        if ts <= 0:
            return None, False
        age = round(max(0.0, time.time() - ts), 1)
        return age, age < 180.0
    except Exception:
        return None, False


def _create_verified_sqlite_gz(db_path: str) -> bytes:
    with tempfile.TemporaryDirectory(prefix="hidl_bk_") as td:
        out_db = os.path.join(td, "hidl_backup.db")
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        dst = sqlite3.connect(out_db)
        try:
            src.backup(dst)
            chk = dst.execute("PRAGMA integrity_check").fetchone()[0]
            assert chk == "ok", f"integrity_check failed: {chk}"
        finally:
            dst.close()
            src.close()
        with open(out_db, "rb") as f:
            raw = f.read()
        return gzip.compress(raw)


async def handle_internal_status(request: web.Request) -> web.Response:
    err = _verify_static_ops_bearer(request)
    if err is not None:
        return err
    db: aiosqlite.Connection = request.app["db_conn"]
    cur = await db.execute("SELECT COUNT(*) FROM users")
    row = await cur.fetchone()
    users_count = int(row[0] if row else 0)
    db_path = await _resolve_app_db_path(request.app)
    age, bot_alive = _read_bot_heartbeat(db_path)
    return web.json_response(
        {
            "ok": True,
            "service": "hidl-mcp",
            "db_ok": True,
            "users_count": users_count,
            "bot_heartbeat_age_sec": age,
            "bot_alive": bot_alive,
        }
    )


async def handle_internal_backup(request: web.Request) -> web.Response:
    err = _verify_static_ops_bearer(request)
    if err is not None:
        return err
    db_path = await _resolve_app_db_path(request.app)
    if not db_path or db_path == ":memory:" or db_path.startswith("file::memory:"):
        return web.json_response({"ok": False, "error": "in_memory_db"}, status=400)
    gz_bytes = await asyncio.to_thread(_create_verified_sqlite_gz, db_path)
    return web.Response(
        body=gz_bytes,
        content_type="application/gzip",
        headers={"Content-Disposition": 'attachment; filename="hidl.db.gz"'},
    )


async def handle_gcal_callback(request: web.Request) -> web.Response:
    db: aiosqlite.Connection = request.app["db_conn"]
    err = (request.query.get("error") or "").strip()
    if err:
        body = (
            "<!doctype html><html lang='ru'><meta charset='utf-8'><body>"
            f"<h2>Авторизация Google Calendar отменена</h2><p>{html.escape(err)}</p>"
            "</body></html>"
        )
        return web.Response(text=body, content_type="text/html", status=400)

    code = (request.query.get("code") or "").strip()
    state = (request.query.get("state") or "").strip()
    ok, msg = await calendar_service.complete_gcal_oauth_callback(db, code=code, state=state)
    title = "Google Calendar подключён" if ok else "Ошибка подключения Google Calendar"
    body = (
        "<!doctype html><html lang='ru'><meta charset='utf-8'><body>"
        f"<h2>{title}</h2><p>{html.escape(msg)}</p>"
        "</body></html>"
    )
    return web.Response(text=body, content_type="text/html", status=200 if ok else 400)


async def handle_mcp_post(request: web.Request) -> web.Response:
    db: aiosqlite.Connection = request.app["db_conn"]
    settings: MCPSettings = request.app["mcp_settings"]
    if not await verify_bearer_request(request, db, settings):
        return unauthorized_response(settings)

    try:
        raw_text = await request.text()
        payload = json.loads(raw_text)
    except Exception:
        return web.json_response(
            _jsonrpc_error(None, -32700, "Parse error"),
            status=400,
        )

    existing_sid = (request.headers.get("Mcp-Session-Id") or "").strip()
    headers: dict[str, str] = {"Cache-Control": "no-store"}
    if existing_sid:
        headers["Mcp-Session-Id"] = existing_sid

    if isinstance(payload, list):
        if not payload:
            return web.json_response(_jsonrpc_error(None, -32600, "Invalid Request"), status=400)
        responses: list[dict[str, Any]] = []
        for item in payload:
            resp_item, new_sid = await _dispatch_jsonrpc(item, db, settings)
            if new_sid:
                headers["Mcp-Session-Id"] = new_sid
            if resp_item is not None:
                responses.append(resp_item)
        if not responses:
            return web.Response(status=202, headers=headers)
        return web.json_response(responses, status=200, headers=headers)

    resp_obj, new_sid = await _dispatch_jsonrpc(payload, db, settings)
    if new_sid:
        headers["Mcp-Session-Id"] = new_sid
    if resp_obj is None:
        return web.Response(status=202, headers=headers)
    return web.json_response(resp_obj, status=200, headers=headers)


async def handle_mcp_get(request: web.Request) -> web.StreamResponse:
    db: aiosqlite.Connection = request.app["db_conn"]
    settings: MCPSettings = request.app["mcp_settings"]
    if not await verify_bearer_request(request, db, settings):
        return unauthorized_response(settings)

    accept = (request.headers.get("Accept") or "").lower()
    if "text/event-stream" not in accept:
        return web.json_response(
            {"ok": True, "transport": "streamable-http", "endpoint": "/mcp"},
            status=200,
        )

    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
    }
    sid = (request.headers.get("Mcp-Session-Id") or "").strip()
    if sid:
        headers["Mcp-Session-Id"] = sid

    stream = web.StreamResponse(status=200, headers=headers)
    await stream.prepare(request)
    try:
        while not request.app.get("mcp_shutting_down", False):
            await stream.write(b": keepalive\n\n")
            await asyncio.sleep(20)
    except (asyncio.CancelledError, ConnectionResetError, Exception):
        pass
    return stream


async def handle_mcp_delete(request: web.Request) -> web.Response:
    db: aiosqlite.Connection = request.app["db_conn"]
    settings: MCPSettings = request.app["mcp_settings"]
    if not await verify_bearer_request(request, db, settings):
        return unauthorized_response(settings)
    return web.Response(status=204)


def create_mcp_app(
    db_conn: aiosqlite.Connection,
    settings: Optional[MCPSettings] = None,
) -> web.Application:
    """Create configured aiohttp Application serving HiDL Streamable HTTP MCP + OAuth 2.1."""
    cfg = settings or get_mcp_settings()
    app = web.Application(middlewares=[cors_middleware])
    app["db_conn"] = db_conn
    app["desk_database_path"] = None
    app["desk_settings"] = get_desk_settings()
    app["mcp_settings"] = cfg
    app["mcp_csrf_key"] = secrets.token_bytes(32)
    app["mcp_shutting_down"] = False

    async def _on_shutdown(application: web.Application) -> None:
        application["mcp_shutting_down"] = True

    app.on_shutdown.append(_on_shutdown)

    app.router.add_get("/", handle_index)
    app.router.add_get("/privacy", handle_privacy)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/internal/status", handle_internal_status)
    app.router.add_get("/internal/backup", handle_internal_backup)
    app.router.add_get("/gcal/callback", handle_gcal_callback)

    app.router.add_post("/integrations/desk/link-tokens", handle_desk_link_tokens)
    app.router.add_get("/integrations/desk/links/{deskUserId}", handle_desk_link_get)
    app.router.add_delete("/integrations/desk/links/{deskUserId}", handle_desk_link_delete)
    app.router.add_post("/integrations/desk/webhook", handle_desk_webhook)

    # RFC 9728 Protected Resource Metadata & RFC 8414 Authorization Server Metadata
    app.router.add_get("/.well-known/oauth-protected-resource", handle_protected_resource_metadata)
    app.router.add_get("/.well-known/oauth-protected-resource/mcp", handle_protected_resource_metadata)
    app.router.add_get("/.well-known/oauth-authorization-server", handle_authorization_server_metadata)
    app.router.add_get("/.well-known/oauth-authorization-server/mcp", handle_authorization_server_metadata)
    app.router.add_get("/.well-known/openid-configuration", handle_authorization_server_metadata)
    app.router.add_get("/.well-known/openid-configuration/mcp", handle_authorization_server_metadata)

    # OAuth 2.1 endpoints
    app.router.add_post("/oauth/register", handle_oauth_register)
    app.router.add_get("/oauth/authorize", handle_oauth_authorize_get)
    app.router.add_post("/oauth/authorize", handle_oauth_authorize_post)
    app.router.add_post("/oauth/token", handle_oauth_token)

    # MCP Streamable HTTP endpoints
    app.router.add_post("/mcp", handle_mcp_post)
    app.router.add_get("/mcp", handle_mcp_get)
    app.router.add_delete("/mcp", handle_mcp_delete)
    return app
