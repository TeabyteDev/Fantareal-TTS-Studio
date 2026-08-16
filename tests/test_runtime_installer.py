from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import venv
import zipfile
from pathlib import Path

import pytest

from fantareal_tts_studio import runtime_installer
from fantareal_tts_studio.runtime_installer import (
    RUNTIME_COMMIT,
    InstallerConfig,
    InstallFailure,
    RuntimeInstaller,
)


def make_runtime_archive(path: Path, *, unsafe_name: str | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("GPT-SoVITS-fixture/api_v2.py", "print('fixture')\n")
        package.writestr("GPT-SoVITS-fixture/requirements.txt", "fastapi\n")
        package.writestr("GPT-SoVITS-fixture/extra-req.txt", "\n")
        if unsafe_name:
            package.writestr(unsafe_name, "escape")
    return path


def installer_config(tmp_path: Path, archive: Path, **overrides: object) -> InstallerConfig:
    values = {
        "assets_root": tmp_path / "assets",
        "data_root": tmp_path / "data",
        "cache_root": tmp_path / "cache",
        "device": "cpu",
        "source_archive": archive,
        "skip_dependencies": True,
        "minimum_free_bytes": 0,
    }
    values.update(overrides)
    return InstallerConfig(**values)  # type: ignore[arg-type]


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def windows_pid_is_alive(pid: int) -> bool:
    if sys.platform != "win32":
        raise RuntimeError("Windows-only PID probe")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x00100000, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x00000102
    finally:
        kernel32.CloseHandle(handle)


def terminate_windows_tree(pid: int) -> None:
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_local_fixture_install_activates_version_and_pointer(tmp_path: Path) -> None:
    archive = make_runtime_archive(tmp_path / "runtime.zip")
    config = installer_config(tmp_path, archive)

    result = RuntimeInstaller(config).run()

    source = config.version_root / "GPT-SoVITS"
    assert (source / "api_v2.py").is_file()
    assert result["runtimeRoot"] == str(source)
    assert result["python"] == sys.executable
    assert read_json(config.current_path)["commit"] == RUNTIME_COMMIT
    state = read_json(config.state_path)
    assert state["status"] == "completed"
    assert state["progress"] == 1.0
    assert not list(config.runtime_root.glob(".staging-*"))


def test_local_runtime_source_installs_environment_without_copying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "complete-pack" / "runtime" / "GPT-SoVITS"
    source.mkdir(parents=True)
    for name in ("api_v2.py", "requirements.txt", "extra-req.txt"):
        (source / name).write_text("# fixture\n", encoding="utf-8")
    config = InstallerConfig(
        assets_root=tmp_path / "assets",
        data_root=tmp_path / "data",
        cache_root=tmp_path / "cache",
        device="cu126",
        source_runtime_root=source,
        minimum_free_bytes=0,
    )

    def fake_install(_installer: RuntimeInstaller, _source: Path, python_root: Path) -> Path:
        python = python_root / "Scripts" / "python.exe"
        python.parent.mkdir(parents=True)
        python.write_bytes(b"fixture-python")
        return python

    monkeypatch.setattr(RuntimeInstaller, "install_dependencies", fake_install)
    monkeypatch.setattr(
        RuntimeInstaller,
        "acquire_archive",
        lambda _installer: pytest.fail("local bundle must not download a runtime archive"),
    )

    result = RuntimeInstaller(config).run()

    assert result["sourceType"] == "local-bundle"
    assert result["runtimeRoot"] == str(source.resolve())
    assert Path(result["python"]).is_file()
    assert Path(result["python"]).is_relative_to(config.runtime_root / "environments")
    assert (source / "api_v2.py").is_file()
    assert not (config.version_root / "GPT-SoVITS").exists()


def test_install_dependencies_uses_opencc_wheel_and_keeps_torchmetrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "GPT-SoVITS"
    source.mkdir()
    (source / "requirements.txt").write_text(
        "--no-binary=opencc\n"
        "torch\n"
        "torchaudio\n"
        "onnxruntime-gpu\n"
        "opencc\n"
        "torchmetrics<=1.5\n"
        "fastapi>=0.115\n",
        encoding="utf-8",
    )
    (source / "extra-req.txt").write_text("faster-whisper\n", encoding="utf-8")
    config = InstallerConfig(
        assets_root=tmp_path / "assets",
        data_root=tmp_path / "data",
        cache_root=tmp_path / "cache",
        device="cu126",
        source_runtime_root=source,
        minimum_free_bytes=0,
    )
    installer = RuntimeInstaller(config)
    installer.staging_root = tmp_path / "staging"
    installer.staging_root.mkdir()
    commands: list[tuple[str, list[str]]] = []
    events: list[str] = []

    def fake_create(_builder: venv.EnvBuilder, python_root: Path) -> None:
        python = python_root / "Scripts" / "python.exe"
        python.parent.mkdir(parents=True)
        python.write_bytes(b"fixture-python")

    def fake_run_command(
        _installer: RuntimeInstaller, command: list[str], step: str, _progress: float
    ) -> None:
        commands.append((step, command))
        events.append(step)

    def fake_install_nltk_data(_installer: RuntimeInstaller, python_root: Path) -> None:
        assert python_root == installer.staging_root / "python"
        events.append("installing_nltk_data")

    monkeypatch.setattr(runtime_installer.venv.EnvBuilder, "create", fake_create)
    monkeypatch.setattr(RuntimeInstaller, "run_command", fake_run_command)
    monkeypatch.setattr(RuntimeInstaller, "install_nltk_data", fake_install_nltk_data)

    installer.install_dependencies(source, installer.staging_root / "python")

    opencc_command = next(command for step, command in commands if step == "installing_opencc")
    assert opencc_command[-2:] == ["--only-binary=opencc", "opencc"]
    filtered = (installer.staging_root / "requirements.fantareal.txt").read_text(encoding="utf-8")
    assert filtered == "torchmetrics<=1.5\nfastapi>=0.115\n"
    assert events.index("installing_runtime_requirements") < events.index("installing_nltk_data")
    assert events.index("installing_nltk_data") < events.index("verifying_environment")
    verification = next(command for step, command in commands if step == "verifying_environment")
    assert "corpora/cmudict" in verification[-1]
    assert "taggers/averaged_perceptron_tagger_eng" in verification[-1]


def test_install_nltk_data_extracts_required_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "nltk_data.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("nltk_data/corpora/cmudict/cmudict", "fixture")
        package.writestr(
            "nltk_data/taggers/averaged_perceptron_tagger/averaged_perceptron_tagger.pickle",
            "fixture",
        )
        package.writestr(
            "nltk_data/taggers/averaged_perceptron_tagger_eng/"
            "averaged_perceptron_tagger_eng.weights.json",
            "{}",
        )
    config = InstallerConfig(
        assets_root=tmp_path / "assets",
        data_root=tmp_path / "data",
        cache_root=tmp_path / "cache",
        device="cpu",
        minimum_free_bytes=0,
    )
    installer = RuntimeInstaller(config)
    installer.staging_root = tmp_path / "staging"
    installer.staging_root.mkdir()
    python_root = installer.staging_root / "python"
    python_root.mkdir()
    monkeypatch.setattr(installer, "acquire_nltk_data_archive", lambda: archive)

    installer.install_nltk_data(python_root)

    assert (python_root / "nltk_data" / "corpora" / "cmudict" / "cmudict").is_file()
    assert (
        python_root
        / "nltk_data"
        / "taggers"
        / "averaged_perceptron_tagger_eng"
        / "averaged_perceptron_tagger_eng.weights.json"
    ).is_file()


def test_run_command_appends_subprocess_output_to_install_log(tmp_path: Path) -> None:
    config = InstallerConfig(
        assets_root=tmp_path / "assets",
        data_root=tmp_path / "data",
        cache_root=tmp_path / "cache",
        device="cpu",
        minimum_free_bytes=0,
    )
    config.data_root.mkdir(parents=True)
    installer = RuntimeInstaller(config)
    installer.staging_root = tmp_path / "staging"
    installer.staging_root.mkdir()

    with pytest.raises(InstallFailure, match="installing_runtime_requirements"):
        installer.run_command(
            [
                sys.executable,
                "-c",
                "raise SystemExit(print('ERROR: Failed building wheel for opencc') or 1)",
            ],
            "installing_runtime_requirements",
            0.8,
        )

    assert (config.data_root / "runtime-install.log").read_text(encoding="utf-8") == (
        "ERROR: Failed building wheel for opencc\n"
    )


@pytest.mark.parametrize(
    "unsafe_name",
    ["GPT-SoVITS-fixture/../escape.txt", "GPT-SoVITS-fixture/link/../../escape.txt"],
)
def test_unsafe_archive_path_is_rejected(tmp_path: Path, unsafe_name: str) -> None:
    archive = make_runtime_archive(tmp_path / "unsafe.zip", unsafe_name=unsafe_name)
    config = installer_config(tmp_path, archive)

    with pytest.raises(InstallFailure, match="unsafe path"):
        RuntimeInstaller(config).run()

    assert not config.current_path.exists()
    assert read_json(config.state_path)["status"] == "failed"


def test_missing_runtime_entrypoint_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "malformed.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("runtime/requirements.txt", "fastapi\n")
        package.writestr("runtime/extra-req.txt", "\n")
    config = installer_config(tmp_path, archive)

    with pytest.raises(InstallFailure, match=r"missing api_v2\.py"):
        RuntimeInstaller(config).run()

    assert not config.current_path.exists()


def test_pointer_write_failure_restores_previous_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = make_runtime_archive(tmp_path / "runtime.zip")
    config = installer_config(tmp_path, archive)
    config.version_root.mkdir(parents=True)
    old_source = config.version_root / "GPT-SoVITS"
    old_source.mkdir()
    (old_source / "old-runtime.txt").write_text("keep", encoding="utf-8")
    previous = {"commit": RUNTIME_COMMIT, "runtimeRoot": str(old_source), "python": "old-python"}
    runtime_installer.atomic_write_json(config.current_path, previous)
    real_atomic_write = runtime_installer.atomic_write_json

    def fail_current_pointer(path: Path, value: object) -> None:
        if path == config.current_path:
            raise OSError("simulated pointer failure")
        real_atomic_write(path, value)

    monkeypatch.setattr(runtime_installer, "atomic_write_json", fail_current_pointer)

    with pytest.raises(OSError, match="simulated pointer failure"):
        RuntimeInstaller(config).run()

    assert (config.version_root / "GPT-SoVITS" / "old-runtime.txt").read_text() == "keep"
    assert read_json(config.current_path) == previous


def test_insufficient_disk_space_fails_before_archive_use(tmp_path: Path) -> None:
    archive = make_runtime_archive(tmp_path / "runtime.zip")
    config = installer_config(tmp_path, archive, minimum_free_bytes=2**63)

    with pytest.raises(InstallFailure, match="insufficient disk space"):
        RuntimeInstaller(config).run()

    state = read_json(config.state_path)
    assert state["status"] == "failed"
    assert not config.current_path.exists()


def test_cancelled_download_removes_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = installer_config(tmp_path, tmp_path / "unused.zip", source_archive=None)
    installer = RuntimeInstaller(config)

    class CancellingResponse:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

        def __enter__(self) -> CancellingResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _size: int) -> bytes:
            installer.request_cancel()
            return b"partial"

    monkeypatch.setattr(
        runtime_installer.urllib.request,
        "urlopen",
        lambda *_args, **_kw: CancellingResponse(),
    )

    with pytest.raises(InstallFailure, match="cancelled"):
        installer.run()

    assert not list(config.downloads_root.glob("*.partial"))


def test_install_state_keeps_stable_install_identity_and_activity_times(tmp_path: Path) -> None:
    archive = make_runtime_archive(tmp_path / "runtime.zip")
    install_id = "e" * 32
    config = installer_config(tmp_path, archive, install_id=install_id)

    RuntimeInstaller(config).run()

    state = read_json(config.state_path)
    assert state["installId"] == install_id
    assert state["startedAt"]
    assert state["heartbeatAt"]
    assert state["lastOutputAt"]
    assert state["stagingRoot"] == str(config.runtime_root / f".staging-{install_id}")


def test_cancel_control_file_must_match_install_identity(tmp_path: Path) -> None:
    archive = make_runtime_archive(tmp_path / "runtime.zip")
    install_id = "f" * 32
    config = installer_config(tmp_path, archive, install_id=install_id)
    installer = RuntimeInstaller(config)
    config.data_root.mkdir(parents=True)
    config.cancel_path.parent.mkdir(parents=True)

    config.cancel_path.write_text(json.dumps({"installId": "0" * 32}), encoding="utf-8")
    installer.check_cancelled()

    config.cancel_path.write_text(json.dumps({"installId": install_id}), encoding="utf-8")
    with pytest.raises(InstallFailure, match="cancelled"):
        installer.check_cancelled()


def test_install_id_bound_cancel_finishes_with_terminal_state_and_cleans_own_staging(
    tmp_path: Path,
) -> None:
    archive = make_runtime_archive(tmp_path / "runtime.zip")
    config = installer_config(tmp_path, archive, install_id="0" * 32)
    installer = RuntimeInstaller(config)
    config.cancel_path.parent.mkdir(parents=True)
    config.cancel_path.write_text(
        json.dumps(
            {
                "installId": config.install_id,
                "ownerToken": installer._owner_token,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(InstallFailure, match="cancelled"):
        installer.run()

    assert read_json(config.state_path)["status"] == "cancelled"
    assert not config.staging_root.exists()
    assert not config.cancel_path.exists()


def test_heartbeat_advances_while_installer_is_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = make_runtime_archive(tmp_path / "runtime.zip")
    config = installer_config(tmp_path, archive, install_id="1" * 32)
    installer = RuntimeInstaller(config)
    installer.staging_root = config.staging_root
    installer.started_at = "2026-08-10T00:00:00Z"
    installer.last_output_at = installer.started_at
    ticks = iter(range(1, 100))
    monkeypatch.setattr(runtime_installer, "HEARTBEAT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(
        runtime_installer,
        "utc_now",
        lambda: f"2026-08-10T00:00:{next(ticks):02d}Z",
    )
    installer.write_state("installing_torch", progress=0.62)
    first = read_json(config.state_path)["heartbeatAt"]

    installer._start_heartbeat()
    try:
        deadline = time.monotonic() + 1.0
        while read_json(config.state_path)["heartbeatAt"] == first:
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        installer._stop_heartbeat()

    assert read_json(config.state_path)["heartbeatAt"] != first


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process-tree ownership regression")
def test_cooperative_cancel_terminates_only_the_owned_command_tree(tmp_path: Path) -> None:
    config = InstallerConfig(
        assets_root=tmp_path / "assets",
        data_root=tmp_path / "data",
        cache_root=tmp_path / "cache",
        device="cpu",
        minimum_free_bytes=0,
        install_id="2" * 32,
    )
    installer = RuntimeInstaller(config)
    installer.staging_root = config.staging_root
    installer.staging_root.mkdir(parents=True)
    config.cancel_path.parent.mkdir(parents=True)
    pid_path = tmp_path / "owned-pids.json"
    helper = (
        "import json, os, pathlib, subprocess, sys, time; "
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "pathlib.Path(sys.argv[1]).write_text(json.dumps([os.getpid(), child.pid])); "
        "time.sleep(30)"
    )

    def request_cancel() -> None:
        deadline = time.monotonic() + 5.0
        while not pid_path.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        if pid_path.is_file():
            config.cancel_path.write_text(
                json.dumps({"installId": config.install_id}), encoding="utf-8"
            )

    requester = threading.Thread(target=request_cancel, daemon=True)
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    requester.start()
    owned_pids: list[int] = []
    try:
        with pytest.raises(InstallFailure, match="cancelled"):
            installer.run_command(
                [sys.executable, "-c", helper, str(pid_path)],
                "installing_torch",
                0.62,
            )
        requester.join(timeout=5)
        owned_pids = json.loads(pid_path.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 5.0
        while any(windows_pid_is_alive(pid) for pid in owned_pids) and time.monotonic() < deadline:
            time.sleep(0.05)
        for pid in owned_pids:
            assert not windows_pid_is_alive(pid)
        assert unrelated.poll() is None
    finally:
        for pid in owned_pids:
            terminate_windows_tree(pid)
        if unrelated.poll() is None:
            unrelated.terminate()
            try:
                unrelated.wait(timeout=5)
            except subprocess.TimeoutExpired:
                unrelated.kill()
                unrelated.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object regression")
def test_killing_installer_process_closes_job_and_terminates_command_tree(tmp_path: Path) -> None:
    pid_path = tmp_path / "job-owned-pids.json"
    staging = tmp_path / "staging"
    staging.mkdir()
    data_root = tmp_path / "data"
    data_root.mkdir()
    child_script = (
        "import json, os, pathlib, subprocess, sys, time; "
        "grandchild=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "pathlib.Path(sys.argv[1]).write_text(json.dumps([os.getpid(), grandchild.pid])); "
        "time.sleep(30)"
    )
    installer_script = (
        "import contextlib, sys; "
        "from pathlib import Path; "
        "from fantareal_tts_studio.runtime_installer import InstallerConfig, RuntimeInstaller; "
        "config=InstallerConfig(assets_root=Path(sys.argv[1]), data_root=Path(sys.argv[2]), "
        "cache_root=Path(sys.argv[3]), device='cpu', install_id='4'*32, "
        "supervise_process_tree=True); "
        "installer=RuntimeInstaller(config); installer.staging_root=Path(sys.argv[4]); "
        "guard=(installer.process_supervision() if hasattr(installer, 'process_supervision') "
        "else contextlib.nullcontext()); "
        "guard.__enter__(); "
        "installer.run_command([sys.executable, '-c', sys.argv[5], sys.argv[6]], "
        "'installing_torch', 0.62)"
    )
    environment = os.environ.copy()
    source_root = str(Path(__file__).parents[1] / "src")
    environment["PYTHONPATH"] = source_root
    installer_process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            installer_script,
            str(tmp_path / "assets"),
            str(data_root),
            str(tmp_path / "cache"),
            str(staging),
            child_script,
            str(pid_path),
        ],
        env=environment,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    owned_pids: list[int] = []
    try:
        deadline = time.monotonic() + 10.0
        while not pid_path.is_file() and installer_process.poll() is None:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert installer_process.poll() is None
        owned_pids = json.loads(pid_path.read_text(encoding="utf-8"))

        installer_process.kill()
        installer_process.wait(timeout=5)
        deadline = time.monotonic() + 5.0
        while any(windows_pid_is_alive(pid) for pid in owned_pids) and time.monotonic() < deadline:
            time.sleep(0.05)

        assert all(not windows_pid_is_alive(pid) for pid in owned_pids)
    finally:
        if installer_process.poll() is None:
            installer_process.kill()
            installer_process.wait(timeout=5)
        for pid in owned_pids:
            terminate_windows_tree(pid)
