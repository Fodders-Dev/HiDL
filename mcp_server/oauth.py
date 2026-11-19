"""OAuth 2.1 (PKCE S256 + RFC 7591 DCR + RFC 9728/8414 discovery) for HiDL MCP."""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import html
import json
import secrets
from typing import Any, Optional
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlparse, urlunparse

from aiohttp import web
import aiosqlite

from config import MCPSettings


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _hash_secret(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _supported_scopes(settings: MCPSettings) -> list[str]:
    scopes = ["hidl:read"]
    if not settings.readonly:
        scopes.append("hidl:write")
    scopes.append("offline_access")
    return scopes


def _resource_url(settings: MCPSettings) -> str:
    return f"{settings.public_base_url.rstrip('/')}/mcp"


def _is_valid_redirect_uri(uri: str) -> bool:
    try:
        parsed = urlparse(uri)
    except Exception:
        return False
    if parsed.scheme == "https" and bool(parsed.netloc):
        return True
    if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        return True
    return False


def _is_valid_resource(resource: str, settings: MCPSettings) -> bool:
    if not resource:
        return True
    norm = resource.rstrip("/")
    base = settings.public_base_url.rstrip("/")
    return norm in {base, f"{base}/mcp"}


def _sign_csrf(
    csrf_key: bytes,
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    issued_ts: str,
) -> str:
    msg = "|".join([client_id, redirect_uri, code_challenge, issued_ts]).encode("utf-8")
    sig = hmac.new(csrf_key, msg, hashlib.sha256).hexdigest()
    return f"{issued_ts}.{sig}"


def _verify_csrf(
    csrf_key: bytes,
    token: str,
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    max_age_sec: int = 900,
) -> bool:
    if "." not in (token or ""):
        return False
    ts_str, sig = token.split(".", 1)
    try:
        issued = int(ts_str)
    except ValueError:
        return False
    now_ts = int(_utc_now().timestamp())
    if issued > now_ts + 30 or now_ts - issued > max_age_sec:
        return False
    expected = _sign_csrf(
        csrf_key,
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        issued_ts=ts_str,
    ).split(".", 1)[1]
    return hmac.compare_digest(sig, expected)


def _append_query_params(url: str, params: dict[str, str]) -> str:
    parsed = urlparse(url)
    existing = dict(parse_qsl(parsed.query, keep_blank_values=True))
    existing.update({k: v for k, v in params.items() if v != ""})
    new_query = urlencode(existing)
    return urlunparse(parsed._replace(query=new_query))


def _oauth_error_json(status: int, err: str, desc: str) -> web.Response:
    return web.json_response(
        {"error": err, "error_description": desc},
        status=status,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def unauthorized_response(settings: MCPSettings) -> web.Response:
    base = settings.public_base_url.rstrip("/")
    return web.json_response(
        {"error": "unauthorized", "error_description": "Bearer authentication required."},
        status=401,
        headers={
            "WWW-Authenticate": (
                f'Bearer error="invalid_token", '
                f'resource_metadata="{base}/.well-known/oauth-protected-resource"'
            ),
            "Cache-Control": "no-store",
        },
    )


async def verify_bearer_request(
    request: web.Request,
    db: aiosqlite.Connection,
    settings: MCPSettings,
) -> bool:
    """Validate Authorization Bearer token against static override or OAuth access tokens."""
    auth_header = (request.headers.get("Authorization") or "").strip()
    if not auth_header.lower().startswith("bearer "):
        return False
    raw_token = auth_header[7:].strip()
    if not raw_token:
        return False

    if settings.bearer_token and hmac.compare_digest(raw_token, settings.bearer_token):
        return True

    token_hash = _hash_secret(raw_token)
    cursor = await db.execute(
        """
        SELECT expires_at, revoked FROM mcp_oauth_tokens
        WHERE token_hash = ? AND token_type = 'access'
        """,
        (token_hash,),
    )
    row = await cursor.fetchone()
    if not row or int(row["revoked"] or 0) != 0:
        return False
    try:
        exp = datetime.datetime.fromisoformat(row["expires_at"])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return False
    return exp > _utc_now()


async def handle_protected_resource_metadata(request: web.Request) -> web.Response:
    settings: MCPSettings = request.app["mcp_settings"]
    base = settings.public_base_url.rstrip("/")
    return web.json_response(
        {
            "resource": _resource_url(settings),
            "authorization_servers": [base],
            "bearer_methods_supported": ["header"],
            "scopes_supported": _supported_scopes(settings),
        }
    )


async def handle_authorization_server_metadata(request: web.Request) -> web.Response:
    settings: MCPSettings = request.app["mcp_settings"]
    base = settings.public_base_url.rstrip("/")
    return web.json_response(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": [
                "none",
                "client_secret_post",
                "client_secret_basic",
            ],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": _supported_scopes(settings),
        }
    )


async def handle_oauth_register(request: web.Request) -> web.Response:
    db: aiosqlite.Connection = request.app["db_conn"]
    try:
        payload = await request.json()
    except Exception:
        return _oauth_error_json(400, "invalid_client_metadata", "Expected JSON body.")
    if not isinstance(payload, dict):
        return _oauth_error_json(400, "invalid_client_metadata", "Expected JSON object.")

    redirect_uris = payload.get("redirect_uris")
    if (
        not isinstance(redirect_uris, list)
        or not redirect_uris
        or not all(isinstance(u, str) and _is_valid_redirect_uri(u) for u in redirect_uris)
    ):
        return _oauth_error_json(400, "invalid_redirect_uri", "Valid redirect_uris list is required.")

    client_name = str(payload.get("client_name") or "MCP Client").strip()[:120]
    auth_method = str(payload.get("token_endpoint_auth_method") or "client_secret_post").strip()
    if auth_method not in {"none", "client_secret_post", "client_secret_basic"}:
        return _oauth_error_json(400, "invalid_client_metadata", "Unsupported token_endpoint_auth_method.")

    client_id = f"hidl_{secrets.token_hex(12)}"
    client_secret: Optional[str] = None
    secret_hash = ""
    if auth_method != "none":
        client_secret = f"hidl_sec_{secrets.token_urlsafe(24)}"
        secret_hash = _hash_secret(client_secret)

    now_dt = _utc_now()
    await db.execute(
        """
        INSERT INTO mcp_oauth_clients (client_id, client_secret_hash, client_name, redirect_uris_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (client_id, secret_hash, client_name, json.dumps(redirect_uris), now_dt.isoformat()),
    )
    await db.commit()

    resp_data: dict[str, Any] = {
        "client_id": client_id,
        "client_id_issued_at": int(now_dt.timestamp()),
        "client_name": client_name,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": auth_method,
    }
    if client_secret is not None:
        resp_data["client_secret"] = client_secret
        resp_data["client_secret_expires_at"] = 0
    return web.json_response(resp_data, status=201, headers={"Cache-Control": "no-store"})


async def _load_and_validate_authorize_params(
    params: dict[str, str],
    db: aiosqlite.Connection,
    settings: MCPSettings,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    response_type = params.get("response_type", "").strip()
    client_id = params.get("client_id", "").strip()
    redirect_uri = params.get("redirect_uri", "").strip()
    code_challenge = params.get("code_challenge", "").strip()
    code_challenge_method = params.get("code_challenge_method", "").strip() or "plain"
    resource = params.get("resource", "").strip()

    if response_type != "code":
        return None, "Поддерживается только response_type=code."
    if not client_id or not redirect_uri:
        return None, "Не переданы client_id или redirect_uri."

    cur = await db.execute(
        "SELECT client_id, client_name, redirect_uris_json FROM mcp_oauth_clients WHERE client_id = ?",
        (client_id,),
    )
    client_row = await cur.fetchone()
    if not client_row:
        return None, "Неизвестный client_id. Сначала зарегистрируйте клиент через /oauth/register."

    try:
        allowed_uris = json.loads(client_row["redirect_uris_json"])
    except Exception:
        allowed_uris = []
    if redirect_uri not in allowed_uris:
        return None, "redirect_uri не совпадает с зарегистрированным."

    if not code_challenge or code_challenge_method != "S256":
        return None, "Требуется PKCE с code_challenge_method=S256."
    if not _is_valid_resource(resource, settings):
        return None, "Неподдерживаемый параметр resource."

    return {
        "client_id": client_id,
        "client_name": client_row["client_name"] or client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "state": params.get("state", ""),
        "scope": params.get("scope", "") or " ".join(_supported_scopes(settings)),
        "resource": resource or _resource_url(settings),
    }, None


def _render_consent_html(
    settings: MCPSettings,
    auth_req: dict[str, Any],
    csrf_token: str,
    *,
    error_message: str = "",
) -> str:
    diary_badge = (
        '<span class="badge ok">Дневник и идеи: разрешены</span>'
        if settings.allow_diary
        else '<span class="badge off">Дневник: отключён</span>'
    )
    mode_badge = (
        '<span class="badge off">Режим: только чтение</span>'
        if settings.readonly
        else '<span class="badge ok">Режим: чтение и запись</span>'
    )
    err_block = f'<p class="err">{html.escape(error_message)}</p>' if error_message else ""
    hidden_fields = "\n".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(str(auth_req[k]))}">'
        for k in (
            "response_type",
            "client_id",
            "redirect_uri",
            "code_challenge",
            "code_challenge_method",
            "state",
            "scope",
            "resource",
        )
        if k in auth_req
    )
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Доступ к HiDL MCP</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:#0f172a; color:#e2e8f0; margin:0; padding:24px; display:flex; min-height:100vh; align-items:center; justify-content:center; }}
  .card {{ background:#1e293b; border:1px solid #334155; border-radius:14px; max-width:440px; width:100%; padding:24px; box-sizing:border-box; }}
  h1 {{ font-size:20px; margin:0 0 8px; }}
  p {{ color:#cbd5e1; font-size:14px; line-height:1.45; }}
  .badges {{ display:flex; flex-wrap:wrap; gap:8px; margin:12px 0 18px; }}
  .badge {{ font-size:12px; padding:4px 10px; border-radius:999px; background:#334155; }}
  .badge.ok {{ background:#14532d; color:#bbf7d0; }}
  .badge.off {{ background:#7c2d12; color:#fed7aa; }}
  label {{ display:block; font-size:13px; margin-bottom:6px; color:#cbd5e1; }}
  input[type=password] {{ width:100%; padding:10px 12px; border-radius:8px; border:1px solid #475569; background:#0f172a; color:#f8fafc; box-sizing:border-box; font-size:15px; }}
  button {{ margin-top:16px; width:100%; padding:11px; border:0; border-radius:8px; background:#2563eb; color:#fff; font-size:15px; font-weight:600; cursor:pointer; }}
  .err {{ background:#7f1d1d; color:#fecaca; padding:10px 12px; border-radius:8px; font-size:13px; }}
</style>
</head>
<body>
  <div class="card">
    <h1>Подключить «{html.escape(auth_req['client_name'])}» к HiDL</h1>
    <p>Внешний клиент получит доступ к твоим данным HiDL (задачи, напоминания, покупки, холодильник, финансы).</p>
    <div class="badges">{mode_badge}{diary_badge}</div>
    {err_block}
    <form method="post" action="/oauth/authorize">
      <input type="hidden" name="response_type" value="code">
      {hidden_fields}
      <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">
      <label for="password">Пароль доступа MCP</label>
      <input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
      <button type="submit">Разрешить доступ</button>
    </form>
  </div>
</body>
</html>"""


async def handle_oauth_authorize_get(request: web.Request) -> web.Response:
    db: aiosqlite.Connection = request.app["db_conn"]
    settings: MCPSettings = request.app["mcp_settings"]
    csrf_key: bytes = request.app["mcp_csrf_key"]

    params = {k: str(v) for k, v in request.query.items()}
    auth_req, err = await _load_and_validate_authorize_params(params, db, settings)
    if err or not auth_req:
        return web.Response(text=f"OAuth error: {err}", status=400)

    if not settings.access_password:
        return web.Response(
            text="MCP_ACCESS_PASSWORD is not configured on server.",
            status=503,
        )

    csrf_token = _sign_csrf(
        csrf_key,
        client_id=auth_req["client_id"],
        redirect_uri=auth_req["redirect_uri"],
        code_challenge=auth_req["code_challenge"],
        issued_ts=str(int(_utc_now().timestamp())),
    )
    body = _render_consent_html(settings, auth_req, csrf_token)
    return web.Response(text=body, content_type="text/html", headers={"Cache-Control": "no-store"})


async def handle_oauth_authorize_post(request: web.Request) -> web.Response:
    db: aiosqlite.Connection = request.app["db_conn"]
    settings: MCPSettings = request.app["mcp_settings"]
    csrf_key: bytes = request.app["mcp_csrf_key"]

    post_data = await request.post()
    params = {k: str(v) for k, v in post_data.items()}
    auth_req, err = await _load_and_validate_authorize_params(params, db, settings)
    if err or not auth_req:
        return web.Response(text=f"OAuth error: {err}", status=400)

    csrf_token = params.get("csrf_token", "")
    if not _verify_csrf(
        csrf_key,
        csrf_token,
        client_id=auth_req["client_id"],
        redirect_uri=auth_req["redirect_uri"],
        code_challenge=auth_req["code_challenge"],
    ):
        fresh_csrf = _sign_csrf(
            csrf_key,
            client_id=auth_req["client_id"],
            redirect_uri=auth_req["redirect_uri"],
            code_challenge=auth_req["code_challenge"],
            issued_ts=str(int(_utc_now().timestamp())),
        )
        return web.Response(
            text=_render_consent_html(
                settings,
                auth_req,
                fresh_csrf,
                error_message="Сессия формы устарела, введи пароль ещё раз.",
            ),
            status=403,
            content_type="text/html",
        )

    if not settings.access_password:
        return web.Response(text="MCP_ACCESS_PASSWORD is not configured on server.", status=503)

    supplied_password = params.get("password", "")
    if not hmac.compare_digest(supplied_password, settings.access_password):
        fresh_csrf = _sign_csrf(
            csrf_key,
            client_id=auth_req["client_id"],
            redirect_uri=auth_req["redirect_uri"],
            code_challenge=auth_req["code_challenge"],
            issued_ts=str(int(_utc_now().timestamp())),
        )
        return web.Response(
            text=_render_consent_html(
                settings,
                auth_req,
                fresh_csrf,
                error_message="Неверный пароль доступа.",
            ),
            status=403,
            content_type="text/html",
        )

    raw_code = secrets.token_urlsafe(32)
    now_dt = _utc_now()
    expires_dt = now_dt + datetime.timedelta(minutes=5)
    await db.execute(
        """
        INSERT INTO mcp_oauth_codes
        (code_hash, client_id, redirect_uri, code_challenge, code_challenge_method, resource, scope, expires_at, used, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (
            _hash_secret(raw_code),
            auth_req["client_id"],
            auth_req["redirect_uri"],
            auth_req["code_challenge"],
            auth_req["code_challenge_method"],
            auth_req["resource"],
            auth_req["scope"],
            expires_dt.isoformat(),
            now_dt.isoformat(),
        ),
    )
    await db.commit()

    redir_params = {"code": raw_code}
    if auth_req["state"]:
        redir_params["state"] = auth_req["state"]
    location = _append_query_params(auth_req["redirect_uri"], redir_params)
    return web.HTTPFound(location=location, headers={"Cache-Control": "no-store"})


async def _parse_token_request(request: web.Request) -> dict[str, str]:
    ctype = (request.content_type or "").lower()
    if "application/json" in ctype:
        data = await request.json()
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    form = await request.post()
    return {str(k): str(v) for k, v in form.items()}


def _extract_basic_client_creds(request: web.Request) -> tuple[str, str]:
    auth = (request.headers.get("Authorization") or "").strip()
    if not auth.lower().startswith("basic "):
        return "", ""
    try:
        decoded = base64.b64decode(auth[6:].strip()).decode("utf-8")
    except Exception:
        return "", ""
    if ":" not in decoded:
        return "", ""
    cid, csec = decoded.split(":", 1)
    return unquote_plus(cid), unquote_plus(csec)


async def _issue_token_pair(
    db: aiosqlite.Connection,
    settings: MCPSettings,
    *,
    client_id: str,
    resource: str,
    scope: str,
) -> dict[str, Any]:
    now_dt = _utc_now()
    access_raw = f"hidl_at_{secrets.token_urlsafe(32)}"
    refresh_raw = f"hidl_rt_{secrets.token_urlsafe(32)}"
    access_exp = now_dt + datetime.timedelta(seconds=settings.access_token_ttl_sec)
    refresh_exp = now_dt + datetime.timedelta(seconds=settings.refresh_token_ttl_sec)

    for ttype, raw, exp in (
        ("access", access_raw, access_exp),
        ("refresh", refresh_raw, refresh_exp),
    ):
        await db.execute(
            """
            INSERT INTO mcp_oauth_tokens
            (token_hash, token_type, client_id, resource, scope, expires_at, revoked, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (_hash_secret(raw), ttype, client_id, resource, scope, exp.isoformat(), now_dt.isoformat()),
        )
    await db.commit()
    return {
        "access_token": access_raw,
        "token_type": "Bearer",
        "expires_in": settings.access_token_ttl_sec,
        "refresh_token": refresh_raw,
        "scope": scope,
    }


async def handle_oauth_token(request: web.Request) -> web.Response:
    db: aiosqlite.Connection = request.app["db_conn"]
    settings: MCPSettings = request.app["mcp_settings"]

    try:
        params = await _parse_token_request(request)
    except Exception:
        return _oauth_error_json(400, "invalid_request", "Malformed token request.")

    basic_cid, basic_sec = _extract_basic_client_creds(request)
    client_id = params.get("client_id", "").strip() or basic_cid
    client_secret = params.get("client_secret", "") or basic_sec
    grant_type = params.get("grant_type", "").strip()

    if not client_id:
        return _oauth_error_json(400, "invalid_client", "Missing client_id.")

    cur = await db.execute(
        "SELECT client_id, client_secret_hash FROM mcp_oauth_clients WHERE client_id = ?",
        (client_id,),
    )
    client_row = await cur.fetchone()
    if not client_row:
        return _oauth_error_json(401, "invalid_client", "Unknown client_id.")

    expected_sec_hash = client_row["client_secret_hash"] or ""
    if expected_sec_hash:
        if not client_secret or not hmac.compare_digest(_hash_secret(client_secret), expected_sec_hash):
            return _oauth_error_json(401, "invalid_client", "Invalid client_secret.")

    now_dt = _utc_now()
    if grant_type == "authorization_code":
        code = params.get("code", "").strip()
        redirect_uri = params.get("redirect_uri", "").strip()
        code_verifier = params.get("code_verifier", "").strip()
        if not code or not redirect_uri or not code_verifier:
            return _oauth_error_json(400, "invalid_request", "Missing code, redirect_uri, or code_verifier.")

        code_hash = _hash_secret(code)
        c_cur = await db.execute("SELECT * FROM mcp_oauth_codes WHERE code_hash = ?", (code_hash,))
        code_row = await c_cur.fetchone()
        if not code_row or int(code_row["used"] or 0) != 0:
            return _oauth_error_json(400, "invalid_grant", "Invalid or already used authorization code.")
        if code_row["client_id"] != client_id or code_row["redirect_uri"] != redirect_uri:
            return _oauth_error_json(400, "invalid_grant", "Authorization code mismatch.")

        try:
            exp = datetime.datetime.fromisoformat(code_row["expires_at"])
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            return _oauth_error_json(400, "invalid_grant", "Invalid authorization code expiry.")
        if exp <= now_dt:
            return _oauth_error_json(400, "invalid_grant", "Authorization code expired.")

        try:
            computed = _pkce_s256(code_verifier)
        except UnicodeEncodeError:
            return _oauth_error_json(400, "invalid_grant", "Invalid code_verifier encoding.")
        if not hmac.compare_digest(computed, code_row["code_challenge"]):
            return _oauth_error_json(400, "invalid_grant", "PKCE verification failed.")

        upd = await db.execute(
            "UPDATE mcp_oauth_codes SET used = 1 WHERE code_hash = ? AND used = 0",
            (code_hash,),
        )
        await db.commit()
        if int(upd.rowcount or 0) != 1:
            return _oauth_error_json(400, "invalid_grant", "Authorization code already consumed.")

        pair = await _issue_token_pair(
            db,
            settings,
            client_id=client_id,
            resource=code_row["resource"] or _resource_url(settings),
            scope=code_row["scope"] or " ".join(_supported_scopes(settings)),
        )
        return web.json_response(pair, status=200, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    if grant_type == "refresh_token":
        refresh_token = params.get("refresh_token", "").strip()
        if not refresh_token:
            return _oauth_error_json(400, "invalid_request", "Missing refresh_token.")

        rt_hash = _hash_secret(refresh_token)
        t_cur = await db.execute(
            "SELECT * FROM mcp_oauth_tokens WHERE token_hash = ? AND token_type = 'refresh'",
            (rt_hash,),
        )
        rt_row = await t_cur.fetchone()
        if not rt_row or int(rt_row["revoked"] or 0) != 0 or rt_row["client_id"] != client_id:
            return _oauth_error_json(400, "invalid_grant", "Invalid refresh_token.")
        try:
            exp = datetime.datetime.fromisoformat(rt_row["expires_at"])
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            return _oauth_error_json(400, "invalid_grant", "Invalid refresh_token expiry.")
        if exp <= now_dt:
            return _oauth_error_json(400, "invalid_grant", "refresh_token expired.")

        rev = await db.execute(
            "UPDATE mcp_oauth_tokens SET revoked = 1 WHERE token_hash = ? AND revoked = 0",
            (rt_hash,),
        )
        await db.commit()
        if int(rev.rowcount or 0) != 1:
            return _oauth_error_json(400, "invalid_grant", "refresh_token already rotated.")

        pair = await _issue_token_pair(
            db,
            settings,
            client_id=client_id,
            resource=rt_row["resource"] or _resource_url(settings),
            scope=rt_row["scope"] or " ".join(_supported_scopes(settings)),
        )
        return web.json_response(pair, status=200, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    return _oauth_error_json(400, "unsupported_grant_type", "Supported: authorization_code, refresh_token.")
