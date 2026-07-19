from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO
from uuid import uuid4

EXTENSION_ID = "com.fantareal.tts-studio"
PROVIDER_ID = "gpt-sovits"
DEFAULT_API_URL = "http://127.0.0.1:9880"
MAX_TEXT_CHARS = 6000
MAX_MODEL_BYTES = 3 * 1024 * 1024 * 1024
MAX_AUDIO_BYTES = 200 * 1024 * 1024
MAX_HISTORY_ITEMS = 200
MAX_PREVIEW_TEXT_CHARS = 500
MAX_PREVIEW_AUDIO_BYTES = 6 * 1024 * 1024
RUNTIME_DEVICES = {"cpu", "cu126", "cu128"}
AUDIO_SUFFIXES = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac"}
ASSET_RULES = {
    "gpt": ({".ckpt"}, MAX_MODEL_BYTES),
    "sovits": ({".pth", ".pt"}, MAX_MODEL_BYTES),
    "audio": (AUDIO_SUFFIXES, MAX_AUDIO_BYTES),
}
FORMAT_MEDIA_TYPES = {
    "wav": "audio/wav",
    "ogg": "audio/ogg",
    "aac": "audio/aac",
}
DEFAULT_VOICE = {
    "id": "default",
    "name": "默认声线",
    "locale": "zh-CN",
    "gptWeights": "",
    "sovitsWeights": "",
    "referenceAudio": "",
    "promptText": "",
    "promptLanguage": "zh",
    "textLanguage": "zh",
}
DEFAULT_SETTINGS = {
    "apiUrl": DEFAULT_API_URL,
    "activeVoiceId": "default",
    "voices": [DEFAULT_VOICE],
    "audioFormat": "wav",
    "requestTimeoutSeconds": 180,
    "topK": 5,
    "topP": 1.0,
    "temperature": 1.0,
    "textSplitMethod": "cut5",
    "batchSize": 1,
    "speedFactor": 1.0,
    "sampleSteps": 32,
    "parallelInfer": True,
    "repetitionPenalty": 1.35,
    "runtimeDevice": "cpu",
}


class RpcFailure(Exception):
    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}


@dataclass(frozen=True)
class StorageLayout:
    workspace: Path
    settings: Path
    data: Path
    cache: Path
    assets: Path

    @classmethod
    def from_initialize(cls, params: dict[str, Any]) -> StorageLayout:
        workspace_raw = params.get("workspace")
        storage = params.get("storage")
        paths = storage.get("paths") if isinstance(storage, dict) else None
        if not isinstance(workspace_raw, str) or not isinstance(paths, dict):
            raise RpcFailure(-32602, "extension.initialize storage is invalid")
        values: dict[str, Path] = {"workspace": Path(workspace_raw).resolve()}
        for name in ("settings", "data", "cache", "assets"):
            raw = paths.get(name)
            if not isinstance(raw, str) or not raw:
                raise RpcFailure(-32003, f"storage.{name} permission is required")
            values[name] = Path(raw).resolve()
        layout = cls(**values)
        for root in (layout.workspace, layout.settings, layout.data, layout.cache, layout.assets):
            root.mkdir(parents=True, exist_ok=True)
        return layout


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def clamp_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def clamp_float(value: Any, minimum: float, maximum: float, default: float) -> float:
    try:
        return max(minimum, min(maximum, float(value)))
    except (TypeError, ValueError):
        return default


def safe_id(value: Any, fallback: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]", "-", str(value or "").strip()).strip("-.")
    return (cleaned[:80] or fallback).lower()


def safe_filename(value: Any, fallback: str) -> str:
    name = Path(str(value or "")).name
    stem = re.sub(r"[^a-zA-Z0-9._ -]", "_", Path(name).stem).strip(" ._") or fallback
    return f"{stem[:120]}{Path(name).suffix.lower()}"


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return json_copy(default)


def contained_existing_file(root: Path, relative_path: Any) -> Path:
    text = str(relative_path or "").strip().replace("\\", "/")
    if not text or Path(text).is_absolute() or ".." in Path(text).parts:
        raise RpcFailure(-32602, "path must be workspace-relative")
    candidate = root.joinpath(*Path(text).parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        raise RpcFailure(-32602, "path escapes the allowed root") from None
    if not resolved.is_file():
        raise RpcFailure(-32602, "path is not a file")
    return resolved


def asset_path(layout: StorageLayout, relative_path: Any, suffixes: set[str]) -> Path:
    path = contained_existing_file(layout.assets, relative_path)
    if path.suffix.lower() not in suffixes:
        raise RpcFailure(-32602, "asset type is not supported")
    return path


def sanitize_voice(raw: Any, index: int) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    voice_id = safe_id(source.get("id"), f"voice-{index + 1}")
    result = {
        "id": voice_id,
        "name": str(source.get("name") or voice_id).strip()[:120],
        "locale": str(source.get("locale") or "zh-CN").strip()[:32],
        "gptWeights": str(source.get("gptWeights") or "").strip().replace("\\", "/"),
        "sovitsWeights": str(source.get("sovitsWeights") or "").strip().replace("\\", "/"),
        "referenceAudio": str(source.get("referenceAudio") or "").strip().replace("\\", "/"),
        "promptText": str(source.get("promptText") or "").strip()[:4000],
        "promptLanguage": str(source.get("promptLanguage") or "zh").strip()[:16],
        "textLanguage": str(source.get("textLanguage") or "zh").strip()[:16],
    }
    for key in ("gptWeights", "sovitsWeights", "referenceAudio"):
        value = result[key]
        if value and (Path(value).is_absolute() or ".." in Path(value).parts):
            result[key] = ""
    return result


def sanitize_settings(raw: Any, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    base = json_copy(existing or DEFAULT_SETTINGS)
    parsed = urllib.parse.urlparse(str(source.get("apiUrl", base["apiUrl"])).strip())
    if parsed.scheme != "http" or (parsed.hostname or "").lower() not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        api_url = DEFAULT_API_URL
    else:
        api_url = urllib.parse.urlunparse(
            (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", "")
        )
    voices_raw = source.get("voices", base.get("voices"))
    voices = [sanitize_voice(item, index) for index, item in enumerate(voices_raw or [])][:64]
    if not voices:
        voices = [json_copy(DEFAULT_VOICE)]
    voice_ids = {item["id"] for item in voices}
    active = safe_id(source.get("activeVoiceId", base.get("activeVoiceId")), voices[0]["id"])
    if active not in voice_ids:
        active = voices[0]["id"]
    audio_format = str(source.get("audioFormat", base.get("audioFormat", "wav"))).lower()
    if audio_format not in FORMAT_MEDIA_TYPES:
        audio_format = "wav"
    return {
        "apiUrl": api_url,
        "activeVoiceId": active,
        "voices": voices,
        "audioFormat": audio_format,
        "requestTimeoutSeconds": clamp_int(
            source.get("requestTimeoutSeconds", base.get("requestTimeoutSeconds")), 5, 600, 180
        ),
        "topK": clamp_int(source.get("topK", base.get("topK")), 1, 100, 5),
        "topP": clamp_float(source.get("topP", base.get("topP")), 0.0, 1.0, 1.0),
        "temperature": clamp_float(
            source.get("temperature", base.get("temperature")), 0.0, 2.0, 1.0
        ),
        "textSplitMethod": str(source.get("textSplitMethod", base.get("textSplitMethod", "cut5")))[
            :32
        ],
        "batchSize": clamp_int(source.get("batchSize", base.get("batchSize")), 1, 200, 1),
        "speedFactor": clamp_float(
            source.get("speedFactor", base.get("speedFactor")), 0.25, 4.0, 1.0
        ),
        "sampleSteps": clamp_int(source.get("sampleSteps", base.get("sampleSteps")), 4, 64, 32),
        "parallelInfer": bool(source.get("parallelInfer", base.get("parallelInfer", True))),
        "repetitionPenalty": clamp_float(
            source.get("repetitionPenalty", base.get("repetitionPenalty")), 0.1, 2.0, 1.35
        ),
        "runtimeDevice": (
            str(source.get("runtimeDevice", base.get("runtimeDevice", "cpu"))).lower()
            if str(source.get("runtimeDevice", base.get("runtimeDevice", "cpu"))).lower()
            in RUNTIME_DEVICES
            else "cpu"
        ),
    }


class TtsStudioService:
    def __init__(self) -> None:
        self.layout: StorageLayout | None = None
        self.should_stop = False
        self.runtime_process: subprocess.Popen[bytes] | None = None
        self.installer_process: subprocess.Popen[bytes] | None = None
        self._cancel_events: dict[str, threading.Event] = {}
        self._cancel_lock = threading.Lock()

    @property
    def settings_path(self) -> Path:
        return self._require_layout().settings / "settings.json"

    @property
    def history_path(self) -> Path:
        return self._require_layout().data / "history.json"

    @property
    def runtime_install_state_path(self) -> Path:
        return self._require_layout().data / "runtime-install-state.json"

    @property
    def runtime_install_log_path(self) -> Path:
        return self._require_layout().data / "runtime-install.log"

    @property
    def runtime_log_path(self) -> Path:
        return self._require_layout().data / "runtime.log"

    @property
    def runtime_current_path(self) -> Path:
        return self._require_layout().assets / "runtime" / "current.json"

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        permissions = params.get("permissions")
        required = {"storage.settings", "storage.data", "storage.cache", "storage.assets"}
        if not isinstance(permissions, list) or not required.issubset(set(permissions)):
            raise RpcFailure(-32003, "required storage permissions were not granted")
        self.layout = StorageLayout.from_initialize(params)
        for relative in ("voices/gpt", "voices/sovits", "voices/audio", "runtime"):
            (self.layout.assets / relative).mkdir(parents=True, exist_ok=True)
        (self.layout.cache / "audio").mkdir(parents=True, exist_ok=True)
        if not self.settings_path.exists():
            atomic_write_json(self.settings_path, DEFAULT_SETTINGS)
        if not self.history_path.exists():
            atomic_write_json(self.history_path, [])
        return {
            "protocol": "fantareal.extension.v1",
            "extensionId": EXTENSION_ID,
            "providerId": PROVIDER_ID,
        }

    def get_settings(self) -> dict[str, Any]:
        current = read_json(self.settings_path, DEFAULT_SETTINGS)
        return sanitize_settings(current)

    def save_settings(self, raw: Any) -> dict[str, Any]:
        settings = sanitize_settings(raw, self.get_settings())
        atomic_write_json(self.settings_path, settings)
        return settings

    def read_history(self) -> list[dict[str, Any]]:
        value = read_json(self.history_path, [])
        return value[:MAX_HISTORY_ITEMS] if isinstance(value, list) else []

    def write_history(self, items: list[dict[str, Any]]) -> None:
        atomic_write_json(self.history_path, items[:MAX_HISTORY_ITEMS])

    def discover_assets(self) -> dict[str, list[str]]:
        layout = self._require_layout()
        result: dict[str, list[str]] = {}
        for kind, (suffixes, _) in ASSET_RULES.items():
            root = layout.assets / "voices" / kind
            result[kind] = [
                path.relative_to(layout.assets).as_posix()
                for path in sorted(root.iterdir(), key=lambda item: item.name.lower())
                if path.is_file() and path.suffix.lower() in suffixes and not path.is_symlink()
            ][:300]
        return result

    def import_asset(self, params: dict[str, Any]) -> dict[str, Any]:
        layout = self._require_layout()
        kind = str(params.get("kind") or "")
        if kind not in ASSET_RULES:
            raise RpcFailure(-32602, "asset kind is invalid")
        suffixes, size_limit = ASSET_RULES[kind]
        source = contained_existing_file(layout.workspace, params.get("path"))
        if source.suffix.lower() not in suffixes:
            raise RpcFailure(-32602, "asset type is not supported")
        size = source.stat().st_size
        if size > size_limit:
            raise RpcFailure(
                -32011, "asset exceeds the size limit", {"size": size, "limit": size_limit}
            )
        target_dir = layout.assets / "voices" / kind
        target_name = safe_filename(params.get("name") or source.name, kind)
        target = target_dir / target_name
        if target.exists():
            target = target_dir / f"{target.stem}-{uuid4().hex[:8]}{target.suffix}"
        with source.open("rb") as reader, target.open("xb") as writer:
            shutil.copyfileobj(reader, writer, 1024 * 1024)
        return {
            "kind": kind,
            "path": target.relative_to(layout.assets).as_posix(),
            "name": target.name,
            "size": size,
        }

    def list_voices(self) -> dict[str, Any]:
        settings = self.get_settings()
        voices = [
            {"id": voice["id"], "name": voice["name"], "locale": voice["locale"]}
            for voice in settings["voices"]
        ]
        return {"activeVoiceId": settings["activeVoiceId"], "voices": voices}

    def probe(self) -> dict[str, Any]:
        settings = self.get_settings()
        try:
            payload = self._request_json(f"{settings['apiUrl']}/openapi.json", timeout=2.0)
            paths = payload.get("paths") if isinstance(payload, dict) else None
            available = isinstance(paths, dict) and "/tts" in paths
            return {
                "available": available,
                "message": "GPT-SoVITS API ready" if available else "GPT-SoVITS /tts not found",
                "apiUrl": settings["apiUrl"],
            }
        except RpcFailure as exc:
            return {"available": False, "message": exc.message, "apiUrl": settings["apiUrl"]}

    def synthesize(self, params: dict[str, Any]) -> dict[str, Any]:
        settings = self.get_settings()
        text = str(params.get("text") or "").strip()
        if not text or len(text) > MAX_TEXT_CHARS:
            raise RpcFailure(-32602, f"text must contain 1-{MAX_TEXT_CHARS} characters")
        voice_id = str(params.get("voiceId") or settings["activeVoiceId"])
        voice = next((item for item in settings["voices"] if item["id"] == voice_id), None)
        if voice is None:
            raise RpcFailure(-32042, "voice not found")
        request_id = safe_id(params.get("requestId"), uuid4().hex)
        cancel_event = threading.Event()
        with self._cancel_lock:
            self._cancel_events[request_id] = cancel_event
        try:
            audio_bytes = self._synthesize_audio(text, settings, voice, cancel_event)
            if cancel_event.is_set():
                raise RpcFailure(-32800, "cancelled")
            audio_format = settings["audioFormat"]
            digest = hashlib.sha256(audio_bytes).hexdigest()[:12]
            audio_id = f"{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{digest}-{uuid4().hex[:6]}"
            output = self._require_layout().cache / "audio" / f"{audio_id}.{audio_format}"
            output.write_bytes(audio_bytes)
            item = {
                "id": audio_id,
                "createdAt": utc_now(),
                "voiceId": voice["id"],
                "voiceName": voice["name"],
                "textPreview": re.sub(r"\s+", " ", text)[:180],
                "size": len(audio_bytes),
                "mediaType": FORMAT_MEDIA_TYPES[audio_format],
                "path": str(output),
            }
            self.write_history([item, *self.read_history()])
            return {
                "requestId": request_id,
                "audio": {
                    "id": audio_id,
                    "path": str(output),
                    "mediaType": FORMAT_MEDIA_TYPES[audio_format],
                },
            }
        finally:
            with self._cancel_lock:
                self._cancel_events.pop(request_id, None)

    def cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        request_id = safe_id(params.get("requestId"), "")
        with self._cancel_lock:
            event = self._cancel_events.get(request_id)
            if event is not None:
                event.set()
        return {"cancelled": event is not None, "requestId": request_id}

    def preview(self, params: dict[str, Any]) -> dict[str, Any]:
        text = str(params.get("text") or "").strip()
        if not text or len(text) > MAX_PREVIEW_TEXT_CHARS:
            raise RpcFailure(
                -32602, f"preview text must contain 1-{MAX_PREVIEW_TEXT_CHARS} characters"
            )
        result = self.synthesize({**params, "text": text})
        descriptor = result["audio"]
        audio_root = self._require_layout().cache / "audio"
        try:
            audio_path = Path(descriptor["path"]).resolve(strict=True)
            audio_path.relative_to(audio_root.resolve(strict=True))
        except (KeyError, OSError, TypeError, ValueError):
            raise RpcFailure(-32603, "preview audio escaped the managed cache") from None
        size = audio_path.stat().st_size
        if size > MAX_PREVIEW_AUDIO_BYTES:
            audio_id = str(descriptor.get("id") or "")
            audio_path.unlink(missing_ok=True)
            self.write_history(
                [item for item in self.read_history() if str(item.get("id")) != audio_id]
            )
            raise RpcFailure(
                -32044,
                "preview audio exceeds the 6 MiB transfer limit",
                {"size": size, "limit": MAX_PREVIEW_AUDIO_BYTES},
            )
        return {
            "requestId": result["requestId"],
            "audio": {
                "id": descriptor["id"],
                "mediaType": descriptor["mediaType"],
                "size": size,
                "base64": base64.b64encode(audio_path.read_bytes()).decode("ascii"),
            },
        }

    def runtime_install(self, params: dict[str, Any]) -> dict[str, Any]:
        process = self.installer_process
        if process is not None and process.poll() is None:
            return self.runtime_install_status()
        self.installer_process = None
        device = str(params.get("device") or self.get_settings()["runtimeDevice"]).lower()
        if device not in RUNTIME_DEVICES:
            raise RpcFailure(-32602, "runtime device is invalid")
        settings = self.get_settings()
        settings["runtimeDevice"] = device
        self.save_settings(settings)
        self.stop_runtime()
        self._cleanup_runtime_staging()
        layout = self._require_layout()
        command = [
            sys.executable,
            "-I",
            "-X",
            "utf8",
            "-m",
            "fantareal_tts_studio.runtime_installer",
            "--assets-root",
            str(layout.assets),
            "--data-root",
            str(layout.data),
            "--cache-root",
            str(layout.cache),
            "--device",
            device,
        ]
        atomic_write_json(
            self.runtime_install_state_path,
            {
                "status": "starting",
                "step": "starting",
                "progress": 0.0,
                "device": device,
                "updatedAt": utc_now(),
                "error": "",
            },
        )
        self.runtime_install_log_path.write_bytes(b"")
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        try:
            with self.runtime_install_log_path.open("ab") as log_handle:
                self.installer_process = subprocess.Popen(
                    command,
                    cwd=str(layout.assets),
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    env=environment,
                    creationflags=self._runtime_creation_flags(),
                )
        except OSError as exc:
            atomic_write_json(
                self.runtime_install_state_path,
                {
                    "status": "failed",
                    "step": "starting",
                    "progress": 0.0,
                    "device": device,
                    "updatedAt": utc_now(),
                    "error": f"failed to start runtime installer: {exc}",
                },
            )
            raise RpcFailure(-32045, f"failed to start runtime installer: {exc}") from exc
        return self.runtime_install_status()

    def runtime_install_status(self) -> dict[str, Any]:
        process = self.installer_process
        running = process is not None and process.poll() is None
        return_code = None if process is None else process.poll()
        state = read_json(
            self.runtime_install_state_path,
            {
                "status": "idle",
                "step": "idle",
                "progress": 0.0,
                "error": "",
            },
        )
        if not isinstance(state, dict):
            state = {"status": "invalid", "step": "invalid", "progress": 0.0, "error": ""}
        if process is not None and not running and state.get("status") in {"running", "starting"}:
            state = {
                **state,
                "status": "failed",
                "step": "failed",
                "error": f"runtime installer exited with code {return_code}",
                "updatedAt": utc_now(),
            }
            atomic_write_json(self.runtime_install_state_path, state)
        if process is not None and not running:
            self.installer_process = None
        return {
            **state,
            "running": running,
            "pid": process.pid if running else state.get("pid"),
            "returnCode": return_code,
            "installed": self._read_runtime_pointer(),
            "logTail": self._read_log_tail(self.runtime_install_log_path),
            "supportedDevices": sorted(RUNTIME_DEVICES),
        }

    def cancel_runtime_install(self) -> dict[str, Any]:
        process = self.installer_process
        if process is None or process.poll() is not None:
            self.installer_process = None
            return self.runtime_install_status()
        self._terminate_process(process)
        self.installer_process = None
        self._cleanup_runtime_staging()
        previous = read_json(self.runtime_install_state_path, {})
        state = {
            **(previous if isinstance(previous, dict) else {}),
            "status": "cancelled",
            "step": "cancelled",
            "progress": 0.0,
            "updatedAt": utc_now(),
            "error": "runtime installation cancelled",
        }
        atomic_write_json(self.runtime_install_state_path, state)
        return self.runtime_install_status()

    def launch_runtime(self) -> dict[str, Any]:
        process = self.runtime_process
        if process is not None and process.poll() is None:
            return self.runtime_status()
        self.runtime_process = None
        pointer = self._read_runtime_pointer(required=True)
        if self.probe().get("available"):
            return self.runtime_status()
        api_path = Path(pointer["runtimeRoot"]) / "api_v2.py"
        python_path = Path(pointer["python"])
        parsed = urllib.parse.urlparse(self.get_settings()["apiUrl"])
        port = parsed.port or 9880
        command = [
            str(python_path),
            str(api_path),
            "-a",
            "127.0.0.1",
            "-p",
            str(port),
        ]
        self.runtime_log_path.write_bytes(b"")
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        try:
            with self.runtime_log_path.open("ab") as log_handle:
                self.runtime_process = subprocess.Popen(
                    command,
                    cwd=str(api_path.parent),
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    env=environment,
                    creationflags=self._runtime_creation_flags(),
                )
        except OSError as exc:
            raise RpcFailure(-32046, f"failed to launch GPT-SoVITS: {exc}") from exc
        return self.runtime_status()

    def runtime_status(self) -> dict[str, Any]:
        process = self.runtime_process
        running = process is not None and process.poll() is None
        pointer = self._read_runtime_pointer()
        return {
            "installed": pointer,
            "runtimeRoot": pointer.get("runtimeRoot", "") if pointer else "",
            "apiExists": bool(pointer and pointer.get("apiExists")),
            "python": pointer.get("python", "") if pointer else "",
            "running": running,
            "pid": process.pid if running else None,
            "returnCode": None if process is None else process.poll(),
            "probe": self.probe(),
            "installSupported": True,
            "logTail": self._read_log_tail(self.runtime_log_path),
        }

    def stop_runtime(self) -> dict[str, Any]:
        if self.runtime_process is not None and self.runtime_process.poll() is None:
            self._terminate_process(self.runtime_process)
        self.runtime_process = None
        return self.runtime_status()

    def _read_runtime_pointer(self, *, required: bool = False) -> dict[str, Any] | None:
        pointer = read_json(self.runtime_current_path, {})
        try:
            if not isinstance(pointer, dict):
                raise ValueError
            commit = str(pointer["commit"])
            runtime_root = Path(str(pointer["runtimeRoot"])).resolve(strict=True)
            python_path = Path(str(pointer["python"])).resolve(strict=True)
            versions_root = (self._require_layout().assets / "runtime" / "versions").resolve(
                strict=True
            )
            runtime_root.relative_to(versions_root)
            python_path.relative_to(runtime_root.parent)
            if runtime_root.parent.name != commit or not (runtime_root / "api_v2.py").is_file():
                raise ValueError
            if not python_path.is_file():
                raise ValueError
        except (KeyError, OSError, TypeError, ValueError):
            if required:
                raise RpcFailure(-32047, "installed GPT-SoVITS runtime is unavailable") from None
            return None
        return {
            **pointer,
            "runtimeRoot": str(runtime_root),
            "python": str(python_path),
            "apiExists": True,
        }

    @staticmethod
    def _read_log_tail(path: Path, limit: int = 64 * 1024) -> str:
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - limit))
                payload = handle.read(limit)
        except OSError:
            return ""
        return payload.decode("utf-8", errors="replace")[-limit:]

    @staticmethod
    def _runtime_creation_flags() -> int:
        if os.name != "nt":
            return 0
        return getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )

    @staticmethod
    def _terminate_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=8)

    def _cleanup_runtime_staging(self) -> None:
        runtime_root = self._require_layout().assets / "runtime"
        runtime_root.mkdir(parents=True, exist_ok=True)
        canonical_root = runtime_root.resolve(strict=True)
        for path in runtime_root.glob(".staging-*"):
            try:
                if path.is_symlink() or path.resolve(strict=True).parent != canonical_root:
                    continue
            except OSError:
                continue
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)

    def dispatch(self, method: str, params: Any) -> Any:
        values = params if isinstance(params, dict) else {}
        if method == "extension.initialize":
            return self.initialize(values)
        if method == "extension.health":
            return {
                "initialized": self.layout is not None,
                "provider": self.probe() if self.layout else {},
            }
        if method == "extension.shutdown":
            self.cancel_runtime_install() if self.layout else None
            self.stop_runtime() if self.layout else None
            self.should_stop = True
            return {"stopping": True}
        self._require_layout()
        if method == "tts.health":
            self._require_provider(values)
            return self.probe()
        if method == "tts.listVoices":
            self._require_provider(values)
            return self.list_voices()
        if method == "tts.synthesize":
            self._require_provider(values)
            return self.synthesize(values)
        if method == "tts.cancel":
            self._require_provider(values)
            return self.cancel(values)
        if method == "ttsStudio.getState":
            return {
                "settings": self.get_settings(),
                "assets": self.discover_assets(),
                "history": self.read_history(),
                "runtime": self.runtime_status(),
            }
        if method == "ttsStudio.saveSettings":
            return {"settings": self.save_settings(values.get("settings"))}
        if method == "ttsStudio.discover":
            return {"assets": self.discover_assets()}
        if method == "ttsStudio.importAsset":
            return {"item": self.import_asset(values), "assets": self.discover_assets()}
        if method == "ttsStudio.history":
            return {"items": self.read_history()}
        if method == "ttsStudio.deleteHistory":
            audio_id = safe_id(values.get("audioId"), "")
            items = [item for item in self.read_history() if item.get("id") != audio_id]
            for path in (self._require_layout().cache / "audio").glob(f"{audio_id}.*"):
                if path.is_file() and not path.is_symlink():
                    path.unlink()
            self.write_history(items)
            return {"items": items}
        if method == "ttsStudio.preview":
            return self.preview(values)
        if method == "ttsStudio.runtimeInstall":
            return self.runtime_install(values)
        if method == "ttsStudio.runtimeInstallStatus":
            return self.runtime_install_status()
        if method == "ttsStudio.runtimeCancel":
            return self.cancel_runtime_install()
        if method == "ttsStudio.runtimeLaunch":
            return self.launch_runtime()
        if method == "ttsStudio.runtimeStatus":
            return self.runtime_status()
        if method == "ttsStudio.runtimeStop":
            return self.stop_runtime()
        raise RpcFailure(-32601, "Method not found")

    def _require_layout(self) -> StorageLayout:
        if self.layout is None:
            raise RpcFailure(-32001, "extension.initialize must be called first")
        return self.layout

    @staticmethod
    def _require_provider(params: dict[str, Any]) -> None:
        if params.get("providerId") != PROVIDER_ID:
            raise RpcFailure(-32602, "providerId is invalid")

    def _synthesize_audio(
        self,
        text: str,
        settings: dict[str, Any],
        voice: dict[str, Any],
        cancel_event: threading.Event,
    ) -> bytes:
        layout = self._require_layout()
        reference_audio = voice["referenceAudio"]
        if not reference_audio:
            raise RpcFailure(-32043, "voice reference audio is not configured")
        reference = asset_path(layout, reference_audio, AUDIO_SUFFIXES)
        prompt_text = voice["promptText"].strip() or reference.stem
        if not prompt_text:
            raise RpcFailure(-32602, "voice reference text is required")
        if cancel_event.is_set():
            raise RpcFailure(-32800, "cancelled")
        for key, endpoint, suffixes in (
            ("gptWeights", "/set_gpt_weights", {".ckpt"}),
            ("sovitsWeights", "/set_sovits_weights", {".pth", ".pt"}),
        ):
            relative = voice[key]
            if relative:
                weight = asset_path(layout, relative, suffixes)
                query = urllib.parse.urlencode({"weights_path": str(weight)})
                self._request_bytes(
                    f"{settings['apiUrl']}{endpoint}?{query}",
                    method="GET",
                    timeout=float(settings["requestTimeoutSeconds"]),
                )
        payload = {
            "text": text,
            "text_lang": voice["textLanguage"],
            "ref_audio_path": str(reference),
            "prompt_text": prompt_text,
            "prompt_lang": voice["promptLanguage"],
            "top_k": settings["topK"],
            "top_p": settings["topP"],
            "temperature": settings["temperature"],
            "text_split_method": settings["textSplitMethod"],
            "batch_size": settings["batchSize"],
            "split_bucket": True,
            "speed_factor": settings["speedFactor"],
            "streaming_mode": False,
            "seed": -1,
            "parallel_infer": settings["parallelInfer"],
            "repetition_penalty": settings["repetitionPenalty"],
            "sample_steps": settings["sampleSteps"],
            "media_type": settings["audioFormat"],
        }
        if cancel_event.is_set():
            raise RpcFailure(-32800, "cancelled")
        return self._request_bytes(
            f"{settings['apiUrl']}/tts",
            method="POST",
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=float(settings["requestTimeoutSeconds"]),
        )

    def _request_json(self, url: str, *, timeout: float) -> Any:
        payload = self._request_bytes(url, method="GET", timeout=timeout)
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise RpcFailure(-32043, "GPT-SoVITS returned invalid JSON") from None

    @staticmethod
    def _request_bytes(
        url: str,
        *,
        method: str,
        timeout: float,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "http" or (parsed.hostname or "").lower() not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise RpcFailure(-32602, "only loopback GPT-SoVITS endpoints are allowed")
        request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read(64 * 1024 * 1024 + 1)
        except (OSError, urllib.error.HTTPError, urllib.error.URLError) as exc:
            raise RpcFailure(-32043, f"GPT-SoVITS request failed: {exc}") from exc
        if len(payload) > 64 * 1024 * 1024:
            raise RpcFailure(-32043, "GPT-SoVITS response is too large")
        return payload


def error_response(request_id: Any, failure: RpcFailure) -> dict[str, Any]:
    error: dict[str, Any] = {"code": failure.code, "message": failure.message}
    if failure.data:
        error["data"] = failure.data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def handle_request(service: TtsStudioService, payload: Any) -> dict[str, Any] | None:
    request_id = payload.get("id") if isinstance(payload, dict) else None
    try:
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            raise RpcFailure(-32600, "Invalid Request")
        method = payload.get("method")
        if not isinstance(method, str) or not method:
            raise RpcFailure(-32600, "Invalid Request")
        result = service.dispatch(method, payload.get("params", {}))
        if "id" not in payload:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except RpcFailure as exc:
        return error_response(request_id, exc)
    except Exception:
        return error_response(request_id, RpcFailure(-32603, "Internal error"))


def run(input_stream: TextIO, output_stream: TextIO) -> int:
    service = TtsStudioService()
    output_lock = threading.Lock()
    workers: list[threading.Thread] = []

    def process(payload: Any) -> None:
        response = handle_request(service, payload)
        if response is None:
            return
        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n"
        with output_lock:
            output_stream.write(encoded)
            output_stream.flush()

    for line in input_stream:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            process({"jsonrpc": "invalid", "id": None})
            continue
        if isinstance(payload, dict) and payload.get("method") in {
            "tts.synthesize",
            "ttsStudio.preview",
        }:
            worker = threading.Thread(target=process, args=(payload,), daemon=True)
            workers.append(worker)
            worker.start()
        else:
            process(payload)
        if service.should_stop:
            break
    for worker in workers:
        worker.join(timeout=2)
    return 0


def main() -> None:
    raise SystemExit(run(sys.stdin, sys.stdout))


if __name__ == "__main__":
    main()
