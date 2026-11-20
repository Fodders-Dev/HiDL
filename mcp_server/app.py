"""Streamable HTTP aiohttp application for HiDL remote MCP server."""

from __future__ import annotations

import asyncio
import html
import json
import secrets
from typing import Any, Optional

from aiohttp import web
import aiosqlite

from config import MCPSettings, get_mcp_settings
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
    if request.method == "OPTIONS":
        resp: web.StreamResponse = web.Response(status=204)
    else:
        resp = await handler(request)
    origin = request.headers.get("Origin") or "*"
    resp.headers["Access-Control-Allow-Origin"] = origin
    resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = (
        "Authorization, Content-Type, Accept, Mcp-Session-Id, MCP-Protocol-Version"
    )
    resp.headers["Access-Control-Expose-Headers"] = "Mcp-Session-Id, WWW-Authenticate"
    return resp


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
    app["mcp_settings"] = cfg
    app["mcp_csrf_key"] = secrets.token_bytes(32)
    app["mcp_shutting_down"] = False

    async def _on_shutdown(application: web.Application) -> None:
        application["mcp_shutting_down"] = True

    app.on_shutdown.append(_on_shutdown)

    app.router.add_get("/health", handle_health)
    app.router.add_get("/gcal/callback", handle_gcal_callback)

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
