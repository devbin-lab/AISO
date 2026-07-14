"""Aiso 백엔드 사이드카 — Ollama 통신 계층 (FastAPI)

Electron 메인 프로세스가 앱 시작 시 이 서버를 스폰한다:
  python -m uvicorn main:app --host 127.0.0.1 --port <동적포트>
"""

import hmac
import json
import os
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from agent import resolve_approval, run_agent, run_research_chat
from rag import RagError, build_index
from rag import search as rag_search
from rag import status as rag_status
from tools import ToolError, validate_workspace
from ollama_util import (
    OllamaHTTPError,
    build_attempts,
    is_load_crash,
    is_think_unsupported,
    model_layers,
)

DEFAULT_OLLAMA = os.environ.get("AISO_OLLAMA_HOST", "http://localhost:11434")

# 세션 인증 토큰 — Electron 메인이 사이드카 스폰 시 환경변수로 넘긴다.
# 백엔드는 127.0.0.1의 동적 포트에 붙지만 그것만으론 인증이 없어, 앱 실행 중
# 악성 웹페이지가 포트를 스캔해 cross-origin으로 /agent(파일 툴!)·/chat을 호출할 수 있다.
# 렌더러만 아는 이 토큰을 X-Aiso-Token 헤더로 검사해 근본적으로 잠근다.
AUTH_TOKEN = os.environ.get("AISO_AUTH_TOKEN", "")

app = FastAPI(title="aiso-backend")

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
        if scope["type"] != "http" or not self.token:
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "")
        path = scope.get("path", "")
        if method == "OPTIONS" or path.startswith(_AUTH_EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
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


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
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


# ---- 라이브 미리보기: 작업 폴더를 정적 서빙 (우측 패널 iframe이 이걸 띄운다) ----
_preview_root: Path | None = None


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
    return FileResponse(str(target))


@app.get("/health")
async def health(host: str | None = None):
    """백엔드 생존 + Ollama 도달성/모델 목록."""
    target = (host or DEFAULT_OLLAMA).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get(f"{target}/api/tags")
            r.raise_for_status()
            models = [m.get("name", "") for m in r.json().get("models", [])]
        return {"status": "ok", "ollama": True, "models": models}
    except Exception as e:  # noqa: BLE001 — 도달 실패 사유를 그대로 전달
        return {"status": "ok", "ollama": False, "models": [], "detail": str(e)[:200]}


async def _stream_ollama(host: str, payload: dict):
    """Ollama /api/chat 스트림을 순회하며 청크 dict를 낸다."""
    timeout = httpx.Timeout(None, connect=5)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", f"{host}/api/chat", json=payload) as r:
            if r.status_code != 200:
                body = (await r.aread()).decode(errors="ignore")
                raise OllamaHTTPError(r.status_code, body)
            async for line in r.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                msg = data.get("message") or {}
                # thinking(사고과정)과 content(최종답변)는 분리되어 온다 (gemma4·gpt-oss 등 think 지원 모델)
                thinking = msg.get("thinking")
                if thinking:
                    yield {"type": "thinking", "text": thinking}
                content = msg.get("content")
                if content:
                    yield {"type": "content", "text": content}
                if data.get("done"):
                    if data.get("done_reason") == "length":
                        yield {
                            "type": "notice",
                            "text": "⚠ 컨텍스트 한도 도달 — 응답이 잘렸습니다. 설정에서 컨텍스트 길이를 늘려보세요.",
                        }
                    yield {
                        "type": "done",
                        "eval_count": data.get("eval_count"),  # 생성 토큰 (출력만 집계)
                        "total_duration": data.get("total_duration"),
                    }


@app.post("/chat")
async def chat(req: ChatRequest):
    """NDJSON 스트리밍 채팅. think(추론 강도)는 지원 모델(gemma4·gpt-oss 등)에 적용된다."""
    host = (req.ollama_host or DEFAULT_OLLAMA).rstrip("/")
    base: dict = {
        "model": req.model,
        "messages": [{"role": m.role, "content": m.content} for m in req.messages],
        "stream": True,
        "keep_alive": req.keep_alive,  # 모델 상주 → 콜드 재로드 방지
        "options": {
            "temperature": req.temperature,
            "num_ctx": req.context_length,
        },
    }

    async def gen():
        # 웹 검색 켜짐 → 조사 루프(web_search·web_fetch)로 위임. NDJSON 이벤트는 동일 계약.
        if req.research:
            async for ev in run_research_chat(
                host=host,
                model=req.model,
                messages=[{"role": m.role, "content": m.content} for m in req.messages],
                reasoning_effort=req.reasoning_effort,
                temperature=req.temperature,
                context_length=req.context_length,
                keep_alive=req.keep_alive,
            ):
                yield json.dumps(ev, ensure_ascii=False) + "\n"
            return

        layers = await model_layers(host, req.model)
        attempts = build_attempts(base, req.reasoning_effort, layers)
        noticed = False
        for i, payload in enumerate(attempts):
            try:
                async for chunk in _stream_ollama(host, payload):
                    yield json.dumps(chunk, ensure_ascii=False) + "\n"
                return
            except OllamaHTTPError as e:
                last = i == len(attempts) - 1
                crash = is_load_crash(e.body)
                if not last and (crash or is_think_unsupported(e.body)):
                    if crash and not noticed:
                        noticed = True
                        print("[ollama] 적재 실패(VRAM 부족 추정) → CPU 오프로드로 재시도")
                        yield json.dumps(
                            {"type": "notice", "text": "VRAM 부족 — CPU 오프로드로 실행합니다 (느려질 수 있어요)"},
                            ensure_ascii=False,
                        ) + "\n"
                    continue
                yield json.dumps(
                    {"type": "error", "error": f"Ollama 오류 ({e.status}): {e.body[:300]}"},
                    ensure_ascii=False,
                ) + "\n"
                return
            except Exception as e:  # noqa: BLE001
                yield json.dumps(
                    {"type": "error", "error": f"연결 실패: {e}"}, ensure_ascii=False
                ) + "\n"
                return

    return StreamingResponse(gen(), media_type="application/x-ndjson")


class AgentRequest(BaseModel):
    messages: list[ChatMessage]
    workspace: str
    model: str = "gemma4:12b"
    reasoning_effort: str = Field(default="medium", pattern="^(low|medium|high)$")
    temperature: float = 0.7
    context_length: int = 16384  # /chat와 통일 — 모드 전환 시 num_ctx 변경으로 인한 재로드 방지
    approval_mode: str = Field(default="read", pattern="^(manual|read|auto)$")
    session_id: str = ""
    ollama_host: str | None = None
    rag_enabled: bool = True
    rag_top_k: int = 5
    keep_alive: str = "30m"


class ApprovalRequest(BaseModel):
    session_id: str
    call_id: str
    approved: bool


@app.post("/agent")
async def agent(req: AgentRequest):
    """에이전트 하네스 — 파일 툴 콜링 루프. NDJSON 이벤트 스트림."""
    host = (req.ollama_host or DEFAULT_OLLAMA).rstrip("/")
    global _preview_root
    try:  # 에이전트가 만드는 파일을 우측 미리보기에서 바로 볼 수 있게 루트 설정
        _preview_root = validate_workspace(req.workspace)
    except ToolError:
        pass

    async def gen():
        async for ev in run_agent(
            host=host,
            workspace=req.workspace,
            model=req.model,
            messages=[{"role": m.role, "content": m.content} for m in req.messages],
            reasoning_effort=req.reasoning_effort,
            temperature=req.temperature,
            context_length=req.context_length,
            approval_mode=req.approval_mode,
            session_id=req.session_id,
            rag_enabled=req.rag_enabled,
            rag_top_k=req.rag_top_k,
            keep_alive=req.keep_alive,
        ):
            yield json.dumps(ev, ensure_ascii=False) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


# ---- RAG: 작업 폴더 의미 색인/검색 (임베딩 모델 = 채팅 모델과 독립) ----


class RagIndexRequest(BaseModel):
    workspace: str
    embed_model: str = "nomic-embed-text"
    ollama_host: str | None = None
    max_files: int | None = None  # 색인할 최대 파일 수(RAG 범위). None이면 저장값/기본값


@app.post("/rag/index")
async def rag_index(req: RagIndexRequest):
    """작업 폴더를 색인한다. 진행 상황을 NDJSON으로 스트리밍."""
    host = (req.ollama_host or DEFAULT_OLLAMA).rstrip("/")

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
    host = (req.ollama_host or DEFAULT_OLLAMA).rstrip("/")
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


@app.post("/verify")
async def verify(req: VerifyRequest):
    """단일 HTML 파일을 헤드리스 실행해 검증 (수동 검증 / 진단용)."""
    from tools import ToolError, validate_workspace
    from webcheck import run_web

    try:
        root = validate_workspace(req.workspace)
        report, shot = await run_web(root, req.path)
        return {"ok": True, "report": report, "screenshot": shot}
    except ToolError as e:
        return {"ok": False, "report": f"[오류] {e}", "screenshot": None}


@app.post("/agent/approve")
async def agent_approve(req: ApprovalRequest):
    """파괴적 툴 승인/거부 — 대기 중인 에이전트 루프를 깨운다."""
    key = f"{req.session_id}:{req.call_id}"
    ok = resolve_approval(key, req.approved)
    return {"ok": ok}
