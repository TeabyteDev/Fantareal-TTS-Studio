from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fantareal_tts_studio import runtime_installer, service, supervision
from fantareal_tts_studio.supervision import (
    InterprocessFileLock,
    LockUnavailable,
    WindowsOwnerEndpoint,
    interprocess_lock_is_held,
    probe_windows_owner_endpoint,
    replace_file_with_retry,
)


def test_interprocess_lock_rejects_second_handle_and_releases(tmp_path: Path) -> None:
    path = tmp_path / "owner.lock"

    with InterprocessFileLock(path):
        assert interprocess_lock_is_held(path) is True
        with pytest.raises(LockUnavailable):
            InterprocessFileLock(path).acquire()

    assert interprocess_lock_is_held(path) is False


def test_interprocess_lock_auto_releases_when_owner_process_is_killed(tmp_path: Path) -> None:
    path = tmp_path / "owner.lock"
    ready = tmp_path / "ready"
    source_root = str(Path(__file__).parents[1] / "src")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = source_root
    script = (
        "import sys, time; from pathlib import Path; "
        "from fantareal_tts_studio.supervision import InterprocessFileLock; "
        "lock=InterprocessFileLock(Path(sys.argv[1])); lock.acquire(); "
        "Path(sys.argv[2]).write_text('ready'); time.sleep(30)"
    )
    owner = subprocess.Popen(
        [sys.executable, "-c", script, str(path), str(ready)],
        env=environment,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        deadline = time.monotonic() + 10.0
        while not ready.is_file() and owner.poll() is None:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert owner.poll() is None
        assert interprocess_lock_is_held(path) is True

        owner.kill()
        owner.wait(timeout=5)

        with InterprocessFileLock(path):
            assert interprocess_lock_is_held(path) is True
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named-pipe owner proof")
def test_windows_owner_endpoint_reports_actual_pid_and_epoch(tmp_path: Path) -> None:
    install_id = "a" * 32
    token = "b" * 64

    with WindowsOwnerEndpoint(tmp_path, install_id=install_id, owner_token=token):
        probe = probe_windows_owner_endpoint(tmp_path)

        assert probe.status == "verified"
        assert probe.actual_pid == os.getpid()
        assert probe.identity == {
            "ownerProtocolVersion": 2,
            "installId": install_id,
            "ownerToken": token,
            "pid": os.getpid(),
        }
        with pytest.raises(LockUnavailable):
            WindowsOwnerEndpoint(
                tmp_path,
                install_id="c" * 32,
                owner_token="d" * 64,
            ).start()

    assert probe_windows_owner_endpoint(tmp_path).status == "absent"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named-pipe owner proof")
def test_windows_owner_endpoint_releases_when_owner_process_is_killed(tmp_path: Path) -> None:
    ready = tmp_path / "endpoint-ready"
    source_root = str(Path(__file__).parents[1] / "src")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = source_root
    script = (
        "import os, sys, time; from pathlib import Path; "
        "from fantareal_tts_studio.supervision import WindowsOwnerEndpoint; "
        "endpoint=WindowsOwnerEndpoint(Path(sys.argv[1]), install_id='e'*32, "
        "owner_token='f'*64); endpoint.start(); "
        "Path(sys.argv[2]).write_text(str(os.getpid())); time.sleep(30)"
    )
    owner = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), str(ready)],
        env=environment,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        deadline = time.monotonic() + 10.0
        while not ready.is_file() and owner.poll() is None:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert owner.poll() is None
        actual_pid = int(ready.read_text(encoding="utf-8"))
        active = probe_windows_owner_endpoint(tmp_path)
        assert active.status == "verified"
        assert active.actual_pid == actual_pid

        owner.kill()
        owner.wait(timeout=5)
        deadline = time.monotonic() + 5.0
        while probe_windows_owner_endpoint(tmp_path, timeout=0.1).status != "absent":
            assert time.monotonic() < deadline
            time.sleep(0.02)

        with WindowsOwnerEndpoint(
            tmp_path,
            install_id="1" * 32,
            owner_token="2" * 64,
        ):
            assert probe_windows_owner_endpoint(tmp_path).status == "verified"
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)


def test_replace_file_retries_transient_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.tmp"
    destination = tmp_path / "state.json"
    source.write_text("new", encoding="utf-8")
    real_replace = supervision.os.replace
    calls = 0

    def flaky_replace(current: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise PermissionError("simulated sharing violation")
        real_replace(current, target)

    monkeypatch.setattr(supervision.os, "replace", flaky_replace)
    monkeypatch.setattr(supervision.time, "sleep", lambda _seconds: None)

    replace_file_with_retry(source, destination)

    assert calls == 3
    assert destination.read_text(encoding="utf-8") == "new"
    assert not source.exists()


@pytest.mark.parametrize("module", [service, runtime_installer])
def test_atomic_write_does_not_hide_permanent_permission_error_or_leave_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: object
) -> None:
    path = tmp_path / "state.json"
    monkeypatch.setattr(
        module,
        "replace_file_with_retry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("permanent")),
    )

    with pytest.raises(PermissionError, match="permanent"):
        module.atomic_write_json(path, {"status": "running"})  # type: ignore[attr-defined]

    assert not list(tmp_path.glob(".*.tmp"))
