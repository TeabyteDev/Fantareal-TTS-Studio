from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any


class LockUnavailable(TimeoutError):
    pass


OWNER_TOKEN_PATTERN = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class OwnerEndpointProbe:
    status: str
    actual_pid: int | None = None
    identity: dict[str, Any] | None = None


def windows_owner_pipe_name(data_root: Path) -> str:
    digest = hashlib.sha256(str(data_root.resolve()).casefold().encode()).hexdigest()
    return rf"\\.\pipe\FantarealTtsStudio-Owner-{digest}"


class WindowsOwnerEndpoint:
    """Single-instance local pipe that proves the installer's actual Windows PID and epoch."""

    def __init__(self, data_root: Path, *, install_id: str, owner_token: str) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows owner endpoints are unavailable on this platform")
        if not re.fullmatch(r"[0-9a-f]{32}", install_id):
            raise ValueError("install_id must be 32 lowercase hexadecimal characters")
        if not OWNER_TOKEN_PATTERN.fullmatch(owner_token):
            raise ValueError("owner_token must be 64 lowercase hexadecimal characters")
        self.pipe_name = windows_owner_pipe_name(data_root)
        self.install_id = install_id
        self.owner_token = owner_token
        self._handle: int | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._fatal_error: BaseException | None = None

    def start(self) -> None:
        if self._handle is not None:
            return
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateNamedPipeW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
        ]
        kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
        handle = kernel32.CreateNamedPipeW(
            self.pipe_name,
            0x00000003 | 0x00080000,
            0x00000000 | 0x00000000 | 0x00000000 | 0x00000008,
            1,
            4096,
            4096,
            250,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            error = ctypes.get_last_error()
            if error in {5, 231}:
                raise LockUnavailable("runtime owner endpoint is already held")
            raise ctypes.WinError(error)
        self._handle = int(handle)
        self._thread = threading.Thread(
            target=self._serve,
            name=f"tts-owner-endpoint-{self.install_id[:8]}",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            self.close()
            raise TimeoutError("runtime owner endpoint did not become ready")
        if self._fatal_error is not None:
            error = self._fatal_error
            self.close()
            raise RuntimeError("runtime owner endpoint failed") from error

    def _serve(self) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
        kernel32.ConnectNamedPipe.restype = wintypes.BOOL
        kernel32.WriteFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPCVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        kernel32.WriteFile.restype = wintypes.BOOL
        kernel32.ReadFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        kernel32.ReadFile.restype = wintypes.BOOL
        kernel32.PeekNamedPipe.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.PeekNamedPipe.restype = wintypes.BOOL
        kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
        kernel32.DisconnectNamedPipe.restype = wintypes.BOOL
        payload = json.dumps(
            {
                "ownerProtocolVersion": 2,
                "installId": self.install_id,
                "ownerToken": self.owner_token,
                "pid": os.getpid(),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        self._ready.set()
        try:
            while not self._stop.is_set():
                connected = kernel32.ConnectNamedPipe(self._handle, None)
                if not connected:
                    error = ctypes.get_last_error()
                    if error != 535:
                        if self._stop.is_set() and error in {6, 109, 232, 233}:
                            break
                        raise ctypes.WinError(error)
                if self._stop.is_set():
                    kernel32.DisconnectNamedPipe(self._handle)
                    break
                written = wintypes.DWORD()
                if not kernel32.WriteFile(
                    self._handle,
                    payload,
                    len(payload),
                    ctypes.byref(written),
                    None,
                ):
                    error = ctypes.get_last_error()
                    if error not in {109, 232, 233}:
                        raise ctypes.WinError(error)
                acknowledgement_deadline = time.monotonic() + 0.5
                while not self._stop.is_set() and time.monotonic() < acknowledgement_deadline:
                    available = wintypes.DWORD()
                    if not kernel32.PeekNamedPipe(
                        self._handle,
                        None,
                        0,
                        None,
                        ctypes.byref(available),
                        None,
                    ):
                        break
                    if available.value:
                        acknowledgement = ctypes.create_string_buffer(16)
                        acknowledgement_size = wintypes.DWORD()
                        kernel32.ReadFile(
                            self._handle,
                            acknowledgement,
                            len(acknowledgement),
                            ctypes.byref(acknowledgement_size),
                            None,
                        )
                        break
                    time.sleep(0.01)
                kernel32.DisconnectNamedPipe(self._handle)
        except BaseException as exc:
            self._fatal_error = exc
            self._ready.set()

    def close(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._stop.set()
        probe_windows_owner_endpoint_by_name(self.pipe_name, timeout=0.25)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(handle)
        self._handle = None
        self._thread = None

    def __enter__(self) -> WindowsOwnerEndpoint:
        self.start()
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()


def probe_windows_owner_endpoint(
    data_root: Path, *, timeout: float = 0.5
) -> OwnerEndpointProbe:
    if os.name != "nt":
        return OwnerEndpointProbe("unsupported")
    return probe_windows_owner_endpoint_by_name(windows_owner_pipe_name(data_root), timeout=timeout)


def probe_windows_owner_endpoint_by_name(
    pipe_name: str, *, timeout: float
) -> OwnerEndpointProbe:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    kernel32.WaitNamedPipeW.restype = wintypes.BOOL
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetNamedPipeServerProcessId.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.ULONG),
    ]
    kernel32.GetNamedPipeServerProcessId.restype = wintypes.BOOL
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.PeekNamedPipe.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.PeekNamedPipe.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        handle = kernel32.CreateFileW(
            pipe_name,
            0x80000000 | 0x40000000,
            0,
            None,
            3,
            0,
            None,
        )
        if handle != ctypes.c_void_p(-1).value:
            break
        error = ctypes.get_last_error()
        if error == 2:
            return OwnerEndpointProbe("absent")
        if error not in {121, 231}:
            return OwnerEndpointProbe("conflict")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return OwnerEndpointProbe("indeterminate")
        kernel32.WaitNamedPipeW(pipe_name, max(1, min(100, int(remaining * 1000))))
    try:
        server_pid = wintypes.ULONG()
        if not kernel32.GetNamedPipeServerProcessId(handle, ctypes.byref(server_pid)):
            return OwnerEndpointProbe("conflict")
        while True:
            available = wintypes.DWORD()
            if not kernel32.PeekNamedPipe(
                handle,
                None,
                0,
                None,
                ctypes.byref(available),
                None,
            ):
                return OwnerEndpointProbe("conflict", actual_pid=int(server_pid.value))
            if available.value:
                break
            if time.monotonic() >= deadline:
                return OwnerEndpointProbe("indeterminate", actual_pid=int(server_pid.value))
            time.sleep(0.01)
        buffer = ctypes.create_string_buffer(4096)
        read = wintypes.DWORD()
        if not kernel32.ReadFile(handle, buffer, len(buffer), ctypes.byref(read), None):
            return OwnerEndpointProbe("conflict", actual_pid=int(server_pid.value))
        acknowledgement = b"ok"
        acknowledgement_size = wintypes.DWORD()
        kernel32.WriteFile(
            handle,
            acknowledgement,
            len(acknowledgement),
            ctypes.byref(acknowledgement_size),
            None,
        )
        try:
            identity = json.loads(buffer.raw[: read.value].decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return OwnerEndpointProbe("conflict", actual_pid=int(server_pid.value))
        if not isinstance(identity, dict):
            return OwnerEndpointProbe("conflict", actual_pid=int(server_pid.value))
        return OwnerEndpointProbe("verified", int(server_pid.value), identity)
    finally:
        kernel32.CloseHandle(handle)


_LOCAL_LOCKS: set[str] = set()
_LOCAL_LOCKS_MUTEX = threading.Lock()


class InterprocessFileLock:
    """Exclusive OS lock whose ownership is released when the process exits."""

    def __init__(self, path: Path, *, timeout: float = 0.0, poll_interval: float = 0.05) -> None:
        self.path = path
        self.timeout = max(0.0, timeout)
        self.poll_interval = max(0.01, poll_interval)
        self._handle: int | None = None
        self._key = str(path.resolve()).casefold()

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self) -> None:
        if self.acquired:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            handle = self._try_acquire()
            if handle is not None:
                self._handle = handle
                return
            if time.monotonic() >= deadline:
                raise LockUnavailable(f"lock is already held: {self.path}")
            time.sleep(min(self.poll_interval, max(0.0, deadline - time.monotonic())))

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes

                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
                kernel32.ReleaseMutex.restype = wintypes.BOOL
                kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
                kernel32.CloseHandle.restype = wintypes.BOOL
                if not kernel32.ReleaseMutex(handle):
                    error = ctypes.get_last_error()
                    kernel32.CloseHandle(handle)
                    raise ctypes.WinError(error)
                if not kernel32.CloseHandle(handle):
                    raise ctypes.WinError(ctypes.get_last_error())
                return
            os.close(handle)
        finally:
            with _LOCAL_LOCKS_MUTEX:
                _LOCAL_LOCKS.discard(self._key)

    def _try_acquire(self) -> int | None:
        with _LOCAL_LOCKS_MUTEX:
            if self._key in _LOCAL_LOCKS:
                return None
            _LOCAL_LOCKS.add(self._key)
        try:
            handle = (
                self._try_acquire_windows() if os.name == "nt" else self._try_acquire_posix()
            )
        except Exception:
            with _LOCAL_LOCKS_MUTEX:
                _LOCAL_LOCKS.discard(self._key)
            raise
        if handle is None:
            with _LOCAL_LOCKS_MUTEX:
                _LOCAL_LOCKS.discard(self._key)
        return handle

    def _try_acquire_windows(self) -> int | None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        digest = hashlib.sha256(str(self.path.resolve()).casefold().encode()).hexdigest()
        handle = kernel32.CreateMutexW(None, False, f"Local\\FantarealTtsStudio-{digest}")
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        result = kernel32.WaitForSingleObject(handle, 0)
        if result in {0x00000000, 0x00000080}:
            return int(handle)
        kernel32.CloseHandle(handle)
        if result == 0x00000102:
            return None
        if result == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        raise OSError(f"unexpected mutex wait result: {result}")

    def _try_acquire_posix(self) -> int | None:
        import fcntl

        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return None
        return descriptor

    def __enter__(self) -> InterprocessFileLock:
        self.acquire()
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.release()


def interprocess_lock_is_held(path: Path) -> bool:
    probe = InterprocessFileLock(path)
    try:
        probe.acquire()
    except LockUnavailable:
        return True
    probe.release()
    return False


def replace_file_with_retry(
    source: Path,
    destination: Path,
    *,
    attempts: int = 6,
    initial_delay: float = 0.01,
) -> None:
    if attempts < 1:
        raise ValueError("attempts must be positive")
    delay = max(0.0, initial_delay)
    for attempt in range(attempts):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt + 1 == attempts:
                raise
            time.sleep(delay)
            delay = min(0.25, max(0.01, delay * 2))


def terminate_windows_process_tree(root_pid: int) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Windows process-tree termination is unavailable")
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    def descendants() -> list[int]:
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        parents: dict[int, list[int]] = {}
        try:
            entry = ProcessEntry()
            entry.dwSize = ctypes.sizeof(entry)
            success = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while success:
                parents.setdefault(int(entry.th32ParentProcessID), []).append(
                    int(entry.th32ProcessID)
                )
                success = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        result: list[int] = []
        pending = list(parents.get(root_pid, []))
        while pending:
            pid = pending.pop()
            if pid in result:
                continue
            result.append(pid)
            pending.extend(parents.get(pid, []))
        return result

    owned = descendants()
    root_handle = kernel32.OpenProcess(0x00100001, False, root_pid)
    if root_handle:
        try:
            kernel32.TerminateProcess(root_handle, 1)
        finally:
            kernel32.CloseHandle(root_handle)
    for pid in descendants():
        if pid not in owned:
            owned.append(pid)
    handles: list[int] = []
    try:
        for pid in reversed(owned):
            handle = kernel32.OpenProcess(0x00100001, False, pid)
            if not handle:
                continue
            handles.append(int(handle))
            kernel32.TerminateProcess(handle, 1)
        for handle in handles:
            kernel32.WaitForSingleObject(handle, 5000)
    finally:
        for handle in handles:
            kernel32.CloseHandle(handle)


class WindowsKillOnCloseJob:
    """Job owned by the installer process; its handle intentionally lives until process exit."""

    def __init__(self) -> None:
        self._handle: int | None = None

    @property
    def active(self) -> bool:
        return self._handle is not None or sys.platform != "win32"

    def activate_for_current_process(self) -> None:
        if self.active:
            return
        import ctypes
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        information = ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        try:
            if not kernel32.SetInformationJobObject(
                handle,
                9,
                ctypes.byref(information),
                ctypes.sizeof(information),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel32.AssignProcessToJobObject(handle, kernel32.GetCurrentProcess()):
                raise ctypes.WinError(ctypes.get_last_error())
        except Exception:
            kernel32.CloseHandle(handle)
            raise
        self._handle = int(handle)
