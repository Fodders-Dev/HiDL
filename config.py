import os
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

_DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "openai_compatible": "",
    "deepseek": "https://api.deepseek.com",
    "qwen": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
}

_GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


def _env_str(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or "").strip()


def _env_float(name: str, default: float) -> float:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name).lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _resolve_provider_defaults(
    provider: str,
    *,
    api_key_override: str = "",
    base_url_override: str = "",
    model_override: str = "",
    proxy_override: str = "",
) -> tuple[str, str, str, str]:
    p = (provider or "openai").strip().lower()
    if p == "openai":
        api_key = api_key_override or _env_str("OPENAI_API_KEY")
        base_url = base_url_override or _env_str("OPENAI_BASE_URL", _DEFAULT_BASE_URLS["openai"])
        model = model_override or _env_str("OPENAI_MODEL")
        proxy_url = proxy_override or _env_str("OPENAI_PROXY_URL")
    elif p == "deepseek":
        api_key = api_key_override or _env_str("DEEPSEEK_API_KEY")
        base_url = base_url_override or _env_str("DEEPSEEK_BASE_URL", _DEFAULT_BASE_URLS["deepseek"])
        model = model_override or _env_str("DEEPSEEK_MODEL") or ("deepseek-v4-flash" if api_key else "")
        proxy_url = proxy_override
    elif p == "qwen":
        api_key = api_key_override or _env_str("QWEN_API_KEY")
        base_url = base_url_override or _env_str("QWEN_BASE_URL", _DEFAULT_BASE_URLS["qwen"])
        model = model_override or _env_str("QWEN_MODEL")
        proxy_url = proxy_override
    else:
        api_key = api_key_override or _env_str("OPENAI_API_KEY")
        base_url = base_url_override or _env_str("OPENAI_BASE_URL")
        model = model_override or _env_str("OPENAI_MODEL")
        proxy_url = proxy_override
    return api_key, base_url.rstrip("/"), model, proxy_url


@dataclass
class Settings:
    """Application settings loaded from environment."""

    bot_token: str
    database_url: str
    default_timezone: str = "UTC"
    debug_log: bool = False


@dataclass(frozen=True)
class LLMRouteSettings:
    """Configuration for a single LLM chat-completions route."""

    provider: str
    base_url: str
    api_key: str
    model: str
    proxy_url: str = ""
    reasoning_effort: str = "none"

    @property
    def is_ready(self) -> bool:
        return bool(self.provider and self.base_url and self.api_key and self.model)


@dataclass(frozen=True)
class LLMSettings:
    """Runtime LLM settings read dynamically from environment."""

    enabled: bool
    primary: LLMRouteSettings
    failover: Optional[LLMRouteSettings] = None
    timeout_sec: float = 12.0
    max_tokens: int = 700


@dataclass(frozen=True)
class STTRouteSettings:
    """Configuration for a single speech-to-text route."""

    provider: str
    base_url: str
    api_key: str
    model: str
    proxy_url: str = ""

    @property
    def is_ready(self) -> bool:
        return bool(self.provider and self.base_url and self.api_key and self.model)


@dataclass(frozen=True)
class STTSettings:
    """Runtime STT settings read dynamically from environment."""

    provider: str
    language: str
    timeout_sec: float
    max_duration_sec: int
    routes: list[STTRouteSettings] = field(default_factory=list)


@dataclass(frozen=True)
class MCPSettings:
    """Runtime configuration for HiDL remote MCP server."""

    host: str
    port: int
    public_base_url: str
    owner_telegram_id: int
    access_password: str
    bearer_token: str
    allow_diary: bool
    readonly: bool
    access_token_ttl_sec: int
    refresh_token_ttl_sec: int


@dataclass(frozen=True)
class GoogleCalendarSettings:
    """Runtime configuration for Google Calendar OAuth 2.0 integration."""

    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: str

    @property
    def oauth_configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)


def get_settings(require_bot_token: bool = True) -> Settings:
    """Return settings from environment variables."""
    bot_token = os.getenv("BOT_TOKEN", "")
    if require_bot_token and not bot_token:
        raise ValueError("BOT_TOKEN is required. Put it in .env or environment.")

    database_url = os.getenv("DATABASE_URL", "sqlite:///hidl.db")
    debug_log = os.getenv("DEBUG_LOG", "0").lower() in {"1", "true", "yes"}
    return Settings(bot_token=bot_token, database_url=database_url, debug_log=debug_log)


def get_llm_settings() -> LLMSettings:
    """Build LLMSettings from current environment variables without inventing model IDs."""
    provider = _env_str("LLM_PROVIDER", "openai").lower() or "openai"
    api_key, base_url, model, proxy_url = _resolve_provider_defaults(
        provider,
        api_key_override=_env_str("LLM_API_KEY"),
        base_url_override=_env_str("LLM_BASE_URL"),
        model_override=_env_str("LLM_MODEL"),
        proxy_override=_env_str("LLM_PROXY_URL"),
    )
    reasoning_effort = _env_str("LLM_REASONING_EFFORT", "none").lower() or "none"
    primary = LLMRouteSettings(
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        proxy_url=proxy_url,
        reasoning_effort=reasoning_effort,
    )

    raw_failover = _env_str("LLM_FAILOVER_PROVIDER").lower()
    if raw_failover in {"none", "off", "false", "0"}:
        failover_provider = ""
    elif raw_failover:
        failover_provider = raw_failover
    elif primary.provider != "deepseek" and _env_str("DEEPSEEK_API_KEY"):
        failover_provider = "deepseek"
    else:
        failover_provider = ""
    failover: Optional[LLMRouteSettings] = None
    if failover_provider:
        f_key, f_url, f_model, f_proxy = _resolve_provider_defaults(
            failover_provider,
            api_key_override=_env_str("LLM_FAILOVER_API_KEY"),
            base_url_override=_env_str("LLM_FAILOVER_BASE_URL"),
            model_override=_env_str("LLM_FAILOVER_MODEL"),
            proxy_override=_env_str("LLM_FAILOVER_PROXY_URL"),
        )
        candidate = LLMRouteSettings(
            provider=failover_provider,
            base_url=f_url,
            api_key=f_key,
            model=f_model,
            proxy_url=f_proxy,
            reasoning_effort=reasoning_effort,
        )
        if candidate.is_ready:
            failover = candidate

    enabled_raw = _env_str("LLM_ENABLED").lower()
    if enabled_raw in {"0", "false", "no", "off"}:
        enabled = False
    elif enabled_raw in {"1", "true", "yes", "on"}:
        enabled = primary.is_ready or (failover is not None and failover.is_ready)
    else:
        enabled = primary.is_ready or (failover is not None and failover.is_ready)

    timeout_sec = max(1.0, _env_float("LLM_TIMEOUT_SEC", 15.0))
    max_tokens = max(64, _env_int("LLM_MAX_TOKENS", 1200))
    return LLMSettings(
        enabled=enabled,
        primary=primary,
        failover=failover,
        timeout_sec=timeout_sec,
        max_tokens=max_tokens,
    )


def get_stt_settings() -> STTSettings:
    """Build STTSettings with ordered provider candidates from environment."""
    preferred = _env_str("STT_PROVIDER", "auto").lower() or "auto"
    language = _env_str("STT_LANGUAGE", "ru")
    timeout_sec = max(2.0, _env_float("STT_TIMEOUT_SEC", 30.0))
    max_duration_sec = max(10, _env_int("STT_MAX_DURATION_SEC", 180))
    if preferred in {"off", "none", "disabled", "false", "0"}:
        return STTSettings(
            provider=preferred,
            language=language,
            timeout_sec=timeout_sec,
            max_duration_sec=max_duration_sec,
            routes=[],
        )

    custom_key = _env_str("STT_API_KEY")
    custom_url = _env_str("STT_BASE_URL").rstrip("/")
    custom_model = _env_str("STT_MODEL")
    custom_proxy = _env_str("STT_PROXY_URL")
    custom_route = STTRouteSettings(
        provider="custom",
        base_url=custom_url,
        api_key=custom_key,
        model=custom_model or "whisper-1",
        proxy_url=custom_proxy,
    )

    groq_key = _env_str("GROQ_API_KEY")
    groq_model = _env_str("STT_GROQ_MODEL") or (custom_model if preferred == "groq" else "") or "whisper-large-v3-turbo"
    groq_route = STTRouteSettings(
        provider="groq",
        base_url=_GROQ_STT_URL,
        api_key=groq_key,
        model=groq_model,
        proxy_url=_env_str("STT_PROXY_URL"),
    )

    openai_key = _env_str("OPENAI_API_KEY")
    openai_base = _env_str("OPENAI_BASE_URL", _DEFAULT_BASE_URLS["openai"]).rstrip("/")
    openai_stt_url = f"{openai_base}/audio/transcriptions" if openai_base else ""
    openai_model = _env_str("STT_OPENAI_MODEL") or (custom_model if preferred == "openai" else "") or "whisper-1"
    openai_route = STTRouteSettings(
        provider="openai",
        base_url=openai_stt_url,
        api_key=openai_key,
        model=openai_model,
        proxy_url=_env_str("STT_PROXY_URL") or _env_str("OPENAI_PROXY_URL"),
    )

    qwen_key = _env_str("QWEN_API_KEY")
    qwen_base = _env_str("QWEN_BASE_URL", _DEFAULT_BASE_URLS["qwen"]).rstrip("/")
    qwen_model = _env_str("STT_QWEN_MODEL", "qwen3-asr-flash")
    qwen_route = STTRouteSettings(
        provider="qwen",
        base_url=qwen_base,
        api_key=qwen_key,
        model=qwen_model,
        proxy_url=_env_str("STT_PROXY_URL"),
    )

    by_name = {
        "custom": custom_route,
        "groq": groq_route,
        "openai": openai_route,
        "qwen": qwen_route,
    }

    if preferred == "auto":
        order = ["custom", "groq", "openai", "qwen"]
    elif preferred in by_name:
        order = [preferred] + [name for name in ("custom", "groq", "openai", "qwen") if name != preferred]
    else:
        order = ["custom", "groq", "openai", "qwen"]

    routes = [by_name[name] for name in order if by_name[name].is_ready]
    return STTSettings(
        provider=preferred,
        language=language,
        timeout_sec=timeout_sec,
        max_duration_sec=max_duration_sec,
        routes=routes,
    )


def get_mcp_settings() -> MCPSettings:
    """Build MCPSettings for Streamable HTTP server and OAuth 2.1 flow."""
    host = _env_str("MCP_HOST", "127.0.0.1") or "127.0.0.1"
    port = max(1, min(65535, _env_int("MCP_PORT", 8765)))
    raw_public = _env_str("MCP_PUBLIC_BASE_URL", f"http://{host}:{port}").rstrip("/")
    public_base_url = raw_public or f"http://{host}:{port}"
    return MCPSettings(
        host=host,
        port=port,
        public_base_url=public_base_url,
        owner_telegram_id=max(0, _env_int("MCP_OWNER_TELEGRAM_ID", 0)),
        access_password=_env_str("MCP_ACCESS_PASSWORD"),
        bearer_token=_env_str("MCP_BEARER_TOKEN"),
        allow_diary=_env_bool("MCP_ALLOW_DIARY", True),
        readonly=_env_bool("MCP_READONLY", False),
        access_token_ttl_sec=max(300, _env_int("MCP_ACCESS_TOKEN_TTL_SEC", 86400)),
        refresh_token_ttl_sec=max(3600, _env_int("MCP_REFRESH_TOKEN_TTL_SEC", 7776000)),
    )


_DEFAULT_GCAL_SCOPES = (
    "https://www.googleapis.com/auth/calendar.events "
    "https://www.googleapis.com/auth/calendar.readonly"
)


def get_gcal_settings() -> GoogleCalendarSettings:
    """Build GoogleCalendarSettings from environment variables."""
    client_id = _env_str("GCAL_CLIENT_ID")
    client_secret = _env_str("GCAL_CLIENT_SECRET")
    raw_redirect = _env_str("GCAL_REDIRECT_URI")
    if not raw_redirect:
        raw_base = _env_str("MCP_PUBLIC_BASE_URL").rstrip("/")
        raw_redirect = f"{raw_base}/gcal/callback" if raw_base else ""
    scopes = _env_str("GCAL_SCOPES") or _DEFAULT_GCAL_SCOPES
    return GoogleCalendarSettings(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=raw_redirect,
        scopes=scopes,
    )
