"""Распознавание голосовых сообщений (STT) с настраиваемыми провайдерами."""

from __future__ import annotations

import asyncio
import base64
import io
from dataclasses import dataclass
from typing import Optional

import aiohttp

from config import STTRouteSettings, get_stt_settings
from utils.logger import log_error, log_info


@dataclass
class STTResult:
    ok: bool
    text: str = ""
    provider: str = ""
    model: str = ""
    error: Optional[str] = None


def available_stt_providers() -> list[str]:
    return [route.provider for route in get_stt_settings().routes]


def stt_enabled() -> bool:
    return bool(get_stt_settings().routes)


def max_voice_duration_sec() -> int:
    return get_stt_settings().max_duration_sec


def _resolve_transcriptions_url(base_url: str) -> str:
    clean = base_url.rstrip("/")
    if clean.endswith("/audio/transcriptions"):
        return clean
    return f"{clean}/audio/transcriptions"


async def download_telegram_voice(bot, voice) -> tuple[Optional[bytes], Optional[str]]:
    """Download Telegram voice file into memory."""
    file_id = getattr(voice, "file_id", None)
    if not file_id:
        return None, "missing_file_id"
    try:
        file_info = await bot.get_file(file_id)
        file_path = getattr(file_info, "file_path", None)
        if not file_path:
            return None, "missing_file_path"
        buf = io.BytesIO()
        await bot.download_file(file_path, destination=buf)
        raw = buf.getvalue()
        if not raw:
            return None, "empty_audio"
        return raw, None
    except Exception as exc:  # noqa: BLE001
        log_error(f"[stt] telegram download failed: {exc.__class__.__name__}")
        return None, "download_failed"


async def _transcribe_openai_compatible(
    route: STTRouteSettings,
    audio_bytes: bytes,
    *,
    filename: str,
    language: str,
    timeout_sec: float,
) -> STTResult:
    url = _resolve_transcriptions_url(route.base_url)
    form = aiohttp.FormData()
    form.add_field("file", audio_bytes, filename=filename, content_type="application/octet-stream")
    form.add_field("model", route.model)
    form.add_field("temperature", "0")
    form.add_field("response_format", "json")
    if language:
        form.add_field("language", language)

    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    request_options = {"proxy": route.proxy_url} if route.proxy_url else {}
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url,
                headers={"Authorization": f"Bearer {route.api_key}"},
                data=form,
                **request_options,
            ) as resp:
                if resp.status != 200:
                    return STTResult(
                        ok=False,
                        provider=route.provider,
                        model=route.model,
                        error=f"http_{resp.status}",
                    )
                payload = await resp.json(content_type=None)
    except asyncio.TimeoutError:
        return STTResult(ok=False, provider=route.provider, model=route.model, error="timeout")
    except aiohttp.ClientError as exc:
        return STTResult(
            ok=False,
            provider=route.provider,
            model=route.model,
            error=exc.__class__.__name__,
        )
    except Exception as exc:  # noqa: BLE001
        return STTResult(
            ok=False,
            provider=route.provider,
            model=route.model,
            error=exc.__class__.__name__,
        )

    text = str((payload or {}).get("text") or "").strip() if isinstance(payload, dict) else ""
    if not text:
        return STTResult(ok=False, provider=route.provider, model=route.model, error="empty_transcript")
    return STTResult(ok=True, text=text, provider=route.provider, model=route.model)


async def _transcribe_qwen(
    route: STTRouteSettings,
    audio_bytes: bytes,
    *,
    filename: str,
    mime_type: str,
    timeout_sec: float,
) -> STTResult:
    effective_mime = mime_type or ("audio/mp4" if filename.endswith(".mp4") else "audio/ogg")
    b64 = base64.b64encode(audio_bytes).decode("ascii")
    body = {
        "model": route.model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {"data": f"data:{effective_mime};base64,{b64}"},
                    }
                ],
            }
        ],
        "stream": False,
    }
    url = f"{route.base_url.rstrip('/')}/chat/completions"
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    request_options = {"proxy": route.proxy_url} if route.proxy_url else {}
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url,
                headers={
                    "Authorization": f"Bearer {route.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                **request_options,
            ) as resp:
                if resp.status != 200:
                    return STTResult(
                        ok=False,
                        provider=route.provider,
                        model=route.model,
                        error=f"http_{resp.status}",
                    )
                payload = await resp.json(content_type=None)
    except asyncio.TimeoutError:
        return STTResult(ok=False, provider=route.provider, model=route.model, error="timeout")
    except aiohttp.ClientError as exc:
        return STTResult(
            ok=False,
            provider=route.provider,
            model=route.model,
            error=exc.__class__.__name__,
        )
    except Exception as exc:  # noqa: BLE001
        return STTResult(
            ok=False,
            provider=route.provider,
            model=route.model,
            error=exc.__class__.__name__,
        )

    try:
        content = (payload or {})["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = " ".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
        text = str(content or "").strip()
    except (KeyError, IndexError, TypeError):
        text = ""
    if not text:
        return STTResult(ok=False, provider=route.provider, model=route.model, error="empty_transcript")
    return STTResult(ok=True, text=text, provider=route.provider, model=route.model)


async def transcribe_voice(
    audio_bytes: bytes,
    *,
    filename: str = "voice.ogg",
    mime_type: str = "audio/ogg",
) -> STTResult:
    """Transcribe audio bytes via configured STT providers with runtime fallback."""
    if not audio_bytes:
        return STTResult(ok=False, error="empty_audio")

    settings = get_stt_settings()
    if not settings.routes:
        return STTResult(ok=False, error="stt_not_configured")

    last_res = STTResult(ok=False, error="stt_not_configured")
    for idx, route in enumerate(settings.routes):
        if route.provider == "qwen":
            res = await _transcribe_qwen(
                route,
                audio_bytes,
                filename=filename,
                mime_type=mime_type,
                timeout_sec=settings.timeout_sec,
            )
        else:
            res = await _transcribe_openai_compatible(
                route,
                audio_bytes,
                filename=filename,
                language=settings.language,
                timeout_sec=settings.timeout_sec,
            )
        if res.ok and res.text:
            if idx > 0:
                log_info(f"[stt] served by fallback provider={route.provider}")
            return res
        last_res = res
        log_error(f"[stt] provider={route.provider} failed err={res.error}")
    return last_res
