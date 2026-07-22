from __future__ import annotations

import json
import subprocess
import sys
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


def test_run_command_appends_subprocess_output_to_install_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        output = kwargs.get("stdout")
        assert output is not None
        output.write(b"ERROR: Failed building wheel for opencc\n")  # type: ignore[union-attr]
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(runtime_installer.subprocess, "run", fake_run)

    with pytest.raises(InstallFailure, match="installing_runtime_requirements"):
        installer.run_command(["python.exe", "-m", "pip"], "installing_runtime_requirements", 0.8)

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
