"""에이전트 하네스 — 로컬 LLM(gemma4·gpt-oss 등) 툴 콜링으로 로컬 파일을 다루는 반복 루프.

생성 → 툴콜 → (승인) → 실행 → 결과 피드백 → 반복.
이벤트를 dict로 yield 하며, 파괴적 툴은 승인 레지스트리로 사용자 확인을 기다린다.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncGenerator

import httpx

from ollama_util import (
    OllamaHTTPError,
    build_attempts,
    is_load_crash,
    is_think_unsupported,
    is_tool_parse_error,
    model_layers,
)
from rag import (
    SEARCH_DOCS_SCHEMA,
    RagError,
    build_index,
    format_context,
    search as rag_search,
    status as rag_status,
)
from toolspec import AGENT_TOOLS, REGISTRY, execute, is_meta, needs_approval
from tools import ToolError, run_tool, validate_workspace

# 대량 작업(수십~수백 파일 정리 등)도 끝까지 돌 수 있게 상한을 높게 둔다.
# 이건 '정상 작업 제한'이 아니라 병리적 폭주를 막는 최후의 안전선일 뿐이고,
# 진짜 무한 루프는 아래 STALL_REPEAT(동일 동작 반복) 감지로 막는다.
MAX_STEPS = 300
STALL_REPEAT = 6  # 완전히 동일한 (툴,인자) 호출이 연속 이 횟수를 넘으면 정체로 보고 중단
MAX_NUDGES = 3    # 툴 없이 멈추려 할 때 '이어서 진행하라'고 찌를 최대 연속 횟수
SPIN_LIMIT = 4    # 실질 진전(update_plan 외 툴 실행) 없는 턴이 연속 이 횟수면 정체로 보고 중단
MAX_PARSE_RETRIES = 2  # gpt-oss 툴콜 파싱 500 오류 시 재생성 최대 횟수 (재생성으로 대개 회복)
APPROVAL_TIMEOUT = 600  # 파괴적 툴 승인 대기 상한(초)

# AGENT_TOOLS·needs_approval·툴 실행은 toolspec 레지스트리에서 온다 (import 참고).
# 스키마 정의·분류·디스패치가 흩어져 있던 것을 한 곳으로 모았다.

_STATUS_WORDS = {"pending", "in_progress", "completed", "not_started", "todo", "done", "doing"}


def _norm_status(raw: Any) -> str:
    """모델마다 제각각인 상태 문자열을 3가지로 정규화."""
    s = str(raw or "").lower().replace("-", "_").replace(" ", "_")
    if s in ("completed", "complete", "done", "finished", "closed", "resolved"):
        return "completed"
    if s in ("in_progress", "inprogress", "doing", "active", "started", "current", "wip", "running"):
        return "in_progress"
    return "pending"  # not_started, todo, pending, 등


def _step_text(s: dict) -> str:
    """단계 텍스트를 여러 키 후보에서 찾는다 (모델이 content 대신 name/task 등을 쓰기 때문)."""
    for k in ("content", "name", "task", "title", "step", "description", "text", "label", "todo"):
        v = s.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # 그래도 없으면: 상태값이 아닌 첫 문자열
    for v in s.values():
        if isinstance(v, str) and v.strip() and v.strip().lower() not in _STATUS_WORDS:
            return v.strip()
    return "(단계)"


def normalize_plan(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        return []
    return [
        {"content": _step_text(s), "status": _norm_status(s.get("status"))}
        for s in raw
        if isinstance(s, dict)
    ]


def render_plan(plan: list[dict]) -> str:
    """현재 계획을 시스템 메시지에 끼워넣을 텍스트로 렌더링한다 (항상 컨텍스트에 유지)."""
    if not plan:
        return ""
    mark = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]"}
    lines = "\n".join(f"{mark.get(s.get('status'), '[ ]')} {s.get('content', '')}" for s in plan)
    return (
        "\n\n[현재 작업 계획]\n" + lines +
        "\n각 단계를 시작할 때 in_progress, 끝내면 completed로 update_plan을 호출해 갱신하라."
    )


def compact_convo(convo: list[dict], context_length: int, reserve_tokens: int = 0) -> list[dict]:
    """대화가 너무 길어지면 오래된 tool 결과를 축약한다 (최근 결과는 유지).

    토크나이저 없이 문자 수로 추정하되, num_ctx에서 고정 오버헤드(시스템+툴)와 응답 여유를
    뺀 만큼을 대화 예산으로 삼아 컨텍스트 오버플로(done_reason=length)를 예방한다.
    """
    # 남은 창(토큰) = num_ctx − (시스템+툴 오버헤드) − 응답 여유(1024). 문자≈토큰×3(한글 혼합 보수).
    avail_tokens = max(1500, context_length - reserve_tokens - 1024)
    budget = avail_tokens * 3
    total = sum(
        len(str(m.get("content") or "")) + len(json.dumps(m.get("tool_calls") or "", ensure_ascii=False))
        for m in convo
    )
    if total <= budget:
        return convo
    keep_full_after = len(convo) - 6  # 최근 6개 메시지는 원본 유지
    out = []
    for i, m in enumerate(convo):
        c = str(m.get("content") or "")
        if m.get("role") == "tool" and i < keep_full_after and len(c) > 200:
            out.append({**m, "content": c[:160] + " …(오래된 결과 축약)"})
        else:
            out.append(m)
    return out


SYSTEM_PROMPT = """너는 Aiso의 로컬 코딩 에이전트다. 사용자의 작업 폴더 안에서만 파일을 읽고 쓸 수 있다.
- **여러 단계가 필요한 작업이면 먼저 update_plan으로 할 일을 3~6단계로 나눠라.** 각 단계를
  시작할 때 in_progress, 끝내면 completed로 갱신한다. update_plan을 호출하면 결과로 현재 계획 전체가
  표시되니 그걸 근거로 삼되, 계획 텍스트를 사용자 답변에 그대로 옮겨 적지는 마라.
- **update_plan(계획 갱신)은 실제 작업의 대체가 아니다.** 계획을 세웠으면 곧바로 실제 작업 툴
  (read_file/edit_file/write_file/run_code 등)을 호출해 일을 진행하라. 같은 계획을 반복해서
  갱신하지 말고, 오류가 나면 원인을 실제로 고쳐라(예: 잘못된 import는 edit_file로 삭제).
- **작업을 마칠 때 마지막 단계(요약 등)까지 반드시 completed로 갱신한 뒤 최종 요약을 하라.**
  한 단계라도 미완으로 남기면 안 된다.
- 파일을 수정하기 전에 필요하면 먼저 read_file/list_dir로 현황을 파악한다.
- **폴더/파일 구조나 "무슨 파일이 있는지"를 물으면 list_tree로 하위까지 재귀 조회해 파일을 빠짐없이 보여줘라.**
  list_dir(한 단계)만 보고 하위 폴더 안의 파일을 빠뜨리지 마라. 요약할 때 실제로 존재하는 파일(문서·PDF 등)을 임의로 생략하지 말고 전부 나열한다.
- **코드에서 특정 함수·변수·문구를 찾을 땐 파일을 하나씩 열지 말고 grep(정규식 내용 검색)을 써라.**
  파일 이름/확장자로 위치를 찾을 땐 glob('**/*.py' 등)을 쓴다. 큰 파일은 read_file의 offset·limit으로 필요한 줄 범위만 읽어라(줄번호가 함께 나온다).
- **한 파일에 여러 곳을 고칠 땐 edit_file을 반복하지 말고 multi_edit으로 한 번에(원자적으로) 처리하라.**
- **테스트 실행·빌드·git·패키지 설치 등 파일 툴로 못 하는 작업은 run_command로 셸 명령을 실행하라**
  (예: `pytest -q`, `npm test`, `git status`, `dotnet build`). 명령 실행은 승인이 필요할 수 있고, 대화형 명령은 피한다.
  코드를 고친 뒤 테스트가 있으면 run_command로 돌려 통과를 확인한다.
- **라이브러리 사용법·API 문서 등 외부 자료가 필요하면 web_fetch로 해당 URL의 본문을 가져와 참고하라**(공개 http/https만).
- 부분 수정은 edit_file, 새 파일이나 전면 재작성은 write_file을 사용한다.
- **파일을 옮기거나 이름을 바꿀 때는 반드시 move 툴을 써라. read_file+write_file로 옮기지 마라 —
  PDF·엑셀·한글 등 바이너리가 깨진다.** 폴더별 정리도 move로 하면 파일당 한 번에 끝난다.
- **정리·분류로 이동할 때는 폴더만 바꾸고 파일 이름은 원본 그대로 둬라 — 사용자가 개명을 명시적으로
  요청하지 않는 한 이름을 절대 바꾸지 마라.** move 목적지는 반드시 `대상폴더/원래파일명` 형태다
  (예: `readme.txt`→`docs/readme.txt`, `매출표.xlsx`→`office/매출표.xlsx`). **한글 이름을 영어로 번역하거나
  임의로 '정규화'하지 마라 — 사용자가 붙인 이름이 곧 정보다**(readme를 resume로 바꾸는 식의 오개명은 치명적).
- 폴더는 create_dir로 만들고 delete_dir로 삭제한다 — **delete_dir는 하위 내용까지 재귀 삭제되니 신중히 써라.**
- **삭제 판단은 '누가 시켰나'로 먼저 갈라라:**
  - **사용자가 삭제를 명시했으면(예: "더미·미완성·중복 파일 삭제해") 지시대로 해당 파일을 찾아 삭제하라 —
    이미 승인된 삭제이니 "확인이 필요하다"며 미루거나 되묻지 마라.** 대상은 미완성 스텁(`# TODO`만 있는
    파일 등)·빈 파일·잔여 사본(main_복사본.py 류)이다. **순서가 중요하다: 분류·이동보다 먼저 삭제 대상을
    골라 지워라**(안 그러면 미완성 파일이 code/ 등으로 잘못 이동돼 오분류가 굳는다). **확장자가 코드(.py 등)
    라고 미완성 판정을 건너뛰지 마라 — 실제 구현 없이 `# TODO`·플레이스홀더 주석만 있거나 사실상 비어 있으면
    확장자와 무관하게 '미완성'이니 code/로 옮기지 말고 삭제한다**(예: 내용이 `# TODO: 나중에 작성`뿐인
    untitled.py). **반대로 실제 동작하는 코드에 TODO 주석이 섞여 있을 뿐이면 완성본이니 삭제하지 말고
    분류·이동하라.**
  - **사용자가 '정리/분류'만 요청하고 삭제는 언급하지 않았으면, 네 판단으로 지우는 건 매우 보수적으로 하라:**
    명백한 시스템 잡동사니 — 임시(*.tmp)·백업(*.bak)·OS 캐시(Thumbs.db·.DS_Store·desktop.ini)·오피스
    잠금 파일(`~$`로 시작) — 만 삭제한다. **내용이 든 문서·데이터·메모·설정·코드 파일과 로그(*.log)는 이름이
    사소해 보여도 임의로 삭제하지 말고 성격에 맞는 폴더로 이동만 하라(로그는 logs/).**
  - **어느 경우든 확신이 안 서면 삭제 대신 이동하라.** 무엇을 삭제·이동했는지 마지막 요약에 명시한다.
- **폴더 정리 시:** 잡동사니는 삭제하고(폴더로 옮기지 말 것) **내용 있는 파일은 성격에 맞는 폴더로 이동한다
  (삭제하지 말 것).** 같은 종류(코드·웹·문서·데이터·설정·로그·스크립트 등)는 반드시 같은 폴더로 일관되게
  분류하고, 로그를 문서 폴더에·스크립트를 문서 폴더에 섞지 마라. **폴더 집합은 시작할 때 한 번 정하고
  (예: code/data/docs/config/logs/web) 같은 용도의 폴더를 도중에 또 만들지 마라(예: script_files와 scripts
  중복 금지).** **move의 '원본 경로'는 list_tree/list_dir 출력에 실제로 나온 이름을 그대로 복사해 쓰고,
  기억에 의존해 없는 파일명을 지어내지 마라.** 정리 후 list_tree로 최상위에 미분류 파일이나 빈 폴더가 남지
  않았는지 확인하고, 빈 폴더는 delete_dir로 정리한다.
- **여러 파일을 처리하는 작업(정리·일괄 변경 등)은 도중에 멈추지 말고 목록의 끝까지 이어서 진행하라.**
  한 파일을 처리한 뒤 설명만 하고 멈추지 말고, 곧바로 다음 파일에 대한 툴을 호출한다. 전부 끝낸 뒤에 요약한다.
- **독립적인 여러 작업(여러 파일 읽기·검색 등)은 한 응답에서 tool call을 여러 개 동시에 호출해 왕복을 줄여라.**
- 모든 경로는 작업 폴더 기준 상대경로로 지정한다.
- **코드는 완전하고 실행 가능하게 작성하라.** TODO·미완성 스텁·"여기에 구현"류 주석을 남기지 마라.
  요구된 기능을 전부 구현한다 (예: 게임이면 렌더링·입력·충돌·점수·게임오버 루프까지 실제로 동작하게).
- **웹 산출물(HTML/JS)을 만들거나 고친 뒤에는 반드시 run_web으로 실제 실행해 검증하라.**
  에러가 보고되면 그 원인을 파일에서 고치고 다시 run_web으로 검증하며, 에러가 0건이 될 때까지 반복한다.
  run_web이 "캔버스가 비어있다"고 하면 렌더링이 안 되는 것이니 그리기 로직을 고쳐 다시 검증하라.
  에러 0건이어도 요구된 기능이 빠졌으면 아직 미완성이다 — 채워서 완성하라.
- **게임처럼 키보드로 조작하는 페이지는 에러 0건·정상 렌더링만으로 "완성"이라 하지 마라 — 실제로 조작해봐야 한다.**
  run_web을 actions와 함께 호출해 실제 컨트롤(예: 방향키)을 눌러보고, 리스너가 등록됐는지와 눌린 방향으로
  화면이 실제로 반응하는지(좌우 변화 비율 등)를 확인하라. "좌우를 눌렀는데 변화량이 비슷하다"는 경고가 나오면
  이동/충돌 판정 로직(좌표가 정수인지 등)을 의심하고 고친 뒤 다시 actions로 검증한다.
  "완성했다"고 말하기 전에 에러 0건 + 화면이 실제로 그려짐 + (조작 가능한 페이지라면) 조작 테스트에서
  경고가 없음을 모두 확인해야 한다.
- **코드(Python·C/C++·C#)를 만들거나 고친 뒤에는 run_code로 실제 실행·컴파일해 검증하라.**
  에러가 나오면 원인을 고쳐 에러 0건이 될 때까지 반복한다. (웹은 run_web, 코드는 run_code)
- **완벽하게 계획한 뒤 한 번에 만들려 하지 마라.** 장황한 계획으로 토큰을 소모하지 말고,
  먼저 동작하는 최소 버전을 write_file로 만든 다음 run_web으로 검증하고, 그 결과를 보며
  점진적으로 개선하라. 생각은 짧게, 행동(툴 호출)은 빠르게.
- 작업을 마치면 무엇을 했고 검증 결과가 어땠는지 한국어로 간결히 요약한다."""

# 승인 대기 레지스트리 (단일 프로세스 asyncio 기준)
_pending: dict[str, dict[str, Any]] = {}


def resolve_approval(key: str, approved: bool) -> bool:
    p = _pending.get(key)
    if not p:
        return False
    p["approved"] = approved
    p["event"].set()
    return True


async def _chat_turn(host: str, payload: dict) -> AsyncGenerator[dict, None]:
    """한 턴 스트리밍. content/thinking 토큰을 흘리고, 마지막에 종합 결과를 yield."""
    timeout = httpx.Timeout(None, connect=5)
    content = ""
    thinking = ""
    tool_calls: list[dict] = []
    done_reason = None
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
                if data.get("error"):  # 스트림 도중 오류(툴콜 파싱 실패 등) → 상위에서 재생성 처리
                    raise OllamaHTTPError(500, str(data["error"]))
                msg = data.get("message") or {}
                if msg.get("thinking"):
                    thinking += msg["thinking"]
                    yield {"type": "thinking", "text": msg["thinking"]}
                if msg.get("content"):
                    content += msg["content"]
                    yield {"type": "content", "text": msg["content"]}
                if msg.get("tool_calls"):
                    tool_calls.extend(msg["tool_calls"])
                if data.get("done"):
                    done_reason = data.get("done_reason")
    yield {
        "_final": True,
        "content": content,
        "thinking": thinking,
        "tool_calls": tool_calls,
        "done_reason": done_reason,
    }


def _parse_args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


_reindexing: set[str] = set()  # 진행 중인 워크스페이스 (중복 방지)
_bg_tasks: set = set()          # 백그라운드 태스크 강참조(GC 방지)


def _fire_reindex(root: Path, host: str) -> None:
    """색인 최신화를 백그라운드로 던진다 — 응답(done)을 막지 않는다.

    색인은 '다음 런의 시작'에서만 쓰이므로 임계 경로에 있을 필요가 없다. 임베딩 시간이
    사용자 체감 완료를 지연시키지 않게 detached task로 실행한다. 색인이 이미 있을 때만.
    """
    key = str(root)
    if key in _reindexing:
        return  # 이미 이 워크스페이스 재색인 중 → 중복 방지
    try:
        st = rag_status(root)
    except Exception:  # noqa: BLE001
        return
    model = st.get("embed_model")
    if not st.get("indexed") or not model:
        return

    async def _bg() -> None:
        try:
            async for _ev in build_index(root, host, model):
                pass
        except Exception:  # noqa: BLE001 — 재색인 실패는 조용히
            pass
        finally:
            _reindexing.discard(key)

    try:
        task = asyncio.create_task(_bg())
        _reindexing.add(key)
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
    except RuntimeError:  # 실행 중 루프 없음(이론상) → 무시
        pass


def _maybe_reindex(root: Path, host: str, dirty: bool, rag_available: bool) -> None:
    """종료(done) 직전마다 호출 — 파일이 바뀌었고 색인이 있으면 백그라운드 재색인을 던진다.

    모든 종료 경로가 반드시 이 한 곳을 거치게 해 '어떤 exit에서 재색인을 빠뜨려
    색인이 조용히 낡는' 실수를 구조적으로 없앤다.
    """
    if dirty and rag_available:
        _fire_reindex(root, host)


async def _generate_turn(
    host: str, base: dict, reasoning_effort: str, layers: Any, offload_noticed: bool
) -> AsyncGenerator[dict, None]:
    """한 턴 생성 — 오프로드 사다리 + gpt-oss 파싱오류 재생성 + 스트리밍을 캡슐화한다.

    스트림/알림 이벤트(thinking·content·notice)는 그대로 yield하고, 마지막에 딱 하나의
    종료 마커를 yield하고 끝난다:
        {"_gen": True, "final": <dict|None>, "error": <str|None>, "offload_noticed": bool}
    - final 있음 → 성공(툴콜/컨텐츠를 담은 _chat_turn 최종 이벤트).
    - error 있음 → 치명적 종료(호출자가 그대로 error 이벤트로 내보내고 런 종료).
    offload_noticed는 '런 1회만 알림' 정책을 유지하려 들어오고 갱신되어 나간다.
    """
    parse_retries = 0
    while True:
        final = None
        yielded_any = False  # 이 시도에서 이미 토큰을 흘렸는지 (중복 렌더 방지)
        parse_failed = False
        attempts = build_attempts(base, reasoning_effort, layers)
        for i, payload in enumerate(attempts):
            try:
                async for ev in _chat_turn(host, payload):
                    if ev.get("_final"):
                        final = ev
                    else:
                        yielded_any = True
                        yield ev
                break
            except OllamaHTTPError as e:
                # 스트리밍 전에 난 파싱 오류(내용 미출력)면 재생성으로 회복 가능
                if is_tool_parse_error(e.body) and not yielded_any:
                    parse_failed = True
                    final = None
                    break
                last = i == len(attempts) - 1
                crash = is_load_crash(e.body)
                if not last and (crash or is_think_unsupported(e.body)):
                    if crash and not offload_noticed:
                        offload_noticed = True
                        yield {
                            "type": "notice",
                            "text": "VRAM 부족 — CPU 오프로드로 실행합니다 (느려질 수 있어요)",
                        }
                    continue
                yield {"_gen": True, "final": None,
                       "error": f"Ollama 오류 ({e.status}): {e.body[:300]}",
                       "offload_noticed": offload_noticed}
                return
            except Exception as e:  # noqa: BLE001
                yield {"_gen": True, "final": None, "error": f"연결 실패: {e}",
                       "offload_noticed": offload_noticed}
                return

        if parse_failed and parse_retries < MAX_PARSE_RETRIES:
            parse_retries += 1
            if parse_retries == 1:
                yield {"type": "notice", "text": "모델 출력 형식 오류(도구 호출 파싱) — 다시 생성합니다…"}
            continue  # 같은 요청으로 재생성 (temperature 편차로 대개 회복)
        break

    if final is None:
        err = (
            "모델이 올바른 형식의 응답을 만들지 못했습니다(도구 호출 파싱 반복 실패). "
            "추론 강도를 낮추거나 다시 시도해보세요."
            if parse_failed else "빈 응답"
        )
        yield {"_gen": True, "final": None, "error": err, "offload_noticed": offload_noticed}
        return
    yield {"_gen": True, "final": final, "error": None, "offload_noticed": offload_noticed}


async def run_agent(
    *,
    host: str,
    workspace: str,
    model: str,
    messages: list[dict],
    reasoning_effort: str = "medium",
    temperature: float = 0.7,
    context_length: int = 16384,
    approval_mode: str = "require",
    session_id: str = "",
    rag_enabled: bool = True,
    rag_top_k: int = 5,
    keep_alive: str = "30m",
) -> AsyncGenerator[dict, None]:
    try:
        root: Path = validate_workspace(workspace)
    except ToolError as e:
        yield {"type": "error", "error": str(e)}
        return

    convo: list[dict] = list(messages)  # 대화(user/assistant/tool)만. 시스템+계획은 매 턴 재구성.
    plan: list[dict] = []
    layers = await model_layers(host, model)
    offload_noticed = False
    dirty = False  # 파일이 실제로 변경됐는지 (자동 재색인 트리거)
    last_call_sig: str | None = None  # 직전 툴 호출 서명 (무한 루프 감지용)
    repeat_count = 0
    nudges = 0  # 툴 없이 멈추려 할 때 이어가라고 찌른 연속 횟수 (진행하면 리셋)
    spin = 0    # 실질 작업(메타 툴 외) 없이 흘려보낸 연속 턴 수 (계획 갱신·설명만 반복 감지)

    # RAG — 색인이 있으면 (1)마지막 사용자 요청으로 자동 검색해 컨텍스트 주입,
    # (2)search_docs 툴 제공. 임베딩 모델은 색인에 저장된 것을 쓰므로 채팅 모델과 무관.
    rag_available = False
    rag_context = ""
    tools = list(AGENT_TOOLS)
    if rag_enabled:
        try:
            if rag_status(root).get("indexed"):
                rag_available = True
                tools = [SEARCH_DOCS_SCHEMA] + tools
                last_user = next(
                    (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), ""
                )
                if last_user.strip():
                    rag_context = format_context(await rag_search(root, host, last_user, rag_top_k))
        except (RagError, Exception):  # noqa: BLE001 — RAG 실패는 치명적이지 않음
            rag_available = rag_available and bool(rag_context)

    # ── KV 캐시 재사용을 위한 '안정적 프리픽스' ──────────────────────────────
    # 시스템 메시지 = SYSTEM_PROMPT(+RAG 힌트/컨텍스트)로 런 내내 바이트 고정한다.
    # (Ollama는 프롬프트 앞부분이 그대로면 KV를 재사용 → 매 턴 ~1.5s 재처리를 15~60ms로.)
    # 계획은 매 턴 별도 메시지로 주입하지 않는다 — update_plan '툴 결과'에 현재 계획 전체를
    # 담아 대화(append-only)에 남긴다. 그래야 (1)프리픽스가 안 깨지고 (2)약한 모델이 계획
    # 리마인더를 자기 답변에 그대로 복사(에코)하는 일이 없다.
    stable_sys = SYSTEM_PROMPT
    if rag_available:
        stable_sys += (
            "\n- 파일명을 몰라도 작업 폴더 전체를 의미로 검색하려면 search_docs를 사용하라. "
            "아래 자동 검색 결과도 참고하되, 정확한 최신 내용은 read_file로 확인하라."
        )
    if rag_context:
        stable_sys += "\n\n" + rag_context
    system_msg = {"role": "system", "content": stable_sys}
    # 압축 예산 계산용 고정 오버헤드(토큰 근사) — 시스템+툴 스키마
    reserve_tokens = (len(stable_sys) + len(json.dumps(tools, ensure_ascii=False))) // 3

    for step in range(MAX_STEPS):
        working = compact_convo(convo, context_length, reserve_tokens)
        messages = [system_msg, *working]
        base = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "stream": True,
            "keep_alive": keep_alive,
            "options": {
                "temperature": temperature,
                "num_ctx": context_length,
            },
        }
        # 생성(오프로드 사다리 + 파싱오류 재생성 + 스트리밍)은 _generate_turn에 위임한다.
        # 스트림/알림은 그대로 흘리고, 종료 마커(_gen)에서 최종 결과 또는 치명 오류를 받는다.
        final = None
        gen_error = None
        async for ev in _generate_turn(host, base, reasoning_effort, layers, offload_noticed):
            if ev.get("_gen"):
                final = ev["final"]
                gen_error = ev["error"]
                offload_noticed = ev["offload_noticed"]
            else:
                yield ev
        if gen_error is not None:  # 치명적 종료(연결·Ollama·빈 응답·파싱 소진) → 런 종료
            yield {"type": "error", "error": gen_error}
            return

        tool_calls = final.get("tool_calls") or []
        if not tool_calls:
            spin += 1  # 툴을 안 부른 턴 = 실질 진전 없음
            truncated = final.get("done_reason") == "length"
            incomplete = [s for s in plan if s.get("status") != "completed"] if plan else []
            # 자동 이어가기: 툴 없이 끝내려 하지만 계획에 미완 단계가 남았으면, 끝내지 말고
            # '다음 단계를 실제로 실행하라'고 찔러 이어가게 한다 (넛지·정체 한도 안에서만).
            if not truncated and incomplete and nudges < MAX_NUDGES and spin < SPIN_LIMIT:
                nudges += 1
                if final.get("content", "").strip():  # 모델의 이번 설명을 대화에 남긴다
                    convo.append({"role": "assistant", "content": final["content"]})
                todo = "; ".join(s.get("content", "") for s in incomplete[:5])
                convo.append({
                    "role": "user",
                    "content": (
                        f"아직 끝나지 않았다. 남은 단계: {todo}. 멈추거나 설명만 하지 말고 지금 바로 "
                        "다음 단계를 tool 호출로 실행하라. 모든 단계가 completed가 될 때까지 이어서 진행하고, "
                        "완료된 단계는 update_plan으로 갱신하라."
                    ),
                })
                yield {"type": "notice", "text": "미완 단계가 남아 자동으로 이어서 진행합니다…"}
                continue
            if truncated:
                yield {
                    "type": "notice",
                    "text": "⚠ 컨텍스트 한도에 도달해 응답이 중간에 잘렸습니다. 설정에서 '컨텍스트 길이'를 늘리거나 '추론 강도'를 낮춰보세요.",
                }
            # 파일이 변경됐고 색인이 있으면 백그라운드로 증분 재색인 (done을 막지 않음)
            _maybe_reindex(root, host, dirty, rag_available)
            yield {"type": "done"}
            return

        # assistant 턴(툴콜 포함)을 대화에 기록
        convo.append(
            {
                "role": "assistant",
                "content": final.get("content", ""),
                "tool_calls": tool_calls,
            }
        )

        for idx, tc in enumerate(tool_calls):
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            args = _parse_args(fn.get("arguments"))
            call_id = f"{step}-{idx}"

            # 무한 루프 감지: 완전히 동일한 (툴,인자) 호출이 연속 반복되면 정체로 보고 멈춘다.
            # (정상 진행은 서명이 매번 달라지므로 걸리지 않는다 — 다른 파일/다른 동작.)
            sig = f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
            if sig == last_call_sig:
                repeat_count += 1
            else:
                repeat_count, last_call_sig = 0, sig
            if repeat_count >= STALL_REPEAT:
                yield {
                    "type": "notice",
                    "text": (
                        f"같은 동작을 {repeat_count + 1}회 연속 반복해 멈췄습니다(무한 루프 방지). "
                        "요청을 조금 더 구체적으로 다시 지시하거나 '계속해줘'로 이어가세요."
                    ),
                }
                _maybe_reindex(root, host, dirty, rag_available)
                yield {"type": "done"}
                return

            yield {"type": "tool_call", "id": call_id, "name": name, "args": args}

            # 계획 갱신 — 별도 상태로 관리하고 UI에 plan 이벤트로 전달
            if name == "update_plan":
                plan = normalize_plan(args.get("steps"))
                done = sum(1 for s in plan if s["status"] == "completed")
                yield {"type": "plan", "steps": plan}
                # 현재 계획 전체를 툴 결과에 담는다 — 모델이 진행 상황을 여기서 확인한다
                result = f"계획 갱신됨 (완료 {done}/{len(plan)}).\n" + render_plan(plan).strip()
                yield {"type": "tool_result", "id": call_id, "ok": True, "output": result}
                convo.append({"role": "tool", "content": result})
                continue

            # 파괴적 툴 → 승인 대기 (모드에 따라)
            if needs_approval(name, approval_mode):
                key = f"{session_id}:{call_id}"
                event = asyncio.Event()
                _pending[key] = {"event": event, "approved": False}
                yield {"type": "approval_request", "id": call_id, "name": name, "args": args}
                try:
                    await asyncio.wait_for(event.wait(), timeout=APPROVAL_TIMEOUT)
                    approved = _pending[key]["approved"]
                except asyncio.TimeoutError:
                    approved = False
                finally:
                    _pending.pop(key, None)
                if not approved:
                    result = "[거부됨] 사용자가 이 작업을 승인하지 않았습니다."
                    yield {"type": "tool_result", "id": call_id, "ok": False, "output": result, "rejected": True}
                    convo.append({"role": "tool", "content": result})
                    continue

            try:
                spec = REGISTRY.get(name)
                if spec is None:
                    # 미등록 툴 → run_tool이 "알 수 없는 툴" ToolError를 낸다 (기존 동작 보존)
                    result, shot = run_tool(root, name, args), None
                else:
                    result, shot = await execute(spec, root, host, args)
                    if spec.mutates:  # 파일이 바뀔 수 있는 툴 → 색인 최신화 필요
                        dirty = True
                yield {"type": "tool_result", "id": call_id, "ok": True, "output": result}
                if shot:
                    yield {"type": "screenshot", "id": call_id, "data": shot}
            except ToolError as e:
                result = f"[오류] {e}"
                yield {"type": "tool_result", "id": call_id, "ok": False, "output": result}
            except Exception as e:  # noqa: BLE001 — 잘못된 인자 등 예기치 못한 예외로 런을
                # 중단하지 말고, 오류를 모델에 돌려주어 스스로 고쳐 이어가게 한다.
                result = f"[오류] 툴 실행 실패 ({type(e).__name__}): {e}"
                yield {"type": "tool_result", "id": call_id, "ok": False, "output": result}
            convo.append({"role": "tool", "content": result})

        # ── 정체(spin) 감지 ── 이번 턴에 실제 작업 툴(메타 툴 외)이 있었나?
        substantive = any(
            not is_meta((tc.get("function") or {}).get("name", "")) for tc in tool_calls
        )
        if substantive:
            spin = 0
            nudges = 0  # 실제 진전 → 카운터 리셋
        else:
            # 이 턴엔 update_plan 같은 메타 툴만 호출 = 실질 진전 없음
            spin += 1
            if spin >= SPIN_LIMIT:
                _maybe_reindex(root, host, dirty, rag_available)
                yield {
                    "type": "notice",
                    "text": (
                        "실제 작업 없이 계획 갱신·설명만 반복하고 있어 중단했습니다. "
                        "요청을 더 구체적으로 다시 지시하거나, 더 강한 모델(gpt-oss)로 바꿔보세요."
                    ),
                }
                yield {"type": "done"}
                return
            # 첫 계획 수립 턴은 정상이므로 봐주고, 두 번째 비생산 턴부터 실제 작업을 재촉한다.
            if spin >= 2:
                convo.append({
                    "role": "user",
                    "content": (
                        "계획(update_plan)만 반복해서 갱신하지 마라. 계획은 이미 있으니 지금 즉시 "
                        "실제 작업 툴을 호출하라 — 예: 코드 오류는 edit_file/write_file로 직접 고치고 "
                        "run_code로 검증하라. 설명이나 계획 갱신 말고 실제 툴 호출로만 응답하라."
                    ),
                })

    # 최후의 안전선 도달 — 오류가 아니라 '길어서 잠깐 멈춤'으로 안내하고 이어갈 수 있게 한다.
    _maybe_reindex(root, host, dirty, rag_available)
    yield {
        "type": "notice",
        "text": (
            f"작업이 매우 길어 {MAX_STEPS}단계에서 일단 멈췄습니다(폭주 방지 안전선). "
            "여기까지 한 내용은 유지됩니다 — 이어서 계속하려면 '계속해줘'라고 해주세요."
        ),
    }
    yield {"type": "done"}
