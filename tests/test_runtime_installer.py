from __future__ import annotations

import json
import sys
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
