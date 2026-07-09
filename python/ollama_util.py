"""Ollama 통신 공통 유틸 — VRAM 부족(CUDA crash) 자동 복원 + think 폴백.

gpt-oss:20b(13.8GB)는 16GB GPU에서 다른 앱이 VRAM을 점유하면 콜드 로드 시
llama-server가 crash한다. 이때 num_gpu로 일부 레이어를 CPU에 오프로드해 재시도한다.
"""

from __future__ import annotations

import httpx

# 모델 적재 실패(주로 VRAM 부족/CUDA crash) 신호
_CRASH_SIGNS = (
    "cuda error",
    "0xc0000409",
    "terminated",
    "shared object",
    "out of memory",
    "failed to allocate",
    "cudamalloc",
    "unable to allocate",
)


class OllamaHTTPError(Exception):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body[:200]}")


def is_load_crash(body: str) -> bool:
    b = body.lower()
    return any(s in b for s in _CRASH_SIGNS)


def is_think_unsupported(body: str) -> bool:
    return "think" in body.lower()


def is_tool_parse_error(body: str) -> bool:
    """gpt-oss 등에서 모델 출력을 툴 호출로 파싱하다 실패한 오류(재생성으로 회복 가능).

    예: {"error":"error parsing tool call: raw='We need to respond: ...'"}
    """
    b = body.lower()
    return "parsing tool" in b or ("tool call" in b and ("error" in b or "parse" in b))


_layer_cache: dict[str, int] = {}


async def model_layers(host: str, model: str) -> int | None:
    """모델 레이어(block) 수 — num_gpu 부분 오프로드 계산용. 실패하면 None."""
    if model in _layer_cache:
        return _layer_cache[model]
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.post(f"{host}/api/show", json={"model": model})
            r.raise_for_status()
            info = r.json().get("model_info") or {}
            for k, v in info.items():
                if k.endswith(".block_count"):
                    _layer_cache[model] = int(v)
                    return int(v)
    except Exception:  # noqa: BLE001 — 조회 실패해도 고정 폴백값으로 진행
        pass
    return None


def build_attempts(base: dict, think_value: str, layers: int | None) -> list[dict]:
    """폴백 사다리 (앞에서부터 시도):
    전체GPU(+think) → 전체GPU → 부분오프로드高(+think) → 부분오프로드高
    → 부분오프로드低(+think) → 부분오프로드低.
    앞쪽은 빠르고, 뒤로 갈수록 VRAM을 덜 쓰지만 느려진다.
    """
    if layers and layers > 6:
        gpu_levels: list[int | None] = [None, max(1, int(layers * 0.55)), max(1, int(layers * 0.30))]
    else:
        gpu_levels = [None, 20, 10]

    attempts: list[dict] = []
    for gpu in gpu_levels:
        opts = dict(base.get("options") or {})
        if gpu is not None:
            opts["num_gpu"] = gpu
        payload = {**base, "options": opts}
        if think_value:
            attempts.append({**payload, "think": think_value})
        attempts.append(payload)
    return attempts
