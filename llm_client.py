"""Конфигурируемый асинхронный LLM-клиент для HiDL.

Поддерживает OpenAI, OpenAI-compatible, DeepSeek и Qwen через единый
OpenAI-совместимый `/chat/completions`, прокси, ретраи на временных сбоях
и опциональный failover-маршрут.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp

from config import LLMRouteSettings, LLMSettings, get_llm_settings
from utils.logger import log_error, log_info

ATTEMPTS = 3
RETRY_BACKOFF_SEC = (0.3, 0.8)
_JSON_OPEN_TO_CLOSE = {"{": "}", "[": "]"}


@dataclass(frozen=True)
class LLMRoute:
    provider: str
    base_url: str
    api_key: str
    model: str
    proxy_url: str = ""
    reasoning_effort: str = "none"

    @classmethod
    def from_settings(cls, route: LLMRouteSettings) -> "LLMRoute":
        return cls(
            provider=route.provider,
            base_url=route.base_url.rstrip("/"),
            api_key=route.api_key,
            model=route.model,
            proxy_url=route.proxy_url,
            reasoning_effort=route.reasoning_effort,
        )

    @property
    def is_ready(self) -> bool:
        return bool(self.provider and self.base_url and self.api_key and self.model)


@dataclass
class LLMResult:
    ok: bool
    text: str = ""
    data: Optional[dict[str, Any]] = None
    provider: str = ""
    model: str = ""
    error: Optional[str] = None
    usage: Optional[dict[str, Any]] = None


def strip_json_fence(text: str) -> str:
    raw = str(text or "").strip()
    raw = re.sub(r"^\s*(?:```|~~~)[a-zA-Z0-9_-]*\s*", "", raw, count=1)
    raw = re.sub(r"\s*(?:```|~~~)\s*$", "", raw, count=1).strip()
    return raw


def _balanced_json_end(raw: str, start: int) -> Optional[int]:
    stack: list[str] = []
    in_string = False
    escaped = False
    for idx in range(start, len(raw)):
        char = raw[idx]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char in _JSON_OPEN_TO_CLOSE:
            stack.append(_JSON_OPEN_TO_CLOSE[char])
            continue
        if char in ("}", "]"):
            if not stack or char != stack[-1]:
                return None
            stack.pop()
            if not stack:
                return idx
    return None


def _repair_truncated_json(raw: str, start: int) -> Optional[str]:
    stack: list[str] = []
    in_string = False
    escaped = False
    for idx in range(start, len(raw)):
        char = raw[idx]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char in _JSON_OPEN_TO_CLOSE:
            stack.append(_JSON_OPEN_TO_CLOSE[char])
            continue
        if char in ("}", "]"):
            if not stack or char != stack[-1]:
                return None
            stack.pop()
    if in_string or not stack:
        return None
    candidate = raw[start:].rstrip()
    candidate = re.sub(r",\s*$", "", candidate)
    candidate += "".join(reversed(stack))
    candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
    return candidate


def extract_json_object(text: str) -> Optional[dict[str, Any]]:
    """Extract and parse the first valid JSON object from model output."""
    raw = strip_json_fence(text)
    for start, char in enumerate(raw):
        if char != "{":
            continue
        candidates: list[str] = []
        end = _balanced_json_end(raw, start)
        if end is not None:
            candidates.append(raw[start : end + 1])
        else:
            repaired = _repair_truncated_json(raw, start)
            if repaired:
                candidates.append(repaired)
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except Exception:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def _retryable_status(status: int) -> bool:
    return status == 429 or 500 <= int(status) < 600


def _build_request_body(
    route: LLMRoute,
    *,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int,
    response_format: Optional[dict[str, Any]] = None,
    image_data_urls: Optional[list[str]] = None,
) -> dict[str, Any]:
    urls = [str(u).strip() for u in (image_data_urls or []) if str(u or "").strip()]
    user_content: Any = (
        [{"type": "text", "text": user_prompt}]
        + [{"type": "image_url", "image_url": {"url": url}} for url in urls]
        if urls
        else user_prompt
    )
    body: dict[str, Any] = {
        "model": route.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": max(0.0, min(1.5, float(temperature))),
        "max_tokens": max(64, int(max_tokens)),
        "stream": False,
    }
    if response_format is not None:
        body["response_format"] = response_format

    provider = (route.provider or "").lower()
    if provider == "openai":
        body["max_completion_tokens"] = body.pop("max_tokens")
        effort = (route.reasoning_effort or "none").lower()
        body["reasoning_effort"] = effort
        if effort != "none":
            body.pop("temperature", None)
        body["store"] = False
    elif provider == "deepseek":
        body["thinking"] = {"type": "disabled"}
    elif provider == "qwen":
        body["enable_thinking"] = False
    return body


async def _attempt_completion(
    route: LLMRoute,
    *,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int,
    timeout_sec: float,
    response_format: Optional[dict[str, Any]] = None,
    image_data_urls: Optional[list[str]] = None,
    attempts: int = ATTEMPTS,
) -> LLMResult:
    url = f"{route.base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {route.api_key}",
        "Content-Type": "application/json",
    }
    body = _build_request_body(
        route,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format=response_format,
        image_data_urls=image_data_urls,
    )
    timeout = aiohttp.ClientTimeout(total=max(1.0, float(timeout_sec)))
    request_options: dict[str, Any] = {"proxy": route.proxy_url} if route.proxy_url else {}
    last_error = "unknown_error"
    max_attempts = max(1, int(attempts))

    for attempt in range(max_attempts):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=body, **request_options) as resp:
                    status = resp.status
                    try:
                        payload = await resp.json(content_type=None)
                    except Exception:
                        payload = None
                        if status == 200:
                            return LLMResult(
                                ok=False,
                                provider=route.provider,
                                model=route.model,
                                error="bad_response",
                            )
                    if status == 200:
                        return _parse_success_payload(route, payload)
                    last_error = f"http_{status}"
                    if not _retryable_status(status):
                        return LLMResult(
                            ok=False,
                            provider=route.provider,
                            model=route.model,
                            error=last_error,
                        )
        except asyncio.TimeoutError:
            return LLMResult(
                ok=False,
                provider=route.provider,
                model=route.model,
                error="timeout",
            )
        except aiohttp.ClientError as exc:
            last_error = exc.__class__.__name__
        except Exception as exc:  # noqa: BLE001
            return LLMResult(
                ok=False,
                provider=route.provider,
                model=route.model,
                error=exc.__class__.__name__,
            )

        if attempt + 1 >= max_attempts:
            return LLMResult(
                ok=False,
                provider=route.provider,
                model=route.model,
                error=last_error,
            )
        backoff = RETRY_BACKOFF_SEC[min(attempt, len(RETRY_BACKOFF_SEC) - 1)]
        await asyncio.sleep(backoff)

    return LLMResult(ok=False, provider=route.provider, model=route.model, error=last_error)


def _parse_success_payload(route: LLMRoute, payload: Any) -> LLMResult:
    try:
        choice = payload["choices"][0]
        content = choice["message"]["content"]
        if isinstance(content, list):
            content = "".join(
                str(part.get("text") or "") for part in content if isinstance(part, dict)
            )
        usage = payload.get("usage") if isinstance(payload, dict) else None
        usage = usage if isinstance(usage, dict) else None
        if not isinstance(content, str) or not content.strip():
            return LLMResult(
                ok=False,
                provider=route.provider,
                model=route.model,
                error="empty_content",
                usage=usage,
            )
        return LLMResult(
            ok=True,
            text=content.strip(),
            provider=route.provider,
            model=route.model,
            usage=usage,
        )
    except Exception:
        return LLMResult(
            ok=False,
            provider=route.provider,
            model=route.model,
            error="bad_response",
        )


class LLMClient:
    """Async chat-completions client with env-driven routing and failover."""

    def __init__(
        self,
        model: Optional[str] = None,
        *,
        route: Optional[LLMRoute] = None,
        failover_route: Optional[LLMRoute] = None,
        timeout_sec: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ):
        self._explicit_model = model
        self._explicit_route = route
        self._explicit_failover = failover_route
        self._timeout_override = timeout_sec
        self._max_tokens_override = max_tokens

    def _resolve_routes(self) -> tuple[bool, list[LLMRoute], float, int]:
        if self._explicit_route is not None:
            routes: list[LLMRoute] = []
            if self._explicit_route.is_ready:
                routes.append(self._explicit_route)
            if self._explicit_failover is not None and self._explicit_failover.is_ready:
                routes.append(self._explicit_failover)
            timeout_sec = self._timeout_override or 15.0
            max_tokens = self._max_tokens_override or 1200
            return bool(routes), routes, timeout_sec, max_tokens

        settings: LLMSettings = get_llm_settings()
        routes = []
        primary = LLMRoute.from_settings(settings.primary)
        if self._explicit_model and not primary.model:
            primary = LLMRoute(
                provider=primary.provider,
                base_url=primary.base_url,
                api_key=primary.api_key,
                model=self._explicit_model,
                proxy_url=primary.proxy_url,
                reasoning_effort=primary.reasoning_effort,
            )
        if primary.is_ready:
            routes.append(primary)
        if settings.failover is not None:
            failover = LLMRoute.from_settings(settings.failover)
            if failover.is_ready:
                routes.append(failover)

        timeout_sec = self._timeout_override or settings.timeout_sec
        max_tokens = self._max_tokens_override or settings.max_tokens
        return bool(settings.enabled and routes), routes, timeout_sec, max_tokens

    @property
    def model(self) -> str:
        _, routes, _, _ = self._resolve_routes()
        if routes:
            return routes[0].model
        return self._explicit_model or ""

    def is_configured(self) -> bool:
        enabled, routes, _, _ = self._resolve_routes()
        return enabled and bool(routes)

    async def complete_text(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        response_format: Optional[dict[str, Any]] = None,
        image_data_urls: Optional[list[str]] = None,
    ) -> LLMResult:
        enabled, routes, timeout_sec, default_max_tokens = self._resolve_routes()
        if not enabled or not routes:
            return LLMResult(ok=False, error="llm_not_configured")

        effective_tokens = max_tokens or default_max_tokens
        last_result = LLMResult(ok=False, error="llm_not_configured")

        for idx, route in enumerate(routes):
            res = await _attempt_completion(
                route,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=temperature,
                max_tokens=effective_tokens,
                timeout_sec=timeout_sec,
                response_format=response_format,
                image_data_urls=image_data_urls,
            )
            if res.ok:
                if idx > 0:
                    log_info(f"[llm] served by failover provider={route.provider} model={route.model}")
                return res
            last_result = res
            log_error(
                f"[llm] attempt failed provider={route.provider} model={route.model} err={res.error}"
            )
        return last_result

    async def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.1,
        max_tokens: Optional[int] = None,
        image_data_urls: Optional[list[str]] = None,
    ) -> LLMResult:
        res = await self.complete_text(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            image_data_urls=image_data_urls,
        )
        if not res.ok:
            return res
        parsed = extract_json_object(res.text)
        if parsed is None:
            return LLMResult(
                ok=False,
                text=res.text,
                provider=res.provider,
                model=res.model,
                error="invalid_json",
                usage=res.usage,
            )
        return LLMResult(
            ok=True,
            text=res.text,
            data=parsed,
            provider=res.provider,
            model=res.model,
            usage=res.usage,
        )

    async def search_web_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_tokens: Optional[int] = None,
        timeout_sec: Optional[float] = None,
    ) -> LLMResult:
        """Выполнить запрос с веб-поиском через OpenAI Responses API (/responses) и извлечь JSON."""
        enabled, routes, default_timeout, default_tokens = self._resolve_routes()
        if not enabled or not routes:
            return LLMResult(ok=False, error="llm_not_configured")

        openai_route = next(
            (r for r in routes if (r.provider or "").lower() == "openai" and r.is_ready),
            None,
        )
        if openai_route is None:
            return LLMResult(ok=False, error="web_search_requires_openai_route")

        url = f"{openai_route.base_url.rstrip('/')}/responses"
        headers = {
            "Authorization": f"Bearer {openai_route.api_key}",
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {
            "model": openai_route.model,
            "tools": [{"type": "web_search"}],
            "reasoning": {"effort": "low"},
            "instructions": system_prompt,
            "input": user_prompt,
            "max_output_tokens": max(1200, int(max_tokens or default_tokens or 1400)),
            "store": False,
        }
        timeout = aiohttp.ClientTimeout(total=max(5.0, float(timeout_sec or max(22.0, default_timeout))))
        request_options: dict[str, Any] = (
            {"proxy": openai_route.proxy_url} if openai_route.proxy_url else {}
        )
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                status = 0
                payload: Any = None
                for attempt in range(2):
                    async with session.post(url, headers=headers, json=body, **request_options) as resp:
                        status = resp.status
                        try:
                            payload = await resp.json(content_type=None)
                        except Exception:
                            payload = None
                    if (
                        attempt == 0
                        and status == 400
                        and "reasoning" in body
                        and "reasoning" in str(payload or "").lower()
                    ):
                        body.pop("reasoning", None)
                        continue
                    break
                if status != 200 or not isinstance(payload, dict):
                    return LLMResult(
                        ok=False,
                        provider=openai_route.provider,
                        model=openai_route.model,
                        error=f"http_{status}",
                    )
        except asyncio.TimeoutError:
            return LLMResult(
                ok=False,
                provider=openai_route.provider,
                model=openai_route.model,
                error="timeout",
            )
        except Exception as exc:  # noqa: BLE001
            return LLMResult(
                ok=False,
                provider=openai_route.provider,
                model=openai_route.model,
                error=exc.__class__.__name__,
            )

        out_txt = str(payload.get("output_text") or "").strip()
        need_text = not out_txt
        text_parts: list[str] = []
        citations: list[str] = []
        for item in payload.get("output") or []:
            if not isinstance(item, dict):
                continue
            for part in item.get("content") or []:
                if not isinstance(part, dict):
                    continue
                if need_text and part.get("text"):
                    text_parts.append(str(part.get("text") or ""))
                for ann in part.get("annotations") or []:
                    if isinstance(ann, dict) and ann.get("type") == "url_citation":
                        u = str(ann.get("url") or "").strip()
                        if u and u not in citations and len(citations) < 3:
                            citations.append(u)
        if need_text:
            out_txt = "".join(text_parts).strip()
        parsed = extract_json_object(out_txt)
        if parsed is None:
            return LLMResult(
                ok=False,
                text=out_txt,
                provider=openai_route.provider,
                model=openai_route.model,
                error="invalid_json",
            )
        if citations and "_citations" not in parsed:
            parsed["_citations"] = citations
        return LLMResult(
            ok=True,
            text=out_txt,
            data=parsed,
            provider=openai_route.provider,
            model=openai_route.model,
        )

    async def ask(self, prompt: str, user_context: Optional[str] = None) -> str:
        """Backward-compatible helper for short conversational replies."""
        if not self.is_configured():
            return "Модель пока не настроена — работаю по базовым правилам без потери записей."
        system_prompt = (
            "Ты — HiDL, заботливая личная помощница. Отвечай коротко (2-4 предложения), "
            "по-русски, в женском роде, без давления и лишней воды."
        )
        user_msg = prompt if not user_context else f"{prompt}\n\nКонтекст: {user_context}"
        res = await self.complete_text(system_prompt, user_msg, temperature=0.3, max_tokens=280)
        if res.ok and res.text:
            return res.text
        return "Сейчас не удалось связаться с моделью, но я рядом — попробуй чуть позже или через кнопки меню."


client = LLMClient()
