"""Aiso 백엔드 사이드카 — Ollama 통신 계층 (FastAPI)

Electron 메인 프로세스가 앱 시작 시 이 서버를 스폰한다:
  python -m uvicorn main:app --host 127.0.0.1 --port <동적포트>
"""

import hmac
import ipaddress
import json
import os
import re
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from agent import MAX_GEN_TOKENS, _chat_turn, resolve_approval, run_agent, run_research_chat
from agent_prompting import final_response_language_prompt
from agent_ledger import AgentExecutionLedger, LedgerError
from attachments import AttachmentError, append_attachment_context
from response_language import response_language_from_messages
from toolspec import (
    BUILTIN_TOOL_NAMES,
    NVIDIA_AGENT_SUPPORTED_TOOLS,
    get_builtin_tool_catalog,
    normalize_enabled_tool_names,
)
from comfy_client import (
    ComfyAPIError,
    DEFAULT_COMFY_BASE_URL,
    InvalidComfyURL,
    fetch_output_image as fetch_comfy_output_image,
    get_checkpoints as get_comfy_checkpoints,
    get_health as get_comfy_health,
)
from comfy_generation import GenerationError, generate_image as generate_comfy_image
from document_todos import delete_todo as delete_document_todo
from document_todos import apply_reschedule as apply_document_todo_reschedule
from document_todos import create_todo_item as create_document_todo_item
from document_todos import list_todos as list_document_todos
from document_todos import list_saved_todos as list_saved_document_todos
from document_todos import preview_reschedule as preview_document_todo_reschedule
from document_todos import update_todo as update_document_todo
from qa_scenarios import run_scenario_pack
from rag import RagError, build_index
from rag import search as rag_search
from rag import status as rag_status
from tools import ToolError, validate_workspace
from webcheck import run_web
from llm import (
    NVIDIA_BUILD_BASE_URL,
    LlmFailureKind,
    LlmModelRuntime,
    LlmProviderError,
    LlmRequest,
    LlmRuntime,
    create_runtime,
    canonicalize_nvidia_endpoint,
)

# discord.py는 선택적 — 미설치/임포트 오류가 사이드카(채팅·에이전트) 전체를 죽이지 않게 가드한다.
try:
    import discordbot
except Exception:  # noqa: BLE001
    discordbot = None

DEFAULT_OLLAMA = os.environ.get("AISO_OLLAMA_HOST", "http://localhost:11434")
MAX_DISCORD_PARSE_RETRIES = 2  # 디스코드 툴 루프의 툴콜 파싱오류 재생성 상한(agent 경로와 동일)

# 세션 인증 토큰 — Electron 메인이 사이드카 스폰 시 환경변수로 넘긴다.
# 백엔드는 127.0.0.1의 동적 포트에 붙지만 그것만으론 인증이 없어, 앱 실행 중
# 악성 웹페이지가 포트를 스캔해 cross-origin으로 /agent(파일 툴!)·/chat을 호출할 수 있다.
# 렌더러만 아는 이 토큰을 X-Aiso-Token 헤더로 검사해 근본적으로 잠근다.
AUTH_TOKEN = os.environ.get("AISO_AUTH_TOKEN", "")
CREDENTIAL_CHANNEL_TOKEN = os.environ.get("AISO_CREDENTIAL_CHANNEL_TOKEN", "")
AGENT_LEDGER_PATH = os.environ.get("AISO_AGENT_LEDGER_PATH", "")
NVIDIA_AGENT_GRANT_TTL_SECONDS = 60
NVIDIA_GRANT_STORE_MAX_RECORDS = 256


class _CredentialMemory:
    """Write-only process memory for a future NVIDIA runtime handoff."""

    def __init__(self) -> None:
        self._secret: bytearray | None = None
        self._binding: dict[str, str] | None = None
        self._used_nonces: set[str] = set()
        self._nonce_order: deque[str] = deque()

    def consume_nonce(self, nonce: str) -> None:
        if not nonce or len(nonce) < 32 or len(nonce) > 256 or nonce in self._used_nonces:
            raise HTTPException(status_code=409, detail="invalid or replayed credential nonce")
        self._used_nonces.add(nonce)
        self._nonce_order.append(nonce)
        while len(self._nonce_order) > 2048:
            self._used_nonces.discard(self._nonce_order.popleft())

    def set(self, binding: dict[str, str], api_key: str) -> None:
        self.bind(binding)
        self._secret = bytearray(api_key.encode("utf-8"))

    def bind(self, binding: dict[str, str]) -> None:
        self.clear_secret()
        self._binding = dict(binding)

    def credential_for(self, binding: dict[str, str]) -> str | None:
        if self._binding != binding:
            raise HTTPException(status_code=409, detail="NVIDIA credential binding mismatch")
        if self._secret is None:
            return None
        return self._secret.decode("utf-8")

    def clear_secret(self) -> None:
        if self._secret is not None:
            for index in range(len(self._secret)):
                self._secret[index] = 0
        self._secret = None
        self._binding = None

    def status(self) -> dict[str, Any]:
        return {"hasCredential": self._secret is not None, "binding": self._binding}


_credential_memory = _CredentialMemory()


class _NvidiaAgentGrantStore:
    """One-use in-memory grants minted only through the Main-only channel."""

    def __init__(self) -> None:
        self._grants: dict[str, dict[str, Any]] = {}

    def clear(self) -> None:
        self._grants.clear()

    def _prune(self) -> None:
        now = time.monotonic()
        expired = [token for token, grant in self._grants.items() if grant["expires"] <= now]
        for token in expired:
            self._grants.pop(token, None)

    def issue(
        self,
        *,
        session_id: str,
        assistant_turn_id: str,
        binding: dict[str, str],
        model: str,
        execution_scope: dict[str, Any],
        ttl_seconds: float = NVIDIA_AGENT_GRANT_TTL_SECONDS,
    ) -> str:
        self._prune()
        while len(self._grants) >= NVIDIA_GRANT_STORE_MAX_RECORDS:
            self._grants.pop(next(iter(self._grants)))
        # 32 random bytes = 256 bits. The token is never logged or persisted.
        token = secrets.token_urlsafe(32)
        self._grants[token] = {
            "sessionId": session_id,
            "assistantTurnId": assistant_turn_id,
            "binding": dict(binding),
            "model": model,
            "executionScope": execution_scope,
            "expires": time.monotonic() + min(NVIDIA_AGENT_GRANT_TTL_SECONDS, ttl_seconds),
        }
        return token

    def consume(
        self,
        token: str,
        *,
        session_id: str,
        assistant_turn_id: str,
        binding: dict[str, str],
        model: str,
    ) -> dict[str, Any]:
        self._prune()
        # Pop before comparison: even a mismatched attempt consumes the bearer.
        grant = self._grants.pop(token, None)
        if grant is None:
            raise HTTPException(status_code=409, detail="invalid or expired NVIDIA Agent grant")
        if (
            grant["sessionId"] != session_id
            or grant["assistantTurnId"] != assistant_turn_id
            or grant["binding"] != binding
            or grant["model"] != model
        ):
            raise HTTPException(status_code=409, detail="NVIDIA Agent grant scope mismatch")
        return dict(grant["executionScope"])


_nvidia_agent_grants = _NvidiaAgentGrantStore()


class _NvidiaResearchGrantStore:
    """One-use exact target grants for NVIDIA web-research chat."""

    def __init__(self) -> None:
        self._grants: dict[str, dict[str, Any]] = {}

    def clear(self) -> None:
        self._grants.clear()

    def _prune(self) -> None:
        now = time.monotonic()
        self._grants = {
            token: grant for token, grant in self._grants.items() if grant["expires"] > now
        }

    def issue(self, binding: dict[str, str], model: str, ttl_seconds: float) -> str:
        self._prune()
        while len(self._grants) >= NVIDIA_GRANT_STORE_MAX_RECORDS:
            self._grants.pop(next(iter(self._grants)))
        token = secrets.token_urlsafe(32)
        self._grants[token] = {
            "binding": dict(binding),
            "model": model,
            "expires": time.monotonic() + min(NVIDIA_AGENT_GRANT_TTL_SECONDS, ttl_seconds),
        }
        return token

    def consume(self, token: str, binding: dict[str, str], model: str) -> None:
        self._prune()
        grant = self._grants.pop(token, None)
        if not grant:
            raise HTTPException(status_code=409, detail="invalid or expired NVIDIA research grant")
        if grant["binding"] != binding or grant["model"] != model:
            raise HTTPException(status_code=409, detail="NVIDIA research grant scope mismatch")


_nvidia_research_grants = _NvidiaResearchGrantStore()


class _NvidiaDiscordGrantStore:
    """One-use exact target grants for Main-authorized Discord configuration."""

    def __init__(self) -> None:
        self._grants: dict[str, dict[str, Any]] = {}

    def clear(self) -> None:
        self._grants.clear()

    def _prune(self) -> None:
        now = time.monotonic()
        self._grants = {
            token: grant for token, grant in self._grants.items() if grant["expires"] > now
        }

    def issue(self, binding: dict[str, str], model: str, ttl_seconds: float) -> str:
        self._prune()
        while len(self._grants) >= NVIDIA_GRANT_STORE_MAX_RECORDS:
            self._grants.pop(next(iter(self._grants)))
        token = secrets.token_urlsafe(32)
        self._grants[token] = {
            "binding": dict(binding),
            "model": model,
            "expires": time.monotonic() + min(NVIDIA_AGENT_GRANT_TTL_SECONDS, ttl_seconds),
        }
        return token

    def consume(self, token: str, binding: dict[str, str], model: str) -> None:
        self._prune()
        grant = self._grants.pop(token, None)
        if not grant:
            raise HTTPException(status_code=409, detail="invalid or expired NVIDIA Discord grant")
        if grant["binding"] != binding or grant["model"] != model:
            raise HTTPException(status_code=409, detail="NVIDIA Discord grant scope mismatch")


_nvidia_discord_grants = _NvidiaDiscordGrantStore()
_agent_ledger_startup_error: str | None = None


def _credential_nonce(request: Request) -> str:
    nonce = request.headers.get("X-Aiso-Credential-Nonce", "")
    _credential_memory.consume_nonce(nonce)
    return nonce


def _credential_binding(deployment_mode: str, endpoint: str) -> dict[str, str]:
    try:
        canonical = canonicalize_nvidia_endpoint(deployment_mode, endpoint)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid NVIDIA credential binding") from exc
    return {"deploymentMode": deployment_mode, "endpoint": canonical}

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _agent_ledger_startup_error
    _agent_ledger_startup_error = None
    if AGENT_LEDGER_PATH:
        ledger: AgentExecutionLedger | None = None
        try:
            ledger = AgentExecutionLedger(AGENT_LEDGER_PATH)
            ledger.recover_incomplete()
        except LedgerError:
            _agent_ledger_startup_error = "NVIDIA Agent 실행 원장을 안전하게 열지 못했습니다."
        finally:
            if ledger is not None:
                ledger.close()
    yield
    _nvidia_agent_grants.clear()
    _nvidia_research_grants.clear()
    _nvidia_discord_grants.clear()
    _credential_memory.clear_secret()
    if discordbot is not None:
        await discordbot.stop()  # 앱 종료 시 디스코드 봇 게이트웨이 정리


app = FastAPI(title="aiso-backend", lifespan=_lifespan)

# 토큰 인증에서 제외하는 경로: /f/ 정적 미리보기는 iframe 네비게이션이라 커스텀 헤더를
# 실을 수 없다(작업폴더 내 읽기전용이라 위험이 낮다). OPTIONS 프리플라이트는 CORS가 처리.
_AUTH_EXEMPT_PREFIXES = ("/f/",)


class TokenAuthMiddleware:
    """세션 토큰(X-Aiso-Token) 검사 — 순수 ASGI 미들웨어.

    BaseHTTPMiddleware가 아닌 순수 ASGI로 작성한 이유: NDJSON 스트리밍 응답
    (/chat·/agent·/rag/index·/ollama/pull)을 버퍼링/간섭하지 않기 위해서다.
    AISO_AUTH_TOKEN 미설정 시(수동 uvicorn 실행·테스트)엔 잠금을 열어 개발 편의를 유지한다.
    실제 앱에선 Electron 메인이 항상 토큰을 넘기므로 상시 잠긴다.
    """

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "")
        path = scope.get("path", "")
        headers = dict(scope.get("headers") or [])
        if (
            path.startswith("/internal/credentials/")
            or path.startswith("/internal/nvidia-agent/")
            or path.startswith("/internal/nvidia-research/")
            or path.startswith("/internal/nvidia-discord/")
        ):
            supplied = headers.get(b"x-aiso-credential-token", b"").decode("latin-1")
            if not CREDENTIAL_CHANNEL_TOKEN or not hmac.compare_digest(supplied, CREDENTIAL_CHANNEL_TOKEN):
                resp = JSONResponse({"detail": "unauthorized"}, status_code=401)
                await resp(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return
        if not self.token:
            await self.app(scope, receive, send)
            return
        if method == "OPTIONS" or path.startswith(_AUTH_EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return
        supplied = headers.get(b"x-aiso-token", b"").decode("latin-1")
        if not hmac.compare_digest(supplied, self.token):
            resp = JSONResponse({"detail": "unauthorized"}, status_code=401)
            await resp(scope, receive, send)
            return
        await self.app(scope, receive, send)


# 미들웨어는 나중에 추가된 것이 바깥(먼저 실행)이다. CORS를 바깥에 둬서 (1) OPTIONS
# 프리플라이트를 CORS가 먼저 처리하고 (2) 인증 실패(401) 응답에도 ACAO 헤더가 붙게 한다.
app.add_middleware(TokenAuthMiddleware, token=AUTH_TOKEN)
# CORS: 실제 앱 오리진만 허용. prod는 Electron file:// 로더라 Origin이 'null'로 온다.
# (null 오리진은 위조 가능하므로 CORS는 보조 방어일 뿐, 근본 잠금은 위 토큰 인증이 담당.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",  # dev: Vite dev 서버
        "http://127.0.0.1:5173",  # dev: 127.0.0.1로 접속하는 경우
        "null",  # prod: Electron file:// 로더 → Origin: null
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


class CredentialSetRequest(BaseModel):
    deploymentMode: Literal["build", "nim"]
    endpoint: str = Field(min_length=1, max_length=2048)
    apiKey: str = Field(min_length=1, max_length=16384)


class CredentialBindRequest(BaseModel):
    deploymentMode: Literal["build", "nim"]
    endpoint: str = Field(min_length=1, max_length=2048)


class NvidiaAgentGrantRequest(BaseModel):
    sessionId: str = Field(min_length=16, max_length=256, pattern=r"^[A-Za-z0-9._:-]+$")
    assistantTurnId: str = Field(min_length=16, max_length=256, pattern=r"^[A-Za-z0-9._:-]+$")
    deploymentMode: Literal["build", "nim"]
    endpoint: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=512)
    ttlSeconds: float = Field(gt=0, le=NVIDIA_AGENT_GRANT_TTL_SECONDS)
    executionScope: dict[str, Any]


class NvidiaResearchGrantRequest(BaseModel):
    deploymentMode: Literal["build", "nim"]
    endpoint: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=512)
    ttlSeconds: float = Field(gt=0, le=NVIDIA_AGENT_GRANT_TTL_SECONDS)


class NvidiaDiscordGrantRequest(NvidiaResearchGrantRequest):
    pass


_NVIDIA_AGENT_SCOPE_TOOLS = NVIDIA_AGENT_SUPPORTED_TOOLS
_NVIDIA_AGENT_WORKSPACE_TOOLS = frozenset({
    "list_dir", "list_tree", "read_file", "grep", "glob", "create_dir", "move", "convert_document", "analyze_document_calendar",
    "write_file", "edit_file", "multi_edit",
    "write_code_file", "edit_code_file", "multi_edit_code_file",
    "delete_file", "delete_dir", "run_web", "run_code", "run_command",
})


def _require_local_ollama_host(value: str) -> str:
    try:
        parsed = urlsplit(str(value or "").strip())
        _ = parsed.port
    except ValueError as error:
        raise HTTPException(status_code=400, detail="local Ollama endpoint is invalid") from error
    host = (parsed.hostname or "").lower()
    loopback = host == "localhost"
    try:
        address = ipaddress.ip_address(host)
        loopback = loopback or (address.is_loopback and getattr(address, "ipv4_mapped", None) is None)
    except ValueError:
        pass
    if (
        parsed.scheme not in ("http", "https")
        or not loopback
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise HTTPException(status_code=400, detail="RAG requires a local Ollama endpoint")
    return str(value).strip().rstrip("/")


def _validate_nvidia_agent_execution_scope(raw: dict[str, Any]) -> dict[str, Any]:
    if set(raw) != {
        "fingerprint", "approvalMode", "workspace", "ragEnabled", "ollamaHost", "ragTopK", "allowedTools", "comfy"
    }:
        raise HTTPException(status_code=400, detail="invalid NVIDIA Agent data scope")
    fingerprint = raw.get("fingerprint")
    approval_mode = raw.get("approvalMode")
    workspace = raw.get("workspace")
    rag_enabled = raw.get("ragEnabled")
    ollama_host = raw.get("ollamaHost")
    rag_top_k = raw.get("ragTopK")
    allowed_tools = raw.get("allowedTools")
    comfy = raw.get("comfy")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise HTTPException(status_code=400, detail="invalid NVIDIA Agent data scope")
    if approval_mode not in ("manual", "read", "auto"):
        raise HTTPException(status_code=400, detail="invalid NVIDIA Agent data scope")
    if not isinstance(workspace, str) or len(workspace) > 32768 or "\x00" in workspace:
        raise HTTPException(status_code=400, detail="invalid NVIDIA Agent data scope")
    if not isinstance(rag_enabled, bool) or (rag_enabled and not workspace):
        raise HTTPException(status_code=400, detail="invalid NVIDIA Agent data scope")
    if (
        not isinstance(ollama_host, str)
        or isinstance(rag_top_k, bool)
        or not isinstance(rag_top_k, int)
        or rag_top_k < 0
        or rag_top_k > 20
        or (rag_enabled and (not ollama_host or rag_top_k < 1))
        or (not rag_enabled and (ollama_host or rag_top_k != 0))
    ):
        raise HTTPException(status_code=400, detail="invalid NVIDIA Agent data scope")
    if rag_enabled:
        _require_local_ollama_host(ollama_host)
    if (
        not isinstance(allowed_tools, list)
        or len(allowed_tools) > len(_NVIDIA_AGENT_SCOPE_TOOLS)
        or any(not isinstance(name, str) or name not in _NVIDIA_AGENT_SCOPE_TOOLS for name in allowed_tools)
        or len(set(allowed_tools)) != len(allowed_tools)
    ):
        raise HTTPException(status_code=400, detail="invalid NVIDIA Agent data scope")
    if not workspace and any(
        name in _NVIDIA_AGENT_WORKSPACE_TOOLS or name == "search_docs" for name in allowed_tools
    ):
        raise HTTPException(status_code=400, detail="invalid NVIDIA Agent data scope")
    if ("search_docs" in allowed_tools) != rag_enabled:
        raise HTTPException(status_code=400, detail="invalid NVIDIA Agent data scope")
    if not isinstance(comfy, dict) or set(comfy) != {
        "enabled", "baseUrl", "profiles", "selectionMode", "selectedProfileId"
    }:
        raise HTTPException(status_code=400, detail="invalid NVIDIA Agent data scope")
    enabled = comfy.get("enabled")
    profiles = comfy.get("profiles")
    selection_mode = comfy.get("selectionMode")
    selected_profile_id = comfy.get("selectedProfileId")
    if (
        not isinstance(enabled, bool)
        or not isinstance(comfy.get("baseUrl"), str)
        or not isinstance(profiles, list)
        or len(profiles) > 100
        or selection_mode not in ("auto", "manual")
        or (selected_profile_id is not None and not isinstance(selected_profile_id, str))
        or ("generate_image" in allowed_tools) != enabled
    ):
        raise HTTPException(status_code=400, detail="invalid NVIDIA Agent data scope")
    if enabled:
        if not comfy["baseUrl"] or not profiles:
            raise HTTPException(status_code=400, detail="invalid NVIDIA Agent data scope")
        if selection_mode == "manual" and not selected_profile_id:
            raise HTTPException(status_code=400, detail="invalid NVIDIA Agent data scope")
    elif comfy["baseUrl"] or profiles or selected_profile_id is not None:
        raise HTTPException(status_code=400, detail="invalid NVIDIA Agent data scope")
    try:
        if len(json.dumps(raw, ensure_ascii=False)) > 1_000_000:
            raise HTTPException(status_code=413, detail="NVIDIA Agent data scope is too large")
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="invalid NVIDIA Agent data scope") from error
    return json.loads(json.dumps(raw, ensure_ascii=False))


@app.post("/internal/credentials/set")
async def credential_set(request: Request, body: CredentialSetRequest):
    _credential_nonce(request)
    _nvidia_agent_grants.clear()
    _nvidia_research_grants.clear()
    _nvidia_discord_grants.clear()
    binding = _credential_binding(body.deploymentMode, body.endpoint)
    _credential_memory.set(binding, body.apiKey)
    return {"ok": True}


@app.post("/internal/credentials/clear")
async def credential_clear(request: Request):
    _credential_nonce(request)
    _nvidia_agent_grants.clear()
    _nvidia_research_grants.clear()
    _nvidia_discord_grants.clear()
    _credential_memory.clear_secret()
    return {"ok": True}


@app.post("/internal/credentials/bind")
async def credential_bind(request: Request, body: CredentialBindRequest):
    _credential_nonce(request)
    _nvidia_agent_grants.clear()
    _nvidia_research_grants.clear()
    _nvidia_discord_grants.clear()
    binding = _credential_binding(body.deploymentMode, body.endpoint)
    if body.deploymentMode == "build":
        raise HTTPException(status_code=400, detail="NVIDIA Build requires a credential")
    _credential_memory.bind(binding)
    return {"ok": True}


@app.post("/internal/credentials/status")
async def credential_status(request: Request):
    _credential_nonce(request)
    return _credential_memory.status()


@app.post("/internal/nvidia-agent/grant")
async def nvidia_agent_grant(request: Request, body: NvidiaAgentGrantRequest):
    _credential_nonce(request)
    binding = _credential_binding(body.deploymentMode, body.endpoint)
    credential = _credential_memory.credential_for(binding)
    if body.deploymentMode == "build" and not credential:
        raise HTTPException(status_code=409, detail="NVIDIA credential is not ready")
    token = _nvidia_agent_grants.issue(
        session_id=body.sessionId,
        assistant_turn_id=body.assistantTurnId,
        binding=binding,
        model=body.model.strip(),
        execution_scope=_validate_nvidia_agent_execution_scope(body.executionScope),
        ttl_seconds=body.ttlSeconds,
    )
    return {"grantId": token, "expiresInSeconds": body.ttlSeconds}


@app.post("/internal/nvidia-agent/clear")
async def nvidia_agent_grant_clear(request: Request):
    _credential_nonce(request)
    _nvidia_agent_grants.clear()
    return {"ok": True}


@app.post("/internal/nvidia-research/grant")
async def nvidia_research_grant(request: Request, body: NvidiaResearchGrantRequest):
    _credential_nonce(request)
    binding = _credential_binding(body.deploymentMode, body.endpoint)
    credential = _credential_memory.credential_for(binding)
    if body.deploymentMode == "build" and not credential:
        raise HTTPException(status_code=409, detail="NVIDIA credential is not ready")
    token = _nvidia_research_grants.issue(binding, body.model.strip(), body.ttlSeconds)
    return {"grantId": token, "expiresInSeconds": body.ttlSeconds}


@app.post("/internal/nvidia-research/clear")
async def nvidia_research_grant_clear(request: Request):
    _credential_nonce(request)
    _nvidia_research_grants.clear()
    return {"ok": True}


@app.post("/internal/nvidia-discord/grant")
async def nvidia_discord_grant(request: Request, body: NvidiaDiscordGrantRequest):
    _credential_nonce(request)
    binding = _credential_binding(body.deploymentMode, body.endpoint)
    credential = _credential_memory.credential_for(binding)
    if body.deploymentMode == "build" and not credential:
        raise HTTPException(status_code=409, detail="NVIDIA credential is not ready")
    token = _nvidia_discord_grants.issue(binding, body.model.strip(), body.ttlSeconds)
    return {"grantId": token, "expiresInSeconds": body.ttlSeconds}


@app.post("/internal/nvidia-discord/clear")
async def nvidia_discord_grant_clear(request: Request):
    _credential_nonce(request)
    _nvidia_discord_grants.clear()
    return {"ok": True}


class NvidiaTargetRequest(BaseModel):
    deployment_mode: Literal["build", "nim"]
    endpoint: str = Field(min_length=1, max_length=2048)


class NvidiaCapabilityProbeRequest(NvidiaTargetRequest):
    model: str = Field(min_length=1, max_length=512)


def _nvidia_runtime_for_target(body: NvidiaTargetRequest) -> LlmRuntime:
    endpoint = canonicalize_nvidia_endpoint(body.deployment_mode, body.endpoint)
    binding = {"deploymentMode": body.deployment_mode, "endpoint": endpoint}
    credential = _credential_memory.credential_for(binding)
    if body.deployment_mode == "build" and not credential:
        raise HTTPException(status_code=409, detail="NVIDIA 자격 증명이 준비되지 않았습니다.")
    return create_runtime(
        "nvidia",
        endpoint,
        credential=credential,
        deployment_mode=body.deployment_mode,
    )


def _raise_nvidia_discovery_error(error: Exception) -> None:
    if isinstance(error, HTTPException):
        raise error
    if isinstance(error, LlmProviderError):
        status = error.status if 400 <= error.status <= 599 else 502
        raise HTTPException(status_code=status, detail=error.body) from None
    if isinstance(error, (ValueError, TypeError)):
        raise HTTPException(status_code=400, detail="NVIDIA 조회 설정을 확인하세요.") from None
    raise HTTPException(status_code=502, detail="NVIDIA 조회 중 안전하게 처리할 수 없는 오류가 발생했습니다.") from None


@app.post("/nvidia/models")
async def nvidia_models(body: NvidiaTargetRequest):
    """Explicit user-triggered model discovery for one credential-bound target."""
    try:
        return {"models": await _nvidia_runtime_for_target(body).list_models()}
    except Exception as error:  # noqa: BLE001 - never expose upstream/raw credential detail
        _raise_nvidia_discovery_error(error)


@app.post("/nvidia/capabilities/probe")
async def nvidia_capability_probe(body: NvidiaCapabilityProbeRequest):
    """Run a non-executing forced-tool protocol probe for one exact model."""
    try:
        capabilities = await _nvidia_runtime_for_target(body).inspect_capabilities(body.model.strip())
        return {
            "capabilities": {
                "chat": capabilities.chat,
                "stream": capabilities.stream,
                "tools": capabilities.tools,
            }
        }
    except Exception as error:  # noqa: BLE001 - never expose upstream/raw credential detail
        _raise_nvidia_discovery_error(error)


class ChatMessage(BaseModel):
    role: str
    content: str
    # Opaque IDs only.  The Electron main process stages user selections in an
    # app-managed store; the renderer never sends an arbitrary local path.
    attachments: list[str] = Field(default_factory=list, max_length=16)


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    provider: Literal["ollama", "nvidia"] = "ollama"
    deployment_mode: Literal["build", "nim"] | None = None
    endpoint: str | None = None
    model: str = "gemma4:12b"
    reasoning_effort: str = Field(default="medium", pattern="^(low|medium|high)$")
    temperature: float = 0.7
    # 컨텍스트 길이(num_ctx) — 사용자 설정. 클수록 긴 추론·대화 가능하나 VRAM↑
    context_length: int = 16384
    ollama_host: str | None = None
    # 모델 상주 유지 시간 — 유휴 시 언로드(콜드 재로드 ~5.8s) 방지. "30m"/"-1"(무한)/"0"(즉시 언로드)
    keep_alive: str = "30m"
    # 웹 검색(리서치) — 켜면 web_search·web_fetch 조사 루프로 흐른다(파일 툴 없음). 기본 꺼짐(로컬 처리).
    research: bool = False
    nvidia_research_grant: str = ""


# ---- 라이브 미리보기: 작업 폴더를 정적 서빙 (우측 패널 iframe이 이걸 띄운다) ----
_preview_root: Path | None = None

# 미리보기(/f) 응답에 거는 CSP — egress(유출) 제어. 업계(Claude Code allowlist·Codex 오프라인)와
# 같은 발상으로, 미리보기 페이지가 데이터를 '밖으로 보내는' 채널을 막는다:
#   - default-src 'self' (+inline·eval·data·blob): 자기 오리진 자산·인라인 JS는 허용(자체완결 게임 정상),
#     외부 도메인 리소스(외부 img/script beacon)는 차단.
#   - connect-src 'self': fetch·XHR·WebSocket·sendBeacon을 외부로 못 보냄(핵심 유출 채널 차단).
#     같은 오리진 읽기는 되나 내보낼 통로가 없어 무해.
#   - form-action·base-uri 'self', object-src 'none': 폼 전송·base 하이재킹·플러그인 차단.
# 대가: CDN·외부 폰트/이미지·런타임 외부 fetch를 쓰는 미리보기는 깨질 수 있다(자체완결이면 무영향).
PREVIEW_CSP = (
    "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; "
    "connect-src 'self'; form-action 'self'; base-uri 'self'; object-src 'none'"
)


class PreviewRequest(BaseModel):
    workspace: str


@app.post("/preview")
async def set_preview(req: PreviewRequest):
    """미리보기 루트(작업 폴더)를 설정한다. 프론트가 작업 폴더 선택 시 호출."""
    global _preview_root
    try:
        _preview_root = validate_workspace(req.workspace)
        return {"ok": True}
    except ToolError as e:
        return {"ok": False, "detail": str(e)}


@app.get("/f/{file_path:path}")
async def serve_preview_file(file_path: str):
    """작업 폴더 내 파일을 서빙 (폴더 밖 접근 차단). iframe이 상대 경로 자산까지 로드."""
    if _preview_root is None:
        raise HTTPException(status_code=404, detail="미리보기 폴더가 설정되지 않았습니다.")
    target = (_preview_root / file_path).resolve()
    if target != _preview_root and _preview_root not in target.parents:
        raise HTTPException(status_code=403, detail="작업 폴더 밖 접근 거부")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="파일 없음")
    # CSP로 미리보기의 외부 유출 채널을 차단(자체완결 콘텐츠는 정상 동작).
    return FileResponse(str(target), headers={"Content-Security-Policy": PREVIEW_CSP})


@app.get("/health")
async def health(host: str | None = None):
    """백엔드 생존 + Ollama 도달성/모델 목록."""
    target = (host or DEFAULT_OLLAMA).rstrip("/")
    try:
        models = await create_runtime("ollama", target).list_models()
        return {"status": "ok", "ollama": True, "models": models}
    except Exception as e:  # noqa: BLE001 — 도달 실패 사유를 그대로 전달
        return {"status": "ok", "ollama": False, "models": [], "detail": str(e)[:200]}


@app.get("/comfy/health")
async def comfy_health(base_url: str = DEFAULT_COMFY_BASE_URL):
    """사용자가 연결한 로컬 ComfyUI의 버전·장치 상태를 조회한다."""
    try:
        return await get_comfy_health(base_url)
    except InvalidComfyURL as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@app.get("/comfy/checkpoints")
async def comfy_checkpoints(base_url: str = DEFAULT_COMFY_BASE_URL):
    """로컬 ComfyUI에 등록된 체크포인트 이름 목록을 조회한다."""
    try:
        return await get_comfy_checkpoints(base_url)
    except InvalidComfyURL as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    except ComfyAPIError as e:
        raise HTTPException(status_code=502, detail=str(e)) from None


@app.get("/comfy/image")
async def comfy_image(
    filename: str,
    subfolder: str = "",
    storage_type: str = Query(default="output", alias="type"),
    base_url: str = DEFAULT_COMFY_BASE_URL,
):
    """ComfyUI 결과 참조를 검증한 뒤 이미지 바이트만 인증된 렌더러에 중계한다."""
    try:
        data, media_type = await fetch_comfy_output_image(
            base_url,
            filename,
            subfolder,
            storage_type,
        )
        return Response(
            content=data,
            media_type=media_type,
            headers={
                "Cache-Control": "private, max-age=86400",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except InvalidComfyURL as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    except ComfyAPIError as e:
        raise HTTPException(status_code=502, detail=str(e)) from None


async def _stream_chat(runtime: LlmRuntime, request: LlmRequest):
    """공용 계약 이벤트를 기존 렌더러 NDJSON 이벤트로 변환한다.

    기존 호출자는 이 호환 래퍼만 사용하고, Ollama NDJSON 전송은 어댑터가 전담한다.
    """
    stream = runtime.chat_stream(request)
    stream_completed = False
    try:
        async for event in stream:
            if event.kind == "thinking":
                yield {"type": "thinking", "text": event.text}
            elif event.kind == "content":
                yield {"type": "content", "text": event.text}
            elif event.kind == "tool_call_delta":
                yield {"type": "tool_calls", "calls": list(event.tool_calls or [])}
            elif event.kind == "usage":
                yield {
                    "type": "usage",
                    "input": event.input_tokens,
                    "output": event.output_tokens,
                    "total": event.total_tokens,
                }
            elif event.kind == "incomplete":
                yield {"type": "incomplete", "error": event.error or "응답 스트림이 불완전하게 종료되었습니다."}
            elif event.kind == "cancelled":
                yield {"type": "cancelled", "error": event.error or "응답이 취소되었습니다."}
            elif event.kind == "error":
                yield {"type": "error", "error": event.error or "LLM 응답 오류"}
            elif event.kind == "done":
                if event.done_reason == "length":
                    yield {
                        "type": "notice",
                        "text": "⚠ 컨텍스트 한도 도달 — 응답이 잘렸습니다. 설정에서 컨텍스트 길이를 늘려보세요.",
                    }
                yield {
                    "type": "done",
                    "eval_count": event.output_tokens,  # 생성 토큰 (출력만 집계)
                    "total_duration": event.total_duration,
                }
        stream_completed = True
    finally:
        if not stream_completed:
            await stream.aclose()


async def _prepare_model(host: str, model: str) -> LlmModelRuntime:
    """실행 시작 시 runtime 모델 준비 결과를 고정한다."""
    return await create_runtime("ollama", host).prepare_model(model)


def _messages_with_latest_attachments(
    messages: list[ChatMessage], *, allow_images: bool
) -> list[dict[str, Any]]:
    """Attach bounded local material to the current request only.

    Re-reading every historical attachment on each follow-up would silently
    inflate small local-model contexts.  The current user turn is the explicit
    attachment scope; conversation history still retains the ordinary text.
    """
    prepared = [{"role": message.role, "content": message.content} for message in messages]
    if not messages:
        return prepared
    last_user = next((message for message in reversed(messages) if message.role == "user"), None)
    if last_user and last_user.attachments:
        return append_attachment_context(prepared, last_user.attachments, allow_images=allow_images)
    return prepared


def _original_user_message_dicts(messages: list[ChatMessage]) -> list[dict[str, str]]:
    """Return typed user text only, before attachments are expanded for the model."""
    return [
        {"role": message.role, "content": message.content}
        for message in messages
        if message.role == "user"
    ]


def _latest_original_user_text(messages: list[ChatMessage]) -> str:
    return next(
        (message.content for message in reversed(messages) if message.role == "user"),
        "",
    )


def _request_response_language(messages: list[ChatMessage]) -> str:
    """Pick language before attachment/RAG/tool context can affect the decision."""
    return response_language_from_messages(_original_user_message_dicts(messages), fallback="ko")


@app.post("/chat")
async def chat(req: ChatRequest):
    """NDJSON 스트리밍 채팅. think(추론 강도)는 지원 모델(gemma4·gpt-oss 등)에 적용된다."""
    host = (
        _require_local_ollama_host(req.ollama_host or DEFAULT_OLLAMA)
        if req.provider == "nvidia" and req.research
        else (req.ollama_host or DEFAULT_OLLAMA).rstrip("/")
    )
    response_language = _request_response_language(req.messages)
    try:
        prepared_messages = _messages_with_latest_attachments(
            req.messages, allow_images=req.provider == "ollama"
        )
    except AttachmentError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    runtime: LlmRuntime | None = None
    if req.provider == "ollama":
        runtime = create_runtime("ollama", host)
    chat_messages = [
        {"role": "system", "content": final_response_language_prompt(response_language)},
        *prepared_messages,
    ]
    base = LlmRequest(
        model=req.model,
        messages=chat_messages,
        temperature=req.temperature,
        max_output_tokens=MAX_GEN_TOKENS,
        provider_options={
            "keep_alive": req.keep_alive,  # 모델 상주 → 콜드 재로드 방지
            "num_ctx": req.context_length,
        },
    )

    async def gen():
        if req.provider == "nvidia":
            if not req.model.strip():
                yield json.dumps(
                    {"type": "error", "error": "NVIDIA 모델명을 설정해 주세요."}, ensure_ascii=False
                ) + "\n"
                return
            try:
                mode = req.deployment_mode or "build"
                endpoint = canonicalize_nvidia_endpoint(mode, req.endpoint or "")
                binding = {"deploymentMode": mode, "endpoint": endpoint}
                if req.research:
                    _nvidia_research_grants.consume(
                        req.nvidia_research_grant, binding, req.model.strip()
                    )
                credential = _credential_memory.credential_for(binding)
                if mode == "build" and not credential:
                    raise ValueError("NVIDIA Build API 키가 준비되지 않았습니다.")
                nvidia_runtime = create_runtime(
                    "nvidia",
                    endpoint,
                    credential=credential,
                    deployment_mode=mode,
                )
                chat_stream = (
                    run_research_chat(
                        host=host,
                        model=req.model,
                        messages=prepared_messages,
                        reasoning_effort=req.reasoning_effort,
                        temperature=req.temperature,
                        context_length=req.context_length,
                        keep_alive=req.keep_alive,
                        runtime=nvidia_runtime,
                        strict_tool_protocol=True,
                        response_language=response_language,
                    )
                    if req.research
                    else _stream_chat(nvidia_runtime, base)
                )
                completed = False
                try:
                    async for chunk in chat_stream:
                        yield json.dumps(chunk, ensure_ascii=False) + "\n"
                    completed = True
                finally:
                    if not completed:
                        await chat_stream.aclose()
                return
            except HTTPException:
                yield json.dumps(
                    {"type": "error", "error": "현재 NVIDIA 배포 대상에 맞는 자격 증명이 준비되지 않았습니다."},
                    ensure_ascii=False,
                ) + "\n"
                return
            except LlmProviderError as e:
                yield json.dumps({"type": "error", "error": e.body}, ensure_ascii=False) + "\n"
                return
            except (ValueError, TypeError):
                yield json.dumps(
                    {"type": "error", "error": "NVIDIA 실행 설정 또는 자격 증명을 확인하세요."}, ensure_ascii=False
                ) + "\n"
                return
            except Exception:  # noqa: BLE001 - upstream detail/key must never escape
                yield json.dumps(
                    {"type": "error", "error": "NVIDIA 연결 중 안전하게 처리할 수 없는 오류가 발생했습니다."},
                    ensure_ascii=False,
                ) + "\n"
                return

        # 웹 검색 켜짐 → 조사 루프(web_search·web_fetch)로 위임. NDJSON 이벤트는 동일 계약.
        if req.research:
            research_stream = run_research_chat(
                host=host,
                model=req.model,
                messages=prepared_messages,
                reasoning_effort=req.reasoning_effort,
                temperature=req.temperature,
                context_length=req.context_length,
                keep_alive=req.keep_alive,
                response_language=response_language,
            )
            research_completed = False
            try:
                async for ev in research_stream:
                    yield json.dumps(ev, ensure_ascii=False) + "\n"
                research_completed = True
            finally:
                if not research_completed:
                    await research_stream.aclose()
            return

        assert runtime is not None
        model_runtime = await _prepare_model(host, req.model)
        attempts = runtime.prepare_attempts(base, req.reasoning_effort, model_runtime)
        noticed = False
        for i, attempt in enumerate(attempts):
            try:
                chat_stream = _stream_chat(runtime, attempt)
                chat_completed = False
                try:
                    async for chunk in chat_stream:
                        yield json.dumps(chunk, ensure_ascii=False) + "\n"
                    chat_completed = True
                finally:
                    if not chat_completed:
                        await chat_stream.aclose()
                return
            except LlmProviderError as e:
                last = i == len(attempts) - 1
                load_failure = e.kind is LlmFailureKind.LOAD_FAILURE
                if not last and (load_failure or e.kind is LlmFailureKind.REASONING_UNSUPPORTED):
                    if load_failure and not noticed:
                        noticed = True
                        print("[ollama] 적재 실패(VRAM 부족 추정) → CPU 오프로드로 재시도")
                        yield json.dumps(
                            {"type": "notice", "text": "VRAM 부족 — CPU 오프로드로 실행합니다 (느려질 수 있어요)"},
                            ensure_ascii=False,
                        ) + "\n"
                    continue
                yield json.dumps(
                    {"type": "error", "error": f"{e.provider_name} 오류 ({e.status}): {e.body[:300]}"},
                    ensure_ascii=False,
                ) + "\n"
                return
            except Exception as e:  # noqa: BLE001
                yield json.dumps(
                    {"type": "error", "error": f"연결 실패: {e}"}, ensure_ascii=False
                ) + "\n"
                return

    return StreamingResponse(gen(), media_type="application/x-ndjson")


# ---- Discord 봇 (채팅 + 서버 구성) ----
async def _discord_step(
    messages: list,
    *,
    model: str,
    context_length: int,
    keep_alive: str,
    host: str,
    tools: list | None = None,
    provider: Literal["ollama", "nvidia"] = "ollama",
    deployment_mode: Literal["build", "nim"] | None = None,
    endpoint: str | None = None,
) -> dict:
    """디스코드 봇용 한 턴 생성 — 전체 텍스트와 툴 호출을 모아 돌려준다(오프로드 사다리 재사용)."""
    base = LlmRequest(
        model=model,
        messages=messages,
        tools=tools,
        temperature=0.7,
        max_output_tokens=MAX_GEN_TOKENS,
        provider_options={"keep_alive": keep_alive, "num_ctx": context_length},
    )
    if provider == "nvidia":
        mode = deployment_mode or "build"
        exact_endpoint = canonicalize_nvidia_endpoint(mode, endpoint or "")
        binding = {"deploymentMode": mode, "endpoint": exact_endpoint}
        credential = _credential_memory.credential_for(binding)
        if mode == "build" and not credential:
            return {"content": "(NVIDIA 자격 증명이 준비되지 않았습니다.)", "tool_calls": []}
        runtime = create_runtime(
            "nvidia", exact_endpoint, credential=credential, deployment_mode=mode
        )
        model_runtime = await runtime.prepare_model(model)
    else:
        runtime = create_runtime("ollama", host)
        model_runtime = await _prepare_model(host, model)
    attempts = runtime.prepare_attempts(base, "medium", model_runtime)
    for i, attempt in enumerate(attempts):
        parse_tries = 0
        while True:  # 파싱오류는 같은 payload 재생성(에이전트 경로와 동일 회복 계약), 그 외는 아래에서 분기
            try:
                parts: list[str] = []
                final_content: str | None = None
                calls: list = []
                chat_stream = (
                    _chat_turn(host, attempt, runtime, strict_tool_protocol=True)
                    if provider == "nvidia"
                    else _stream_chat(runtime, attempt)
                )
                chat_completed = False
                try:
                    async for chunk in chat_stream:
                        if chunk.get("_final"):
                            if provider == "nvidia":
                                final_content = chunk.get("content", "")
                            else:
                                parts.append(chunk.get("content", ""))
                            calls.extend(chunk.get("tool_calls") or [])
                        elif chunk.get("type") == "content":
                            parts.append(chunk["text"])
                        elif chunk.get("type") == "tool_calls":
                            calls.extend(chunk["calls"])
                    chat_completed = True
                finally:
                    if not chat_completed:
                        await chat_stream.aclose()
                content = final_content if provider == "nvidia" and final_content is not None else "".join(parts)
                return {"content": content.strip(), "tool_calls": calls}
            except LlmProviderError as e:
                # 모델이 도구를 지원하지 않으면(400) 도구 없이 한 번 더 시도(도구 못 붙인다고 채팅 전체가 죽지 않게).
                if provider == "ollama" and tools and e.kind is LlmFailureKind.TOOLS_UNSUPPORTED:
                    return await _discord_step(
                        messages, model=model, context_length=context_length,
                        keep_alive=keep_alive, host=host, tools=None,
                    )
                # gpt-oss 툴콜 파싱 오류 → 같은 payload로 재생성(대개 회복). 안 그러면 서버구성이 무응답으로 실패.
                if (
                    provider == "ollama"
                    and e.kind is LlmFailureKind.TOOL_PARSE
                    and parse_tries < MAX_DISCORD_PARSE_RETRIES
                ):
                    parse_tries += 1
                    continue
                if i < len(attempts) - 1 and e.kind in (
                    LlmFailureKind.LOAD_FAILURE,
                    LlmFailureKind.REASONING_UNSUPPORTED,
                ):
                    break  # 오프로드 사다리 다음 단계로
                return {"content": f"(모델 오류 {e.status})", "tool_calls": []}
            except Exception as e:  # noqa: BLE001
                return {"content": f"(연결 실패: {e})", "tool_calls": []}
    return {"content": "(응답 실패)", "tool_calls": []}


async def _discord_generate(
    messages: list, *, model: str, context_length: int, keep_alive: str, host: str,
    provider: Literal["ollama", "nvidia"] = "ollama",
    deployment_mode: Literal["build", "nim"] | None = None,
    endpoint: str | None = None,
) -> str:
    """디스코드 봇용 — 전체 응답 텍스트만 필요할 때(_discord_step의 얇은 래퍼)."""
    r = await _discord_step(
        messages, model=model, context_length=context_length, keep_alive=keep_alive, host=host,
        provider=provider, deployment_mode=deployment_mode, endpoint=endpoint,
    )
    return r["content"] or "(빈 응답)"


async def _discord_research(
    messages: list, *, model: str, context_length: int, keep_alive: str, host: str,
    provider: Literal["ollama", "nvidia"] = "ollama",
    deployment_mode: Literal["build", "nim"] | None = None,
    endpoint: str | None = None,
    response_language: str = "ko",
) -> str:
    """Discord 최신 질문·브리핑용 — 근거를 확보한 웹 조사 결과만 반환한다."""
    parts: list[str] = []
    runtime = None
    if provider == "nvidia":
        mode = deployment_mode or "build"
        exact_endpoint = canonicalize_nvidia_endpoint(mode, endpoint or "")
        binding = {"deploymentMode": mode, "endpoint": exact_endpoint}
        credential = _credential_memory.credential_for(binding)
        if mode == "build" and not credential:
            return "(브리핑 생성 실패: NVIDIA 자격 증명이 준비되지 않았습니다.)"
        runtime = create_runtime(
            "nvidia", exact_endpoint, credential=credential, deployment_mode=mode
        )
    research_stream = run_research_chat(
        host=host, model=model,
        messages=[{"role": m["role"], "content": m["content"]} for m in messages],
        context_length=context_length, keep_alive=keep_alive,
        runtime=runtime, strict_tool_protocol=provider == "nvidia",
        response_language=response_language,
    )
    research_completed = False
    terminal_failure = False
    evidence_succeeded = False
    openai_question = any(
        "openai" in str(item.get("content") or "").lower()
        for item in messages if item.get("role") == "user"
    )
    openai_official_fetched = False
    try:
        async for ev in research_stream:
            t = ev.get("type")
            if t == "content":
                parts.append(ev.get("text", ""))
            elif t == "reset_content":
                # 리서치 루프가 '스니펫만 본 임시 답'을 폐기하라는 신호 — 누적분을 버린다
                # (안 그러면 서두·폐기 초안·최종답이 뒤섞인 브리핑이 자율 전송된다).
                parts.clear()
            elif t == "tool_result" and ev.get("ok") and ev.get("name") in ("web_search", "web_fetch"):
                evidence_succeeded = True
                if ev.get("name") == "web_fetch":
                    output = str(ev.get("output") or "").lower()
                    if any(domain in output for domain in ("openai.com", "help.openai.com", "status.openai.com")):
                        openai_official_fetched = True
            elif t in ("error", "incomplete", "cancelled"):
                terminal_failure = True
        research_completed = True
    except Exception as e:  # noqa: BLE001
        return f"(브리핑 생성 실패: {e})"
    finally:
        if not research_completed:
            await research_stream.aclose()
    if terminal_failure:
        return "(브리핑 생성 실패: 선택한 모델의 조사 응답이 안전하게 완료되지 않았습니다.)"
    if not evidence_succeeded:
        return "(웹 조사 실패: 검색·원문 확인 근거를 확보하지 못해 모델의 기억으로 답하지 않았습니다.)"
    if openai_question and not openai_official_fetched:
        return "(웹 조사 실패: OpenAI 공식 원문을 확인하지 못해 비공식 자료만으로 답하지 않았습니다.)"
    return "".join(parts).strip() or "(빈 브리핑)"


class DiscordConfig(BaseModel):
    enabled: bool = False
    token: str = ""
    # 소유자·채널·허용목록은 런타임 자동 판별/동적 관리 — 여기서 받지 않는다.
    data_dir: str = ""  # 봇 동적 상태(guild·channel·allowlist) 영속 폴더
    provider: Literal["ollama", "nvidia"] = "ollama"
    deployment_mode: Literal["build", "nim"] | None = None
    endpoint: str = ""
    model: str = "gemma4:12b"
    context_length: int = 16384
    keep_alive: str = "30m"
    ollama_host: str | None = None
    nvidia_runtime_grant: str = Field(default="", max_length=256)
    # Electron main process is the registry authority. The sidecar receives a
    # snapshot only for this live bot configuration and cannot accept arbitrary
    # workflows or model paths from a Discord message/LLM.
    comfy_base_url: str = Field(default="", max_length=512)
    comfy_profiles: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    allow_attachment_images: bool = False


@app.post("/discord/config")
async def discord_config_ep(req: DiscordConfig):
    """디스코드 봇 설정을 적용(재시작/중지). Electron 메인이 앱 시작·설정변경 시 인증 호출.

    봇 토큰은 여기서 받아 discordbot 프로세스 메모리에만 둔다(env·디스크 미기록)."""
    if discordbot is None:
        raise HTTPException(status_code=503, detail="discord.py가 설치되어 있지 않습니다.")
    host = _require_local_ollama_host(req.ollama_host or DEFAULT_OLLAMA)
    exact_endpoint = ""
    if req.provider == "nvidia":
        try:
            mode = req.deployment_mode or "build"
            exact_endpoint = canonicalize_nvidia_endpoint(mode, req.endpoint)
        except ValueError as error:
            raise HTTPException(status_code=400, detail="invalid NVIDIA Discord target") from error
        if req.enabled:
            binding = {"deploymentMode": mode, "endpoint": exact_endpoint}
            _nvidia_discord_grants.consume(
                req.nvidia_runtime_grant, binding, req.model.strip()
            )
            credential = _credential_memory.credential_for(binding)
            if mode == "build" and not credential:
                raise HTTPException(status_code=409, detail="NVIDIA credential is not ready")

    async def gen(messages: list) -> str:
        return await _discord_generate(
            messages,
            model=req.model,
            context_length=req.context_length,
            keep_alive=req.keep_alive,
            host=host,
            provider=req.provider,
            deployment_mode=req.deployment_mode,
            endpoint=exact_endpoint,
        )

    async def step(messages: list, tools: list | None = None) -> dict:
        return await _discord_step(
            messages,
            model=req.model,
            context_length=req.context_length,
            keep_alive=req.keep_alive,
            host=host,
            tools=tools,
            provider=req.provider,
            deployment_mode=req.deployment_mode,
            endpoint=exact_endpoint,
        )

    async def research(messages: list, response_language: str | None = "ko") -> str:
        return await _discord_research(
            messages,
            model=req.model,
            context_length=req.context_length,
            keep_alive=req.keep_alive,
            host=host,
            provider=req.provider,
            deployment_mode=req.deployment_mode,
            endpoint=exact_endpoint,
            response_language=response_language or "ko",
        )

    async def image(args: dict[str, Any]) -> dict[str, Any]:
        if not req.comfy_base_url.strip() or not req.comfy_profiles:
            raise GenerationError(
                "Discord 이미지 생성에는 ComfyUI 연결과 준비된 Agent 모델이 필요합니다.", kind="input"
            )

        def text_arg(name: str, maximum: int) -> str:
            value = args.get(name, "")
            if value is None:
                return ""
            if not isinstance(value, str):
                raise GenerationError(f"{name} 입력 형식이 올바르지 않습니다.", kind="input")
            if len(value) > maximum:
                raise GenerationError(f"{name} 입력이 너무 깁니다.", kind="input")
            return value

        def integer_arg(name: str) -> int | None:
            value = args.get(name)
            if value is None:
                return None
            if not isinstance(value, int) or isinstance(value, bool):
                raise GenerationError(f"{name} 입력 형식이 올바르지 않습니다.", kind="input")
            return value

        seed = args.get("seed")
        if seed is not None and not isinstance(seed, (str, int)):
            raise GenerationError("seed 입력 형식이 올바르지 않습니다.", kind="input")
        result = await generate_comfy_image(
            base_url=req.comfy_base_url,
            profiles=req.comfy_profiles,
            prompt=text_arg("prompt", 4_000),
            negative_prompt=text_arg("negative_prompt", 4_000),
            model_hint=text_arg("model_hint", 120),
            # Discord does not expose a raw profile ID control. Only the same
            # Agent-enabled candidates that desktop automatic mode may use are
            # eligible, then the registry workflow/asset checks run again.
            selected_profile_id=None,
            selection_context="Discord에서 사용자가 요청한 이미지 생성",
            width=integer_arg("width"),
            height=integer_arg("height"),
            seed=seed,
        )
        image_result = result.get("image")
        if not isinstance(image_result, dict):
            raise GenerationError("이미지 생성 결과 형식이 올바르지 않습니다.")
        try:
            data, media_type = await fetch_comfy_output_image(
                str(image_result["baseUrl"]),
                str(image_result["filename"]),
                str(image_result.get("subfolder") or ""),
                str(image_result["storageType"]),
            )
        except (KeyError, TypeError, ComfyAPIError) as error:
            raise GenerationError("ComfyUI 생성 이미지를 가져오지 못했습니다.") from error
        return {
            "summary": str(result.get("summary") or "이미지를 생성했습니다."),
            "data": data,
            "filename": str(image_result["filename"]),
            "content_type": media_type,
        }

    # The one-use bearer authorizes this configuration only and is never kept
    # in the long-lived Discord bot state.
    image_ready = bool(
        req.comfy_base_url.strip()
        and any(bool(profile.get("agentEnabled")) for profile in req.comfy_profiles)
    )
    await discordbot.apply_config(
        req.model_dump(exclude={"nvidia_runtime_grant"}),
        gen,
        step,
        research,
        image if image_ready else None,
    )
    return {"ok": True, **discordbot.status()}


@app.get("/discord/status")
async def discord_status_ep():
    if discordbot is None:
        return {"running": False, "user": None, "last_error": "discord.py 미설치"}
    return discordbot.status()


@app.get("/discord/schedules")
async def discord_schedules_ep():
    """등록된 예약 목록 — 설정 탭이 조회. 봇 미연결이어도 저장소만 있으면 동작."""
    try:
        import discordsched
        return {"jobs": discordsched.jobs()}
    except Exception as e:  # noqa: BLE001
        return {"jobs": [], "detail": str(e)}


class ScheduleRemoveRequest(BaseModel):
    id: str


@app.post("/discord/schedules/remove")
async def discord_schedule_remove_ep(req: ScheduleRemoveRequest):
    # 조회 엔드포인트와 동일하게 실패를 우아하게 반환한다(인접 API의 실패 계약 통일).
    try:
        import discordsched
        return {"ok": discordsched.remove(req.id)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": str(e)}


# ---- 긴급 GPU 확보: 로드된 Ollama 모델을 즉시 언로드 ----
class UnloadRequest(BaseModel):
    ollama_host: str | None = None


@app.post("/ollama/unload")
async def ollama_unload(req: UnloadRequest):
    """로드된 Ollama 모델을 즉시 VRAM에서 내린다(긴급 GPU 확보).

    LLM runtime이 현재 적재 모델을 찾아 best-effort로 해제한다.
    생성 중인 모델은 그 요청이 끝난 뒤 내려간다(Ollama가 직렬 처리)."""
    host = _require_local_ollama_host(req.ollama_host or DEFAULT_OLLAMA)
    try:
        unloaded = await create_runtime("ollama", host).release_accelerator_memory()
        return {"ok": True, "unloaded": unloaded}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": str(e), "unloaded": []}


class AgentRequest(BaseModel):
    messages: list[ChatMessage]
    provider: Literal["ollama", "nvidia"] = "ollama"
    workspace: str
    model: str = "gemma4:12b"
    reasoning_effort: str = Field(default="medium", pattern="^(low|medium|high)$")
    temperature: float = 0.7
    context_length: int = 16384  # /chat와 통일 — 모드 전환 시 num_ctx 변경으로 인한 재로드 방지
    approval_mode: str = Field(default="read", pattern="^(manual|read|auto)$")
    session_id: str = ""
    # Client-side conversation correlation only. It never writes to My DB,
    # changes tool scope, or grants file access.
    conversation_id: str = Field(default="", max_length=160)
    assistant_turn_id: str = ""
    # Renderer-only provenance bit: true solely after a persisted image_result
    # card exists in this conversation.  It never expands tool permissions.
    image_context_verified: bool = False
    nvidia_grant: str = ""
    deployment_mode: Literal["build", "nim"] | None = None
    endpoint: str = Field(default="", max_length=2048)
    ollama_host: str | None = None
    rag_enabled: bool = True
    rag_top_k: int = 5
    keep_alive: str = "30m"
    comfy_base_url: str | None = None
    comfy_profiles: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    # Manual selection is a profile ID, not a free-form model name.  The agent
    # validates it again against the registered profile data before execution.
    comfy_selection_mode: Literal["auto", "manual"] = "auto"
    selected_comfy_model_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    # Ollama 실행 정책은 Renderer가 저장 설정의 provider별 스냅샷을 전달한다.
    # NVIDIA 실행은 이 값을 무시하고 Main-issued exact scope의 allowedTools만 신뢰한다.
    enabled_tools: list[str] | None = Field(default=None, max_length=len(BUILTIN_TOOL_NAMES))


class ApprovalRequest(BaseModel):
    session_id: str
    call_id: str
    approved: bool


@app.post("/agent")
async def agent(req: AgentRequest):
    """에이전트 하네스 — 파일 툴 콜링 루프. NDJSON 이벤트 스트림."""
    response_language = _request_response_language(req.messages)
    raw_user_request = _latest_original_user_text(req.messages)
    try:
        prepared_messages = _messages_with_latest_attachments(
            req.messages, allow_images=req.provider == "ollama"
        )
    except AttachmentError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    runtime: LlmRuntime | None = None
    ledger: AgentExecutionLedger | None = None
    nvidia_execution_scope: dict[str, Any] | None = None
    local_enabled_tools: list[str] | None = None
    if req.provider == "nvidia":
        if not req.deployment_mode or not req.endpoint or not req.model.strip():
            raise HTTPException(status_code=400, detail="NVIDIA Agent target is incomplete")
        if len(req.session_id) < 16 or len(req.assistant_turn_id) < 16 or not req.nvidia_grant:
            raise HTTPException(status_code=400, detail="NVIDIA Agent execution scope is invalid")
        binding = _credential_binding(req.deployment_mode, req.endpoint)
        nvidia_execution_scope = _nvidia_agent_grants.consume(
            req.nvidia_grant,
            session_id=req.session_id,
            assistant_turn_id=req.assistant_turn_id,
            binding=binding,
            model=req.model.strip(),
        )
        if _agent_ledger_startup_error or not AGENT_LEDGER_PATH:
            raise HTTPException(status_code=503, detail="NVIDIA Agent execution ledger is unavailable")
        try:
            ledger = AgentExecutionLedger(AGENT_LEDGER_PATH)
        except LedgerError as error:
            raise HTTPException(status_code=503, detail="NVIDIA Agent execution ledger is unavailable") from error
        runtime = _nvidia_runtime_for_target(
            NvidiaTargetRequest(deployment_mode=req.deployment_mode, endpoint=binding["endpoint"])
        )
    else:
        try:
            local_enabled_tools = list(normalize_enabled_tool_names(req.enabled_tools))
        except ToolError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    host = (
        _require_local_ollama_host(str(nvidia_execution_scope.get("ollamaHost") or DEFAULT_OLLAMA))
        if nvidia_execution_scope is not None and nvidia_execution_scope.get("ragEnabled")
        else (
            DEFAULT_OLLAMA.rstrip("/")
            if nvidia_execution_scope is not None
            else (req.ollama_host or DEFAULT_OLLAMA).rstrip("/")
        )
    )
    authoritative_workspace = (
        str(nvidia_execution_scope.get("workspace") or "")
        if nvidia_execution_scope is not None
        else req.workspace
    )
    authoritative_rag = (
        bool(nvidia_execution_scope.get("ragEnabled"))
        if nvidia_execution_scope is not None
        else req.rag_enabled and "search_docs" in (local_enabled_tools or [])
    )
    authoritative_rag_top_k = (
        int(nvidia_execution_scope.get("ragTopK") or 0)
        if nvidia_execution_scope is not None else req.rag_top_k
    )
    authoritative_comfy = (
        nvidia_execution_scope.get("comfy")
        if nvidia_execution_scope is not None
        else None
    )
    authoritative_approval_mode = (
        str(nvidia_execution_scope.get("approvalMode"))
        if nvidia_execution_scope is not None else req.approval_mode
    )
    global _preview_root
    _preview_root = None
    try:  # 에이전트가 만드는 파일을 우측 미리보기에서 바로 볼 수 있게 루트 설정
        _preview_root = validate_workspace(authoritative_workspace)
    except ToolError:
        pass

    async def gen():
        agent_stream = run_agent(
            host=host,
            workspace=authoritative_workspace,
            model=req.model,
            messages=prepared_messages,
            reasoning_effort=req.reasoning_effort,
            temperature=req.temperature,
            context_length=req.context_length,
            approval_mode=authoritative_approval_mode,
            session_id=req.session_id,
            conversation_id=req.conversation_id,
            rag_enabled=authoritative_rag,
            rag_top_k=authoritative_rag_top_k,
            keep_alive=req.keep_alive,
            comfy_base_url=(
                str(authoritative_comfy.get("baseUrl") or "")
                if isinstance(authoritative_comfy, dict) and authoritative_comfy.get("enabled")
                else (None if req.provider == "nvidia" else req.comfy_base_url)
            ),
            comfy_profiles=(
                list(authoritative_comfy.get("profiles") or [])
                if isinstance(authoritative_comfy, dict) and authoritative_comfy.get("enabled")
                else ([] if req.provider == "nvidia" else req.comfy_profiles)
            ),
            comfy_selection_mode=(
                str(authoritative_comfy.get("selectionMode") or "auto")
                if isinstance(authoritative_comfy, dict) else req.comfy_selection_mode
            ),
            selected_comfy_model_id=(
                authoritative_comfy.get("selectedProfileId")
                if isinstance(authoritative_comfy, dict) else req.selected_comfy_model_id
            ),
            provider=req.provider,
            runtime=runtime,
            assistant_turn_id=req.assistant_turn_id,
            execution_ledger=ledger,
            nvidia_allowed_tools=(
                list(nvidia_execution_scope.get("allowedTools") or [])
                if nvidia_execution_scope is not None else None
            ),
            enabled_tools=local_enabled_tools,
            user_request_text=raw_user_request,
            image_context_verified=req.image_context_verified,
            response_language=response_language,
        )
        agent_completed = False
        try:
            async for ev in agent_stream:
                yield json.dumps(ev, ensure_ascii=False) + "\n"
            agent_completed = True
        finally:
            if not agent_completed:
                await agent_stream.aclose()
            if ledger is not None:
                ledger.close()

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/agent/tools")
async def agent_tools():
    """설정 화면용 내장 Agent 툴 카탈로그.

    사용자 스킬은 런타임 확장이라 포함하지 않고, 실제 레지스트리와 조건부 이미지 생성
    도구만 읽기 전용으로 반환한다. 다른 Agent 경로와 동일하게 세션 토큰 인증이 적용된다.
    """
    return {"tools": get_builtin_tool_catalog()}


# ---- Deterministic scenario QA ----


class DocumentTodoPatchRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=110)
    status: Literal["open", "done"] | None = None
    priority: Literal["high", "medium", "low"] | None = None
    dueDate: str | None = Field(default=None, max_length=32)
    dueTime: str | None = Field(default=None, max_length=8, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    endTime: str | None = Field(default=None, max_length=8, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    startDate: str | None = Field(default=None, max_length=32)
    endDate: str | None = Field(default=None, max_length=32)
    estimatedMinutes: int | None = Field(default=None, ge=5, le=1440)
    recurrence: dict[str, Any] | None = None


class DocumentTodoCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=110)
    priority: Literal["high", "medium", "low"] = "medium"
    startDate: str | None = Field(default=None, max_length=32)
    endDate: str | None = Field(default=None, max_length=32)
    dueTime: str | None = Field(default=None, max_length=8, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    endTime: str | None = Field(default=None, max_length=8, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    estimatedMinutes: int | None = Field(default=30, ge=5, le=1440)
    recurrence: dict[str, Any] | None = None


class DocumentTodoReplanRequest(BaseModel):
    """A reviewable recovery-plan request, never an autonomous write."""

    asOf: str | None = Field(default=None, max_length=32)


@app.get("/creator/todos")
async def creator_todos_list(workspace: str | None = None):
    """Read saved ToDos globally, or one workspace when explicitly requested."""
    try:
        result = list_document_todos(workspace) if workspace and workspace.strip() else list_saved_document_todos()
        return {"ok": True, **result}
    except ToolError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None


@app.post("/creator/todos")
async def creator_todos_create(req: DocumentTodoCreateRequest):
    """Create one Aiso-owned planner item without requiring a workspace."""
    try:
        return {
            "ok": True,
            "item": create_document_todo_item(
                title=req.title,
                priority=req.priority,
                start_date=req.startDate,
                end_date=req.endDate,
                start_time=req.dueTime,
                end_time=req.endTime,
                estimated_minutes=req.estimatedMinutes,
                recurrence=req.recurrence,
            ),
        }
    except ToolError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None


@app.patch("/creator/todos/{item_id}")
async def creator_todos_patch(item_id: str, req: DocumentTodoPatchRequest):
    try:
        # Explicit null clears an optional date/time/recurrence field.  Omitting
        # the field leaves it unchanged.
        patch = req.model_dump(exclude_unset=True)
        return {"ok": True, "item": update_document_todo(item_id, patch)}
    except ToolError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None


@app.delete("/creator/todos/{item_id}")
async def creator_todos_delete(item_id: str):
    try:
        delete_document_todo(item_id)
        return {"ok": True, "id": item_id}
    except ToolError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None


@app.post("/creator/todos/replan-preview")
async def creator_todos_replan_preview(req: DocumentTodoReplanRequest):
    """Calculate a missed-work recovery proposal without modifying ToDos."""
    try:
        return {"ok": True, **preview_document_todo_reschedule(req.asOf)}
    except ToolError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None


@app.post("/creator/todos/replan-apply")
async def creator_todos_replan_apply(req: DocumentTodoReplanRequest):
    """Apply the current proposal only after an explicit UI confirmation."""
    try:
        return {"ok": True, **apply_document_todo_reschedule(req.asOf)}
    except ToolError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None


@app.post("/qa/scenarios/run")
async def qa_scenarios_run():
    """Run local contracts without consuming a configured LLM's token budget."""
    return run_scenario_pack()


# ---- RAG: 작업 폴더 의미 색인/검색 (임베딩 모델 = 채팅 모델과 독립) ----


class RagIndexRequest(BaseModel):
    workspace: str
    embed_model: str = "nomic-embed-text"
    ollama_host: str | None = None
    max_files: int | None = None  # 색인할 최대 파일 수(RAG 범위). None이면 저장값/기본값


@app.post("/rag/index")
async def rag_index(req: RagIndexRequest):
    """작업 폴더를 색인한다. 진행 상황을 NDJSON으로 스트리밍."""
    host = _require_local_ollama_host(req.ollama_host or DEFAULT_OLLAMA)

    async def gen():
        try:
            root = validate_workspace(req.workspace)
        except ToolError as e:
            yield json.dumps({"type": "error", "error": str(e)}, ensure_ascii=False) + "\n"
            return
        try:
            async for ev in build_index(root, host, req.embed_model, req.max_files):
                yield json.dumps(ev, ensure_ascii=False) + "\n"
        except RagError as e:
            yield json.dumps({"type": "error", "error": str(e)}, ensure_ascii=False) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/rag/status")
async def rag_status_ep(workspace: str):
    """작업 폴더의 색인 상태."""
    try:
        root = validate_workspace(workspace)
    except ToolError as e:
        return {"indexed": False, "count": 0, "detail": str(e)}
    return rag_status(root)


# ---- Ollama 모델 다운로드(pull) — 처음 설치 온보딩에서 임베딩 모델 원클릭 설치 ----


class OllamaPullRequest(BaseModel):
    model: str
    ollama_host: str | None = None


@app.post("/ollama/pull")
async def ollama_pull(req: OllamaPullRequest):
    """Ollama 모델을 내려받으며 진행 상황을 NDJSON으로 스트리밍한다.

    Ollama /api/pull 은 레이어별로 {status, digest, total, completed} 를 흘리고
    마지막에 {status:"success"} 를 준다. 이를 그대로 progress/done/error 로 중계한다.
    """
    host = (req.ollama_host or DEFAULT_OLLAMA).rstrip("/")
    model = req.model.strip()

    async def gen():
        if not model:
            yield json.dumps({"type": "error", "error": "모델명이 비어 있습니다."}, ensure_ascii=False) + "\n"
            return
        timeout = httpx.Timeout(None, connect=5)  # 다운로드는 무제한, 연결만 5s
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                # name(구버전)·model(신버전) 둘 다 실어 호환성 확보
                payload = {"name": model, "model": model, "stream": True}
                async with client.stream("POST", f"{host}/api/pull", json=payload) as r:
                    if r.status_code != 200:
                        body = (await r.aread()).decode(errors="ignore")
                        yield json.dumps(
                            {"type": "error", "error": f"Ollama 오류 ({r.status_code}): {body[:300]}"},
                            ensure_ascii=False,
                        ) + "\n"
                        return
                    async for line in r.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        data = json.loads(line)
                        if data.get("error"):
                            yield json.dumps(
                                {"type": "error", "error": str(data["error"])[:300]}, ensure_ascii=False
                            ) + "\n"
                            return
                        status = data.get("status", "")
                        ev: dict = {"type": "progress", "status": status}
                        if isinstance(data.get("total"), int):
                            ev["total"] = data["total"]
                        if isinstance(data.get("completed"), int):
                            ev["completed"] = data["completed"]
                        yield json.dumps(ev, ensure_ascii=False) + "\n"
                        if status == "success":
                            yield json.dumps({"type": "done", "model": model}, ensure_ascii=False) + "\n"
                            return
            # success 없이 스트림이 끝난 경우도 완료로 간주
            yield json.dumps({"type": "done", "model": model}, ensure_ascii=False) + "\n"
        except Exception as e:  # noqa: BLE001 — 연결 끊김 등은 오류로 중계
            yield json.dumps({"type": "error", "error": f"연결 실패: {e}"}, ensure_ascii=False) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


class RagSearchRequest(BaseModel):
    workspace: str
    query: str
    k: int = 5
    ollama_host: str | None = None


@app.post("/rag/search")
async def rag_search_ep(req: RagSearchRequest):
    """의미 검색 (진단·수동 검색용)."""
    host = _require_local_ollama_host(req.ollama_host or DEFAULT_OLLAMA)
    try:
        root = validate_workspace(req.workspace)
    except ToolError as e:
        return {"ok": False, "detail": str(e), "results": []}
    try:
        results = await rag_search(root, host, req.query, req.k)
        return {"ok": True, "results": results}
    except RagError as e:
        return {"ok": False, "detail": str(e), "results": []}


class VerifyRequest(BaseModel):
    workspace: str
    path: str
    actions: list[dict] | None = None
    steps: list[dict] | None = None


@app.post("/verify")
async def verify(req: VerifyRequest):
    """단일 HTML 파일을 헤드리스 실행해 검증 (수동 검증 / 진단용)."""
    try:
        root = validate_workspace(req.workspace)
        report, shot = await run_web(root, req.path, actions=req.actions, steps=req.steps)
        status_match = re.search(r"(?m)^status=(PASS|FAIL|INCONCLUSIVE)\b", report)
        status = status_match.group(1) if status_match is not None else "INCONCLUSIVE"
        return {"ok": status == "PASS", "status": status, "report": report, "screenshot": shot}
    except ToolError as e:
        return {"ok": False, "status": "FAIL", "report": f"[오류] {e}", "screenshot": None}


@app.post("/agent/approve")
async def agent_approve(req: ApprovalRequest):
    """파괴적 툴 승인/거부 — 대기 중인 에이전트 루프를 깨운다."""
    key = f"{req.session_id}:{req.call_id}"
    ok = resolve_approval(key, req.approved)
    return {"ok": ok}
