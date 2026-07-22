from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import urllib.error
import urllib.request
import venv
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

RUNTIME_VERSION = "20250606v2pro"
RUNTIME_COMMIT = "d7c2210da8c013e81a94bfc7b811a477c99fd506"
RUNTIME_ARCHIVE_URL = "https://codeload.github.com/RVC-Boss/GPT-SoVITS/zip/" + RUNTIME_COMMIT
NLTK_DATA_URLS = (
    "https://hf-mirror.com/XXXXRT/GPT-SoVITS-Pretrained/resolve/main/nltk_data.zip",
    "https://huggingface.co/XXXXRT/GPT-SoVITS-Pretrained/resolve/main/nltk_data.zip",
)
NLTK_DATA_SHA256 = "eb3ec26ace3f9ccbb08a6d333e26f0941c47e230ece0717dc992bdb7e99808dd"
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_EXTRACTED_BYTES = 8 * 1024 * 1024 * 1024
MAX_NLTK_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_NLTK_EXTRACTED_BYTES = 64 * 1024 * 1024
REQUIRED_NLTK_FILES = (
    Path("corpora/cmudict/cmudict"),
    Path("taggers/averaged_perceptron_tagger/averaged_perceptron_tagger.pickle"),
    Path("taggers/averaged_perceptron_tagger_eng/averaged_perceptron_tagger_eng.weights.json"),
)
MIN_FREE_BYTES = {
    "cpu": 15 * 1024 * 1024 * 1024,
    "cu126": 25 * 1024 * 1024 * 1024,
    "cu128": 25 * 1024 * 1024 * 1024,
}
LOCAL_MIN_FREE_BYTES = {
    "cpu": 8 * 1024 * 1024 * 1024,
    "cu126": 10 * 1024 * 1024 * 1024,
    "cu128": 10 * 1024 * 1024 * 1024,
}
TORCH_INDEX_URLS = {
    "cpu": "https://download.pytorch.org/whl/cpu",
    "cu126": "https://download.pytorch.org/whl/cu126",
    "cu128": "https://download.pytorch.org/whl/cu128",
}


class InstallFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class InstallerConfig:
    assets_root: Path
    data_root: Path
    cache_root: Path
    device: str
    source_runtime_root: Path | None = None
    source_archive: Path | None = None
    skip_dependencies: bool = False
    minimum_free_bytes: int | None = None

    @property
    def runtime_root(self) -> Path:
        return self.assets_root / "runtime"

    @property
    def versions_root(self) -> Path:
        return self.runtime_root / "versions"

    @property
    def version_root(self) -> Path:
        return self.versions_root / RUNTIME_COMMIT

    @property
    def state_path(self) -> Path:
        return self.data_root / "runtime-install-state.json"

    @property
    def current_path(self) -> Path:
        return self.runtime_root / "current.json"

    @property
    def downloads_root(self) -> Path:
        return self.cache_root / "runtime-downloads"

    @property
    def environments_root(self) -> Path:
        return self.runtime_root / "environments"

    def local_runtime_key(self) -> str:
        if self.source_runtime_root is None:
            raise InstallFailure("local runtime source is not configured")
        source = self.source_runtime_root.resolve(strict=True)
        digest = hashlib.sha256(str(source).casefold().encode("utf-8"))
        for filename in ("api_v2.py", "requirements.txt", "extra-req.txt"):
            path = source / filename
            digest.update(filename.encode("ascii"))
            digest.update(path.read_bytes())
        return digest.hexdigest()[:16]

    @property
    def local_environment_root(self) -> Path:
        return self.environments_root / f"{self.local_runtime_key()}-{self.device}"


class RuntimeInstaller:
    def __init__(self, config: InstallerConfig) -> None:
        self.config = config
        self.cancelled = False
        self.staging_root: Path | None = None

    def request_cancel(self, *_args: object) -> None:
        self.cancelled = True

    def run(self) -> dict[str, Any]:
        config = self.config
        for root in (config.assets_root, config.data_root, config.cache_root):
            root.mkdir(parents=True, exist_ok=True)
        if config.source_runtime_root is None:
            config.versions_root.mkdir(parents=True, exist_ok=True)
            config.downloads_root.mkdir(parents=True, exist_ok=True)
        else:
            config.environments_root.mkdir(parents=True, exist_ok=True)
        self.staging_root = config.runtime_root / f".staging-{uuid4().hex}"
        self.staging_root.mkdir(parents=True, exist_ok=False)
        self.write_state("checking_space", progress=0.02)
        try:
            self.check_cancelled()
            self.check_disk_space()
            if config.source_runtime_root is None:
                archive = self.acquire_archive()
                self.check_cancelled()
                source_root = self.extract_archive(archive)
            else:
                source_root = config.source_runtime_root.resolve(strict=True)
                self.write_state(
                    "using_local_bundle",
                    progress=0.48,
                    sourceType="local-bundle",
                    runtimeRoot=str(source_root),
                )
            self.check_runtime_source(source_root)
            python_root = self.staging_root / "python"
            if config.skip_dependencies:
                self.write_state("skipping_dependencies", progress=0.82)
                python_executable = Path(sys.executable)
            else:
                python_executable = self.install_dependencies(source_root, python_root)
            self.check_cancelled()
            result = (
                self.activate_local(source_root, python_executable)
                if config.source_runtime_root is not None
                else self.activate(source_root, python_executable)
            )
            self.write_state("completed", progress=1.0, status="completed", result=result)
            return result
        except Exception as exc:
            status = "cancelled" if self.cancelled else "failed"
            self.write_state(status, progress=0.0, status=status, error=str(exc))
            raise
        finally:
            if self.staging_root is not None:
                shutil.rmtree(self.staging_root, ignore_errors=True)

    def check_disk_space(self) -> None:
        defaults = (
            LOCAL_MIN_FREE_BYTES if self.config.source_runtime_root is not None else MIN_FREE_BYTES
        )
        minimum = (
            self.config.minimum_free_bytes
            if self.config.minimum_free_bytes is not None
            else defaults[self.config.device]
        )
        free = shutil.disk_usage(self.config.assets_root).free
        if free < minimum:
            raise InstallFailure(
                f"insufficient disk space: requires {minimum} bytes, only {free} bytes free"
            )
        self.write_state(
            "checking_space",
            progress=0.04,
            disk={"requiredBytes": minimum, "freeBytes": free},
        )

    def acquire_archive(self) -> Path:
        if self.config.source_archive is not None:
            archive = self.config.source_archive.resolve(strict=True)
            if not archive.is_file() or archive.suffix.lower() != ".zip":
                raise InstallFailure("test source archive must be a zip file")
            self.write_state("using_local_fixture", progress=0.12)
            return archive
        archive = self.config.downloads_root / f"gpt-sovits-{RUNTIME_COMMIT}.zip"
        partial = archive.with_suffix(".zip.partial")
        if archive.is_file() and 0 < archive.stat().st_size <= MAX_ARCHIVE_BYTES:
            self.write_state("using_cached_archive", progress=0.28)
            return archive
        partial.unlink(missing_ok=True)
        self.write_state("downloading", progress=0.06, bytesDownloaded=0)
        request = urllib.request.Request(
            RUNTIME_ARCHIVE_URL,
            headers={"Accept": "application/zip", "User-Agent": "Fantareal-TTS-Studio/0.1"},
        )
        try:
            with (
                urllib.request.urlopen(request, timeout=60) as response,
                partial.open("xb") as output,
            ):
                total = int(response.headers.get("Content-Length") or 0)
                downloaded = 0
                while True:
                    self.check_cancelled()
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > MAX_ARCHIVE_BYTES:
                        raise InstallFailure("runtime archive exceeds the 2 GiB limit")
                    output.write(chunk)
                    if downloaded % (8 * 1024 * 1024) < len(chunk):
                        fraction = downloaded / total if total else 0.0
                        self.write_state(
                            "downloading",
                            progress=min(0.27, 0.06 + fraction * 0.21),
                            bytesDownloaded=downloaded,
                            bytesTotal=total,
                        )
        except InstallFailure:
            partial.unlink(missing_ok=True)
            raise
        except (OSError, urllib.error.HTTPError, urllib.error.URLError) as exc:
            partial.unlink(missing_ok=True)
            raise InstallFailure(f"runtime download failed: {exc}") from exc
        os.replace(partial, archive)
        self.write_state(
            "downloaded",
            progress=0.28,
            bytesDownloaded=archive.stat().st_size,
        )
        return archive

    def extract_archive(self, archive: Path) -> Path:
        if self.staging_root is None:
            raise InstallFailure("staging root is unavailable")
        extract_root = self.staging_root / "source"
        extract_root.mkdir(parents=True)
        self.write_state("extracting", progress=0.3)
        total = 0
        with zipfile.ZipFile(archive) as package:
            entries = [item for item in package.infolist() if not item.is_dir()]
            prefixes = {
                Path(item.filename).parts[0] for item in entries if Path(item.filename).parts
            }
            strip_prefix = next(iter(prefixes)) if len(prefixes) == 1 else ""
            for index, item in enumerate(entries):
                self.check_cancelled()
                pure = Path(item.filename.replace("\\", "/"))
                parts = (
                    pure.parts[1:] if strip_prefix and pure.parts[0] == strip_prefix else pure.parts
                )
                if not parts:
                    continue
                if pure.is_absolute() or ".." in parts:
                    raise InstallFailure("runtime archive contains an unsafe path")
                mode = item.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise InstallFailure("runtime archive contains a symbolic link")
                total += item.file_size
                if total > MAX_EXTRACTED_BYTES:
                    raise InstallFailure("runtime archive expands beyond the 8 GiB limit")
                target = extract_root.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.open(item) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                if index % 100 == 0:
                    fraction = (index + 1) / max(1, len(entries))
                    self.write_state("extracting", progress=0.3 + fraction * 0.18)
        self.write_state("extracted", progress=0.48, extractedBytes=total)
        return extract_root

    @staticmethod
    def check_runtime_source(source_root: Path) -> None:
        for relative in ("api_v2.py", "requirements.txt", "extra-req.txt"):
            path = source_root / relative
            if not path.is_file() or path.is_symlink():
                raise InstallFailure(f"runtime source is missing {relative}")

    def install_dependencies(self, source_root: Path, python_root: Path) -> Path:
        self.write_state("creating_environment", progress=0.5)
        venv.EnvBuilder(with_pip=True, clear=True).create(python_root)
        python_executable = python_root / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        self.run_command(
            [
                str(python_executable),
                "-m",
                "pip",
                "install",
                "--upgrade",
                "pip",
                "wheel",
                "setuptools<82",
            ],
            "installing_build_tools",
            0.56,
        )
        self.run_command(
            [
                str(python_executable),
                "-m",
                "pip",
                "install",
                "torch",
                "torchaudio",
                "--index-url",
                TORCH_INDEX_URLS[self.config.device],
            ],
            "installing_torch",
            0.62,
        )
        filtered = self.staging_root / "requirements.fantareal.txt"  # type: ignore[operator]
        lines = []
        for raw in (source_root / "requirements.txt").read_text(encoding="utf-8").splitlines():
            stripped = raw.strip().lower()
            if stripped.startswith(("--no-binary", "--only-binary")) and "opencc" in stripped:
                continue
            requirement = stripped.split(";", 1)[0].strip()
            package = re.split(r"[<>=!~\[\s]", requirement, maxsplit=1)[0]
            package = re.sub(r"[-_.]+", "-", package)
            if package in {"torch", "torchaudio", "onnxruntime", "onnxruntime-gpu", "opencc"}:
                continue
            lines.append(raw)
        filtered.write_text("\n".join(lines) + "\n", encoding="utf-8")
        onnx = "onnxruntime" if self.config.device == "cpu" else "onnxruntime-gpu"
        self.run_command(
            [str(python_executable), "-m", "pip", "install", onnx],
            "installing_onnx",
            0.7,
        )
        self.run_command(
            [
                str(python_executable),
                "-m",
                "pip",
                "install",
                "--only-binary=opencc",
                "opencc",
            ],
            "installing_opencc",
            0.72,
        )
        self.run_command(
            [
                str(python_executable),
                "-m",
                "pip",
                "install",
                "-r",
                str(source_root / "extra-req.txt"),
                "--no-deps",
            ],
            "installing_extra_requirements",
            0.75,
        )
        self.run_command(
            [str(python_executable), "-m", "pip", "install", "-r", str(filtered)],
            "installing_runtime_requirements",
            0.8,
        )
        self.install_nltk_data(python_root)
        self.run_command(
            [
                str(python_executable),
                "-c",
                "import fastapi, nltk.data, torch; "
                "nltk.data.find('corpora/cmudict'); "
                "nltk.data.find('taggers/averaged_perceptron_tagger'); "
                "nltk.data.find('taggers/averaged_perceptron_tagger_eng'); "
                "print(torch.__version__)",
            ],
            "verifying_environment",
            0.9,
        )
        return python_executable

    def acquire_nltk_data_archive(self) -> Path:
        downloads_root = self.config.downloads_root
        downloads_root.mkdir(parents=True, exist_ok=True)
        archive = downloads_root / f"nltk-data-{NLTK_DATA_SHA256[:12]}.zip"
        if archive.is_file():
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            if digest == NLTK_DATA_SHA256 and archive.stat().st_size <= MAX_NLTK_ARCHIVE_BYTES:
                return archive
            archive.unlink(missing_ok=True)

        partial = archive.with_suffix(".zip.partial")
        last_error: Exception | None = None
        for url in NLTK_DATA_URLS:
            partial.unlink(missing_ok=True)
            self.write_state("downloading_nltk_data", progress=0.83)
            request = urllib.request.Request(
                url,
                headers={"Accept": "application/zip", "User-Agent": "Fantareal-TTS-Studio/0.2"},
            )
            try:
                digest = hashlib.sha256()
                downloaded = 0
                with (
                    urllib.request.urlopen(request, timeout=60) as response,
                    partial.open("xb") as output,
                ):
                    while True:
                        self.check_cancelled()
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        downloaded += len(chunk)
                        if downloaded > MAX_NLTK_ARCHIVE_BYTES:
                            raise InstallFailure("NLTK data archive exceeds the 32 MiB limit")
                        digest.update(chunk)
                        output.write(chunk)
                if digest.hexdigest() != NLTK_DATA_SHA256:
                    raise InstallFailure("NLTK data archive checksum mismatch")
                os.replace(partial, archive)
                return archive
            except InstallFailure as exc:
                partial.unlink(missing_ok=True)
                last_error = exc
            except (OSError, urllib.error.HTTPError, urllib.error.URLError) as exc:
                partial.unlink(missing_ok=True)
                last_error = exc
        raise InstallFailure(f"NLTK data download failed: {last_error}")

    def install_nltk_data(self, python_root: Path) -> None:
        target = python_root / "nltk_data"
        if all((target / relative).is_file() for relative in REQUIRED_NLTK_FILES):
            self.write_state("nltk_ready", progress=0.88)
            return
        if self.staging_root is None:
            raise InstallFailure("staging root is unavailable")

        archive = self.acquire_nltk_data_archive()
        extraction_root = self.staging_root / "nltk-support"
        shutil.rmtree(extraction_root, ignore_errors=True)
        extraction_root.mkdir()
        total = 0
        with zipfile.ZipFile(archive) as package:
            for item in package.infolist():
                pure = Path(item.filename.replace("\\", "/"))
                parts = pure.parts
                if not parts:
                    continue
                if pure.is_absolute() or ".." in parts or parts[0] != "nltk_data":
                    raise InstallFailure("NLTK data archive contains an unsafe path")
                mode = item.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise InstallFailure("NLTK data archive contains a symbolic link")
                if item.is_dir():
                    continue
                total += item.file_size
                if total > MAX_NLTK_EXTRACTED_BYTES:
                    raise InstallFailure("NLTK data archive expands beyond the 64 MiB limit")
                destination = extraction_root.joinpath(*parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with package.open(item) as source, destination.open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)

        staged_data = extraction_root / "nltk_data"
        missing = [
            str(relative)
            for relative in REQUIRED_NLTK_FILES
            if not (staged_data / relative).is_file()
        ]
        if missing:
            detail = ", ".join(missing)
            raise InstallFailure(f"NLTK data archive is missing required resources: {detail}")
        shutil.rmtree(target, ignore_errors=True)
        os.replace(staged_data, target)
        self.write_state("nltk_ready", progress=0.88)

    def run_command(self, command: list[str], step: str, progress: float) -> None:
        self.check_cancelled()
        self.write_state(step, progress=progress, command=[Path(command[0]).name, *command[1:3]])
        log_path = self.config.data_root / "runtime-install.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as log_handle:
            completed = subprocess.run(
                command,
                cwd=str(self.staging_root),
                check=False,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        if completed.returncode != 0:
            raise InstallFailure(f"{step} failed with exit code {completed.returncode}")

    def activate(self, source_root: Path, python_executable: Path) -> dict[str, Any]:
        if self.staging_root is None:
            raise InstallFailure("staging root is unavailable")
        self.write_state("activating", progress=0.94)
        payload_root = self.staging_root / "payload"
        payload_root.mkdir()
        os.replace(source_root, payload_root / "GPT-SoVITS")
        staged_python = self.staging_root / "python"
        if staged_python.exists():
            os.replace(staged_python, payload_root / "python")
            relative_python = (
                "python/Scripts/python.exe" if os.name == "nt" else "python/bin/python"
            )
        else:
            relative_python = str(python_executable)
        target = self.config.version_root
        backup = target.with_name(f"{target.name}.backup-{uuid4().hex}")
        displaced = target.with_name(f"{target.name}.failed-{uuid4().hex}")
        if target.exists():
            os.replace(target, backup)
        try:
            os.replace(payload_root, target)
        except Exception:
            if backup.exists() and not target.exists():
                os.replace(backup, target)
            raise
        current = {
            "version": RUNTIME_VERSION,
            "commit": RUNTIME_COMMIT,
            "runtimeRoot": str(target / "GPT-SoVITS"),
            "python": (
                str(target / relative_python)
                if not Path(relative_python).is_absolute()
                else relative_python
            ),
            "device": self.config.device,
            "installedAt": utc_now(),
        }
        try:
            atomic_write_json(self.config.current_path, current)
        except Exception:
            if target.exists():
                os.replace(target, displaced)
            if backup.exists():
                os.replace(backup, target)
            shutil.rmtree(displaced, ignore_errors=True)
            raise
        shutil.rmtree(backup, ignore_errors=True)
        return current

    def activate_local(self, source_root: Path, python_executable: Path) -> dict[str, Any]:
        if self.staging_root is None:
            raise InstallFailure("staging root is unavailable")
        self.write_state("activating", progress=0.94, sourceType="local-bundle")
        target = self.config.local_environment_root
        backup = target.with_name(f"{target.name}.backup-{uuid4().hex}")
        displaced = target.with_name(f"{target.name}.failed-{uuid4().hex}")
        staged_python = self.staging_root / "python"
        installed_managed_environment = staged_python.exists()
        if installed_managed_environment:
            payload_root = self.staging_root / "environment"
            payload_root.mkdir()
            os.replace(staged_python, payload_root / "python")
            relative_python = (
                "python/Scripts/python.exe" if os.name == "nt" else "python/bin/python"
            )
            if target.exists():
                os.replace(target, backup)
            try:
                os.replace(payload_root, target)
            except Exception:
                if backup.exists() and not target.exists():
                    os.replace(backup, target)
                raise
            installed_python = target / relative_python
        else:
            installed_python = python_executable.resolve(strict=True)

        current = {
            "version": "local-bundle",
            "commit": "",
            "sourceType": "local-bundle",
            "runtimeKey": self.config.local_runtime_key(),
            "runtimeRoot": str(source_root.resolve(strict=True)),
            "python": str(installed_python),
            "device": self.config.device,
            "installedAt": utc_now(),
        }
        try:
            atomic_write_json(self.config.current_path, current)
        except Exception:
            if installed_managed_environment and target.exists():
                os.replace(target, displaced)
            if backup.exists():
                os.replace(backup, target)
            shutil.rmtree(displaced, ignore_errors=True)
            raise
        shutil.rmtree(backup, ignore_errors=True)
        return current

    def check_cancelled(self) -> None:
        if self.cancelled:
            raise InstallFailure("runtime installation cancelled")

    def write_state(
        self,
        step: str,
        *,
        progress: float,
        status: str = "running",
        error: str = "",
        result: dict[str, Any] | None = None,
        **extra: Any,
    ) -> None:
        payload = {
            "status": status,
            "step": step,
            "progress": max(0.0, min(1.0, progress)),
            "pid": os.getpid(),
            "version": RUNTIME_VERSION,
            "commit": RUNTIME_COMMIT,
            "device": self.config.device,
            "updatedAt": utc_now(),
            "error": error,
            **extra,
        }
        if result is not None:
            payload["result"] = result
        atomic_write_json(self.config.state_path, payload)


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def parse_args(argv: list[str] | None = None) -> InstallerConfig:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets-root", required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--cache-root", required=True)
    parser.add_argument("--device", choices=sorted(TORCH_INDEX_URLS), required=True)
    parser.add_argument("--source-runtime-root")
    args = parser.parse_args(argv)
    return InstallerConfig(
        assets_root=Path(args.assets_root).resolve(),
        data_root=Path(args.data_root).resolve(),
        cache_root=Path(args.cache_root).resolve(),
        device=args.device,
        source_runtime_root=(
            Path(args.source_runtime_root).resolve() if args.source_runtime_root else None
        ),
    )


def main(argv: list[str] | None = None) -> int:
    installer = RuntimeInstaller(parse_args(argv))
    signal.signal(signal.SIGINT, installer.request_cancel)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, installer.request_cancel)
    try:
        installer.run()
    except Exception as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
