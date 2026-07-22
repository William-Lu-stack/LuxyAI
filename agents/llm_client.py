"""
LLM client for the internal vLLM gateway.

The gateway requires OAuth2 Client Credentials first, then an OpenAI-compatible
chat completion request with ``Authorization: Bearer <access_token>``.  This
module keeps that flow explicit instead of relying on LangChain internals to
inject the token.
"""
import asyncio
import json
import os
import threading
import time
from typing import Any, AsyncIterator

import httpx
from dotenv import load_dotenv
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import OpenAIEmbeddings

from agents.model_registry import ModelProfile, model_profile_from_payload, select_model_profile

load_dotenv()

TOKEN_URL = os.getenv("OAUTH_TOKEN_URL", "")
CLIENT_ID = os.getenv("OAUTH_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("OAUTH_CLIENT_SECRET", "")

LLM_GATEWAY_BASE = os.getenv("LLM_GATEWAY_BASE", "")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:7b")
LLM_API_BASE = os.getenv(
    "LLM_API_BASE",
    f"{LLM_GATEWAY_BASE}/v1" if LLM_GATEWAY_BASE else "http://localhost:11434/v1",
)

EMBEDDING_GATEWAY_BASE = os.getenv("EMBEDDING_GATEWAY_BASE", "")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")
EMBEDDING_API_BASE = os.getenv(
    "EMBEDDING_API_BASE",
    EMBEDDING_GATEWAY_BASE if EMBEDDING_GATEWAY_BASE else LLM_API_BASE,
)
LLM_VERIFY_SSL = os.getenv("LLM_VERIFY_SSL", "true").lower() in {"1", "true", "yes", "on"}
OAUTH_VERIFY_SSL = os.getenv("OAUTH_VERIFY_SSL", str(LLM_VERIFY_SSL).lower()).lower() in {"1", "true", "yes", "on"}
_HTTP_LIMITS = httpx.Limits(
    max_connections=int(os.getenv("LLM_HTTP_MAX_CONNECTIONS", "64")),
    max_keepalive_connections=int(os.getenv("LLM_HTTP_KEEPALIVE_CONNECTIONS", "16")),
    keepalive_expiry=float(os.getenv("LLM_HTTP_KEEPALIVE_SECONDS", "30")),
)
_oauth_client = httpx.Client(
    timeout=httpx.Timeout(15.0, connect=5.0, pool=2.0),
    verify=OAUTH_VERIFY_SSL,
    limits=_HTTP_LIMITS,
)
_gateway_client = httpx.Client(
    timeout=httpx.Timeout(
        float(os.getenv("LLM_READ_TIMEOUT_SECONDS", "45")),
        connect=float(os.getenv("LLM_CONNECT_TIMEOUT_SECONDS", "5")),
        pool=2.0,
    ),
    verify=LLM_VERIFY_SSL,
    limits=_HTTP_LIMITS,
)
_embedding_client = httpx.Client(
    timeout=httpx.Timeout(30.0, connect=5.0, pool=2.0),
    verify=LLM_VERIFY_SSL,
    limits=_HTTP_LIMITS,
)
_embedding_async_client = httpx.AsyncClient(
    timeout=httpx.Timeout(30.0, connect=5.0, pool=2.0),
    verify=LLM_VERIFY_SSL,
    limits=_HTTP_LIMITS,
)


class TokenCache:
    """Thread-safe OAuth2 token cache."""

    def __init__(self):
        self._token = ""
        self._expires_at = 0.0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(TOKEN_URL)

    def get(self) -> str:
        if not self.enabled:
            return os.getenv("LLM_API_KEY", "")

        if not CLIENT_ID or not CLIENT_SECRET:
            raise RuntimeError("OAUTH_CLIENT_ID/OAUTH_CLIENT_SECRET are required")

        now = time.time()
        if self._token and now < self._expires_at - 60:
            return self._token

        with self._lock:
            now = time.time()
            if self._token and now < self._expires_at - 60:
                return self._token

            resp = _oauth_client.post(
                TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = data["access_token"]
            self._expires_at = now + int(data.get("expires_in", 300))
            return self._token


_token_cache = TokenCache()
_profile_token_cache: dict[str, tuple[str, float]] = {}
_profile_token_lock = threading.Lock()


def _message_to_openai(message: BaseMessage) -> dict[str, Any]:
    role = getattr(message, "type", "user")
    if role == "human":
        role = "user"
    elif role == "ai":
        role = "assistant"
    elif role == "system":
        role = "system"
    else:
        role = getattr(message, "role", "user")
    return {"role": role, "content": message.content}


def _usage_dict(usage: Any) -> dict[str, int]:
    if usage is None:
        return {}
    if hasattr(usage, "model_dump"):
        data = usage.model_dump()
    elif isinstance(usage, dict):
        data = usage
    else:
        data = {}
    return {
        "input_tokens": int(data.get("prompt_tokens", data.get("input_tokens", 0)) or 0),
        "output_tokens": int(data.get("completion_tokens", data.get("output_tokens", 0)) or 0),
        "total_tokens": int(data.get("total_tokens", 0) or 0),
    }


class GatewayChatModel(BaseChatModel):
    """Minimal LangChain chat model backed by a compatible gateway."""

    model_name: str
    base_url: str
    profile_id: str = ""
    auth_type: str = "api_key"
    api_key: str = ""
    headers: dict[str, str] = {}
    verify_ssl: bool = True
    temperature: float = 0.1
    max_tokens: int = 4096

    @property
    def _llm_type(self) -> str:
        return "compatible-gateway"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        payload = {
            "model": self.model_name,
            "messages": [_message_to_openai(m) for m in messages],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if stop:
            payload["stop"] = stop
        payload.update(kwargs)

        base = self.base_url.rstrip("/")
        url = base if base.endswith("/chat/completions") else base + "/chat/completions"
        headers = {"Content-Type": "application/json", **(self.headers or {})}
        if (self.auth_type or "api_key").lower() not in {"none", "noauth", "anonymous"}:
            headers["Authorization"] = f"Bearer {self.api_key or _token_cache.get()}"
        if self.verify_ssl == LLM_VERIFY_SSL:
            response = _gateway_client.post(url, headers=headers, json=payload)
        else:
            with httpx.Client(timeout=_gateway_client.timeout, verify=self.verify_ssl, limits=_HTTP_LIMITS) as client:
                response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()

        choice = data["choices"][0]
        content = choice.get("message", {}).get("content") or choice.get("text") or ""
        usage = _usage_dict(data.get("usage"))
        message = AIMessage(
            content=content,
            response_metadata={
                "model": data.get("model", self.model_name),
                "model_profile_id": self.profile_id,
                "token_usage": usage,
            },
        )
        return ChatResult(
            generations=[ChatGeneration(message=message)],
            llm_output={"token_usage": usage},
        )


class OAuthOpenAIEmbeddings(OpenAIEmbeddings):
    """OpenAI-compatible embeddings with the same OAuth token flow."""

    def _invocation_params(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        params = super()._invocation_params(*args, **kwargs)
        params["api_key"] = _token_cache.get()
        return params


def _profile_token(profile: ModelProfile) -> str:
    auth_type = (profile.auth_type or "api_key").lower()
    if auth_type in {"none", "noauth", "anonymous"}:
        return ""
    if auth_type in {"api_key", "bearer", "openai_api_key"}:
        token = profile.api_key or os.getenv("LLM_API_KEY", "")
        if not token:
            raise RuntimeError(f"LLM_API_KEY is required for model profile {profile.id}")
        return token
    if auth_type not in {"oauth_client_credentials", "oauth2_client_credentials", "client_credentials"}:
        token = profile.api_key or os.getenv("LLM_API_KEY", "")
        if not token:
            raise RuntimeError(f"model credential is required for profile {profile.id}")
        return token

    token_url = profile.token_url or TOKEN_URL
    client_id = profile.client_id or CLIENT_ID
    client_secret = profile.client_secret or CLIENT_SECRET
    if not token_url:
        raise RuntimeError(f"token_url is required for model profile {profile.id}")
    if not client_id or not client_secret:
        raise RuntimeError(f"client_id/client_secret are required for model profile {profile.id}")

    now = time.time()
    cached = _profile_token_cache.get(profile.id)
    if cached and now < cached[1] - 60:
        return cached[0]

    with _profile_token_lock:
        cached = _profile_token_cache.get(profile.id)
        if cached and now < cached[1] - 60:
            return cached[0]
        if profile.verify_ssl == OAUTH_VERIFY_SSL:
            resp = _oauth_client.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        else:
            with httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0, pool=2.0), verify=profile.verify_ssl, limits=_HTTP_LIMITS) as client:
                resp = client.post(
                    token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": client_id,
                        "client_secret": client_secret,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
        resp.raise_for_status()
        data = resp.json()
        token = data["access_token"]
        _profile_token_cache[profile.id] = (token, now + int(data.get("expires_in", 300)))
        return token


def get_llm(
    temperature: float = 0.1,
    max_tokens: int = 4096,
    profile_id: str | None = None,
    profile_override: dict[str, Any] | ModelProfile | None = None,
) -> GatewayChatModel:
    if isinstance(profile_override, ModelProfile):
        profile = profile_override
    elif isinstance(profile_override, dict) and profile_override:
        profile = model_profile_from_payload(profile_override) or select_model_profile(profile_id, role="primary")
    else:
        profile = select_model_profile(profile_id, role="primary")
    return GatewayChatModel(
        model_name=profile.model,
        base_url=profile.base_url or LLM_API_BASE,
        profile_id=profile.id,
        auth_type=profile.auth_type,
        api_key=_profile_token(profile),
        headers=profile.headers,
        verify_ssl=profile.verify_ssl,
        temperature=temperature,
        max_tokens=min(max_tokens, profile.max_tokens or max_tokens),
    )


async def stream_chat_text(
    prompt: str,
    *,
    profile_id: str | None = None,
    temperature: float = 0.1,
    max_tokens: int = 1600,
) -> AsyncIterator[str]:
    """Stream visible answer tokens from an OpenAI-compatible gateway.

    Only ``delta.content`` is exposed. Provider-specific hidden reasoning fields
    are deliberately ignored; the UI receives auditable workflow stages instead.
    """
    model = await asyncio.to_thread(
        lambda: get_llm(temperature=temperature, max_tokens=max_tokens, profile_id=profile_id)
    )
    base = model.base_url.rstrip("/")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream", **(model.headers or {})}
    if (model.auth_type or "api_key").lower() not in {"none", "noauth", "anonymous"}:
        headers["Authorization"] = f"Bearer {model.api_key or _token_cache.get()}"
    payload = {
        "model": model.model_name,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": model.temperature,
        "max_tokens": model.max_tokens,
        "stream": True,
    }
    timeout = httpx.Timeout(
        float(os.getenv("LLM_STREAM_TIMEOUT_SECONDS", "90")),
        connect=float(os.getenv("LLM_CONNECT_TIMEOUT_SECONDS", "5")),
        pool=3.0,
    )
    async with httpx.AsyncClient(timeout=timeout, verify=model.verify_ssl, limits=_HTTP_LIMITS) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                value = line.strip()
                if not value or value.startswith(":"):
                    continue
                if value.startswith("data:"):
                    value = value[5:].strip()
                if value == "[DONE]":
                    break
                try:
                    event = json.loads(value)
                except json.JSONDecodeError:
                    continue
                choice = (event.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                content = delta.get("content")
                if content is None and choice.get("text"):
                    content = choice.get("text")
                if content:
                    yield str(content)


def get_embeddings() -> OpenAIEmbeddings:
    return OpenAIEmbeddings(
        model=EMBEDDING_MODEL,
        openai_api_base=EMBEDDING_API_BASE,
        openai_api_key=os.getenv("EMBEDDING_API_KEY") or _token_cache.get(),
        http_client=_embedding_client,
        http_async_client=_embedding_async_client,
    )
