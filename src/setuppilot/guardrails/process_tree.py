"""Start a command as the root of its own process tree, so a stop reaches every process it
starts: a process group on macOS and Linux, a Job Object on Windows."""

import contextlib
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

# PROVISIONAL: time for `docker run` and `docker compose` to stop their containers after a
# polite stop, before the tree is forced to stop.
STOP_GRACE_SECONDS = 3


class ProcessTree(Protocol):
    """A running command and every process it started."""

    @property
    def was_stopped(self) -> bool:
        """True once stop() was called, so the command did not end by itself."""

    def stop(self) -> None:
        """Stop the tree: on macOS and Linux ask, wait STOP_GRACE_SECONDS, then force; on
        Windows force at once. Safe from any thread, and safe to call twice."""

    def close(self) -> None:
        """Force-stop whatever is still running and let go of the tree. Call it once, at the end."""


# Coverage is measured on Linux, where this block cannot run; the Windows CI job tests it.
if sys.platform == "win32":  # pragma: no cover
    import ctypes
    from ctypes import wintypes

    # Values from the Win32 headers (winbase.h, winnt.h, tlhelp32.h).
    _CREATE_SUSPENDED = 0x00000004
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    # Closing the job kills it; a crash dialog cannot keep a run waiting.
    _LIMIT_FLAGS = 0x00002000 | 0x00000400  # KILL_ON_JOB_CLOSE | DIE_ON_UNHANDLED_EXCEPTION
    _PROCESS_SET_QUOTA_AND_TERMINATE = 0x0100 | 0x0001
    _THREAD_SUSPEND_RESUME = 0x0002
    _TH32CS_SNAPTHREAD = 0x00000004
    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
    _RESUME_FAILED = 0xFFFFFFFF

    class _BasicLimits(ctypes.Structure):  # JOBOBJECT_BASIC_LIMIT_INFORMATION
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),  # ULONG_PTR
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimits(ctypes.Structure):  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", ctypes.c_ulonglong * 6),  # IO_COUNTERS: six ULONGLONG counters
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _ThreadEntry(ctypes.Structure):  # THREADENTRY32
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def _api(name: str, restype: Any, *argtypes: Any) -> Any:
        # Without argtypes and a HANDLE restype, ctypes cuts 64-bit handles down to 32 bits.
        function = getattr(_kernel32, name)
        function.restype, function.argtypes = restype, argtypes
        return function

    _HANDLE, _DWORD, _BOOL = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL
    _create_job = _api("CreateJobObjectW", _HANDLE, wintypes.LPVOID, wintypes.LPCWSTR)
    _set_job_info = _api(
        "SetInformationJobObject", _BOOL, _HANDLE, ctypes.c_int, wintypes.LPVOID, _DWORD
    )
    _assign_to_job = _api("AssignProcessToJobObject", _BOOL, _HANDLE, _HANDLE)
    _terminate_job = _api("TerminateJobObject", _BOOL, _HANDLE, wintypes.UINT)
    _open_process = _api("OpenProcess", _HANDLE, _DWORD, _BOOL, _DWORD)
    _open_thread = _api("OpenThread", _HANDLE, _DWORD, _BOOL, _DWORD)
    _resume_thread = _api("ResumeThread", _DWORD, _HANDLE)
    _snapshot = _api("CreateToolhelp32Snapshot", _HANDLE, _DWORD, _DWORD)
    _first_thread = _api("Thread32First", _BOOL, _HANDLE, ctypes.POINTER(_ThreadEntry))
    _next_thread = _api("Thread32Next", _BOOL, _HANDLE, ctypes.POINTER(_ThreadEntry))
    _close_handle = _api("CloseHandle", _BOOL, _HANDLE)

    def _check(result: Any, what: str) -> Any:
        if not result:
            code = ctypes.get_last_error()
            raise ctypes.WinError(code, f"{what} failed: {ctypes.FormatError(code).strip()}")
        return result

    def _new_job() -> int:
        job = _check(_create_job(None, None), "CreateJobObjectW")
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = _LIMIT_FLAGS
        try:
            info = (_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits))
            _check(_set_job_info(job, *info, ctypes.sizeof(limits)), "SetInformationJobObject")
        except BaseException:
            _close_handle(job)
            raise
        return job

    def _assign(job: int, pid: int) -> None:
        process = _check(_open_process(_PROCESS_SET_QUOTA_AND_TERMINATE, False, pid), "OpenProcess")
        try:
            _check(_assign_to_job(job, process), "AssignProcessToJobObject")
        finally:
            _close_handle(process)

    def _resume(pid: int) -> None:
        """Popen closes the thread handle CreateProcess gives back, so find the thread again."""
        snapshot = _snapshot(_TH32CS_SNAPTHREAD, 0)
        if snapshot == _INVALID_HANDLE_VALUE:
            _check(False, "CreateToolhelp32Snapshot")
        resumed = 0
        try:
            entry = _ThreadEntry(dwSize=ctypes.sizeof(_ThreadEntry))
            has_entry = _first_thread(snapshot, ctypes.byref(entry))
            while has_entry:
                if entry.th32OwnerProcessID == pid:
                    thread = _check(
                        _open_thread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID),
                        "OpenThread",
                    )
                    try:
                        if _resume_thread(thread) == _RESUME_FAILED:
                            _check(False, "ResumeThread")
                    finally:
                        _close_handle(thread)
                    resumed += 1
                has_entry = _next_thread(snapshot, ctypes.byref(entry))
        finally:
            _close_handle(snapshot)
        if not resumed:
            raise OSError(f"Process {pid} was started paused, but none of its threads was found.")

    class _Job:
        def __init__(self, job: int) -> None:
            self._job: int | None = job
            # The Cancel thread and the runner both use the handle; it must not be used closed.
            self._lock = threading.Lock()
            self._was_stopped = False

        @property
        def was_stopped(self) -> bool:
            return self._was_stopped

        def stop(self) -> None:
            # No polite Ctrl+Break first: sent to a group that has already ended, it can reach
            # every program in the console, SetupPilot too.
            self._was_stopped = True
            with self._lock:
                if self._job is not None:
                    _check(_terminate_job(self._job, 1), "TerminateJobObject")

        def close(self) -> None:
            with self._lock:
                job, self._job = self._job, None
            if job is None:
                return
            try:
                _check(_terminate_job(job, 1), "TerminateJobObject")
            finally:
                _close_handle(job)

    def start_tree(
        argv: Sequence[str] | str,
        cwd: Path,
        env: Mapping[str, str],
        executable: str | None,
        *,
        stderr: int,
    ) -> tuple[subprocess.Popen[bytes], ProcessTree]:
        """Start the command paused, put it in a new job, then let it run, so not even its
        first child can start outside the job. Nothing in the job may break away from it."""
        job = _new_job()
        try:
            process = subprocess.Popen(  # noqa: S603 - argv was checked by validate_command
                argv,
                executable=executable,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=stderr,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | _CREATE_SUSPENDED,
            )
        except BaseException:
            _close_handle(job)
            raise
        try:
            _assign(job, process.pid)
            _resume(process.pid)
        except BaseException:
            with process:  # closes its pipe and waits for it
                process.kill()
            _close_handle(job)
            raise
        return process, _Job(job)

else:

    class _ProcessGroup:
        def __init__(self, process: subprocess.Popen[bytes]) -> None:
            self._process = process
            self._was_stopped = False
            self._is_closed = False

        @property
        def was_stopped(self) -> bool:
            return self._was_stopped

        def stop(self) -> None:
            self._was_stopped = True
            # After close the group id may already belong to some other program.
            if not self._is_closed:
                self._stop_group()

        def close(self) -> None:
            self._is_closed = True
            self._stop_group()

        def _stop_group(self) -> None:
            # macOS answers EPERM, not ESRCH, for a group that holds only finished processes.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self._process.pid, signal.SIGTERM)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self._process.wait(timeout=STOP_GRACE_SECONDS)
                os.killpg(self._process.pid, signal.SIGKILL)

    def start_tree(
        argv: Sequence[str] | str,
        cwd: Path,
        env: Mapping[str, str],
        executable: str | None,
        *,
        stderr: int,
    ) -> tuple[subprocess.Popen[bytes], ProcessTree]:
        """Start the command in a new session, so its process group holds everything it starts."""
        process = subprocess.Popen(  # noqa: S603 - argv was checked by validate_command
            argv,
            executable=executable,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=stderr,
            start_new_session=True,
        )
        return process, _ProcessGroup(process)
