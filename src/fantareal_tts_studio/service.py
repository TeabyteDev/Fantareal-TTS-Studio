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
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO
from uuid import uuid4

from .model_pack import ModelPackError, scan_model_pack, validate_model_pack_manifest

EXTENSION_ID = "com.fantareal.tts-studio"
PROVIDER_ID = "gpt-sovits"
DEFAULT_API_URL = "http://127.0.0.1:9880"
MAX_TEXT_CHARS = 6000
MAX_MODEL_BYTES = 3 * 1024 * 1024 * 1024
MAX_AUDIO_BYTES = 200 * 1024 * 1024
MAX_HISTORY_ITEMS = 200
MAX_PREVIEW_TEXT_CHARS = 500
MAX_PREVIEW_AUDIO_BYTES = 6 * 1024 * 1024
MODEL_PACK_REFERENCE_PREFIX = "model-pack:"
ACTIVE_MODEL_PACK_KIND = "fantareal.active-model-pack"
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
    "modelVersion": "v4",
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


def contained_existing_directory(root: Path, relative_path: Any) -> Path:
    text = str(relative_path or "").strip().replace("\\", "/")
    if not text or Path(text).is_absolute() or ".." in Path(text).parts:
        raise RpcFailure(-32602, "directory path must be workspace-relative")
    candidate = root.joinpath(*Path(text).parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        raise RpcFailure(-32602, "directory path escapes the allowed root") from None
    if candidate.is_symlink() or os.path.islink(candidate) or not resolved.is_dir():
        raise RpcFailure(-32602, "path is not a regular directory")
    return resolved


def contained_directory_grant(layout: StorageLayout, token: Any) -> Path:
    token_text = str(token or "").strip().lower()
    token_pattern = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    if not re.fullmatch(token_pattern, token_text):
        raise RpcFailure(-32602, "directory token is invalid")
    grant_path = layout.workspace / "input-directory-grants" / f"{token_text}.json"
    if grant_path.is_symlink() or os.path.islink(grant_path) or not grant_path.is_file():
        raise RpcFailure(-32602, "directory token is unavailable")
    grant = read_json(grant_path, {})
    if not isinstance(grant, dict) or grant.get("kind") != "fantareal.directory-grant":
        raise RpcFailure(-32602, "directory grant is invalid")
    if str(grant.get("token") or "").strip().lower() != token_text:
        raise RpcFailure(-32602, "directory grant token does not match")
    source_raw = grant.get("path")
    if not isinstance(source_raw, str) or not Path(source_raw).is_absolute():
        raise RpcFailure(-32602, "directory grant path is invalid")
    source = Path(source_raw)
    try:
        resolved = source.resolve(strict=True)
    except OSError:
        raise RpcFailure(-32602, "directory grant path is unavailable") from None
    if source.is_symlink() or os.path.islink(source) or not resolved.is_dir():
        raise RpcFailure(-32602, "directory grant path is not a regular directory")
    return resolved


def asset_path(layout: StorageLayout, relative_path: Any, suffixes: set[str]) -> Path:
    path = contained_existing_file(layout.assets, relative_path)
    if path.suffix.lower() not in suffixes:
        raise RpcFailure(-32602, "asset type is not supported")
    return path


def model_pack_file(
    service: TtsStudioService,
    relative_path: Any,
    suffixes: set[str],
) -> Path:
    text = str(relative_path or "").strip().replace("\\", "/")
    if not text.startswith(MODEL_PACK_REFERENCE_PREFIX):
        return asset_path(service._require_layout(), text, suffixes)
    relative = text[len(MODEL_PACK_REFERENCE_PREFIX) :]
    if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise RpcFailure(-32602, "model pack path is invalid")
    active = service.active_model_pack(required=True)
    root = Path(active["root"])
    candidate = root.joinpath(*Path(relative).parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        raise RpcFailure(-32602, "model pack path is unavailable") from None
    if candidate.is_symlink() or os.path.islink(candidate) or not resolved.is_file():
        raise RpcFailure(-32602, "model pack path is not a regular file")
    if resolved.suffix.lower() not in suffixes:
        raise RpcFailure(-32602, "model pack asset type is not supported")
    allowed = {
        str(item.get("path"))
        for item in active["manifest"].get("files", [])
        if str(item.get("role")) in {"gpt", "sovits", "audio", "pretrained"}
    }
    if relative not in allowed:
        raise RpcFailure(-32602, "model pack path is not declared by the manifest")
    return resolved


def sanitize_voice(raw: Any, index: int) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    voice_id = safe_id(source.get("id"), f"voice-{index + 1}")
    def sanitize_reference(value: Any) -> str:
        text = str(value or "").strip().replace("\\", "/")
        if text.startswith(MODEL_PACK_REFERENCE_PREFIX):
            relative = text[len(MODEL_PACK_REFERENCE_PREFIX) :]
            if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
                return ""
            return MODEL_PACK_REFERENCE_PREFIX + relative.lstrip("./")
        if text and (Path(text).is_absolute() or ".." in Path(text).parts):
            return ""
        return text

    result = {
        "id": voice_id,
        "name": str(source.get("name") or voice_id).strip()[:120],
        "locale": str(source.get("locale") or "zh-CN").strip()[:32],
        "gptWeights": sanitize_reference(source.get("gptWeights")),
        "sovitsWeights": sanitize_reference(source.get("sovitsWeights")),
        "referenceAudio": sanitize_reference(source.get("referenceAudio")),
        "promptText": str(source.get("promptText") or "").strip()[:4000],
        "promptLanguage": str(source.get("promptLanguage") or "zh").strip()[:16],
        "textLanguage": str(source.get("textLanguage") or "zh").strip()[:16],
        "modelVersion": str(source.get("modelVersion") or "v4").strip()[:16],
    }
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

    @property
    def active_model_pack_path(self) -> Path:
        return self._require_layout().data / "active-model-pack.json"

    @property
    def runtime_model_pack_config_path(self) -> Path:
        return self._require_layout().data / "runtime-model-pack-config.json"

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

    def active_model_pack(self, *, required: bool = False) -> dict[str, Any] | None:
        state = read_json(self.active_model_pack_path, {})
        try:
            if not isinstance(state, dict) or state.get("kind") != ACTIVE_MODEL_PACK_KIND:
                raise ValueError
            root = Path(str(state["root"])).resolve(strict=True)
            if root.is_symlink() or os.path.islink(root) or not root.is_dir():
                raise ValueError
            manifest = validate_model_pack_manifest(state["manifest"], root)
        except (KeyError, OSError, TypeError, ValueError, ModelPackError):
            if required:
                raise RpcFailure(-32056, "active model pack is unavailable") from None
            return None
        return {
            "kind": ACTIVE_MODEL_PACK_KIND,
            "root": str(root),
            "packId": manifest["packId"],
            "version": manifest["version"],
            "manifest": manifest,
            "activatedAt": str(state.get("activatedAt") or ""),
        }

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
        active = self.active_model_pack()
        if active:
            for item in active["manifest"]["files"]:
                role = str(item.get("role") or "")
                if role in {"gpt", "sovits", "audio"}:
                    result.setdefault(role, []).append(
                        MODEL_PACK_REFERENCE_PREFIX + str(item["path"])
                    )
            for kind in ("gpt", "sovits", "audio"):
                result[kind] = result.get(kind, [])[:300]
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

    def inspect_model_pack(self, params: dict[str, Any]) -> dict[str, Any]:
        layout = self._require_layout()
        if params.get("directoryToken"):
            source = contained_directory_grant(layout, params.get("directoryToken"))
        else:
            source = contained_existing_directory(layout.workspace, params.get("path"))
        try:
            return scan_model_pack(
                source,
                pack_id=str(params.get("packId") or "").strip() or None,
                version=str(params.get("version") or "local").strip(),
                compute_sha256=params.get("computeSha256") is True,
            )
        except ModelPackError as exc:
            raise RpcFailure(-32054, str(exc)) from exc

    def activate_model_pack(self, params: dict[str, Any]) -> dict[str, Any]:
        layout = self._require_layout()
        if not params.get("directoryToken"):
            raise RpcFailure(-32602, "directoryToken is required")
        source = contained_directory_grant(layout, params.get("directoryToken"))
        raw_manifest = params.get("manifest")
        try:
            manifest = (
                validate_model_pack_manifest(raw_manifest, source)
                if isinstance(raw_manifest, dict)
                else scan_model_pack(
                    source,
                    pack_id=str(params.get("packId") or source.name),
                    version=str(params.get("version") or "local"),
                )
            )
        except ModelPackError as exc:
            raise RpcFailure(-32054, str(exc)) from exc
        previous_running = self.runtime_process is not None and self.runtime_process.poll() is None
        if previous_running:
            self.stop_runtime()
        state = {
            "kind": ACTIVE_MODEL_PACK_KIND,
            "root": str(source),
            "packId": manifest["packId"],
            "version": manifest["version"],
            "manifest": manifest,
            "activatedAt": utc_now(),
        }
        atomic_write_json(self.active_model_pack_path, state)
        self.runtime_model_pack_config_path.unlink(missing_ok=True)
        return {
            "active": self.active_model_pack(required=True),
            "assets": self.discover_assets(),
            "runtimeRestarted": previous_running,
        }

    def deactivate_model_pack(self) -> dict[str, Any]:
        self.stop_runtime()
        self.active_model_pack_path.unlink(missing_ok=True)
        self.runtime_model_pack_config_path.unlink(missing_ok=True)
        return {"active": None, "assets": self.discover_assets()}

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

    def readiness(self) -> dict[str, Any]:
        """Return actionable diagnostics without starting processes."""
        settings = self.get_settings()
        checks: list[dict[str, Any]] = []

        def check(check_id: str, ok: bool, code: str, message: str) -> None:
            checks.append(
                {
                    "id": check_id,
                    "ok": ok,
                    "code": "ok" if ok else code,
                    "message": message,
                }
            )

        active_state_exists = self.active_model_pack_path.is_file()
        active = self.active_model_pack()
        if active_state_exists and active is None:
            check(
                "modelPack",
                False,
                "active_model_pack_unavailable",
                "active model pack is unavailable; rescan and activate it again",
            )
        else:
            check(
                "modelPack",
                True,
                "ok",
                "active model pack is valid" if active else "no external model pack is active",
            )

        voice = next(
            (item for item in settings["voices"] if item["id"] == settings["activeVoiceId"]),
            None,
        )
        if voice is None:
            check("voice", False, "voice_not_configured", "active voice is not configured")
        else:
            check("voice", True, "ok", f"active voice: {voice['name']}")
            reference_audio = str(voice.get("referenceAudio") or "").strip()
            if not reference_audio:
                check(
                    "referenceAudio",
                    False,
                    "reference_audio_missing",
                    "active voice reference audio is not configured",
                )
            else:
                try:
                    model_pack_file(self, reference_audio, AUDIO_SUFFIXES)
                except RpcFailure as exc:
                    check("referenceAudio", False, "reference_audio_unavailable", exc.message)
                else:
                    check("referenceAudio", True, "ok", "reference audio is available")

            for key, suffixes, label, code in (
                ("gptWeights", {".ckpt"}, "GPT weights", "gpt_weights_unavailable"),
                ("sovitsWeights", {".pth", ".pt"}, "SoVITS weights", "sovits_weights_unavailable"),
            ):
                value = str(voice.get(key) or "").strip()
                if not value:
                    check(key, True, "ok", f"{label} will use runtime defaults")
                    continue
                try:
                    model_pack_file(self, value, suffixes)
                except RpcFailure as exc:
                    check(key, False, code, exc.message)
                else:
                    check(key, True, "ok", f"{label} is available")

        pointer = self._read_runtime_pointer()
        check(
            "runtimeInstalled",
            pointer is not None,
            "runtime_not_installed",
            "GPT-SoVITS runtime is installed" if pointer else "GPT-SoVITS runtime is not installed",
        )
        process = self.runtime_process
        running = process is not None and process.poll() is None
        check(
            "runtimeProcess",
            running,
            "runtime_not_running",
            "runtime process is running" if running else "runtime process is not running",
        )
        probe = self.probe()
        check(
            "api",
            bool(probe.get("available")),
            "api_not_ready",
            str(probe.get("message") or "GPT-SoVITS API is not ready"),
        )

        if active is not None and voice is not None:
            try:
                self._prepare_runtime_config(settings, voice, persist=False)
            except RpcFailure as exc:
                message = exc.message
                code = (
                    "pretrained_missing"
                    if "pretrained directory" in message
                    else "runtime_config_invalid"
                )
                check("runtimeConfig", False, code, message)
            else:
                check("runtimeConfig", True, "ok", "runtime model-pack config is valid")
        else:
            check("runtimeConfig", True, "ok", "runtime model-pack config is not required")

        model_failures = [
            item
            for item in checks
            if not item["ok"]
            and item["id"]
            in {
                "modelPack",
                "voice",
                "referenceAudio",
                "gptWeights",
                "sovitsWeights",
                "runtimeConfig",
            }
        ]
        api_available = bool(probe.get("available"))
        if model_failures:
            first_failure = model_failures[0]
            status = first_failure["code"]
            ready = False
            message = first_failure["message"]
        elif api_available:
            first_failure = None
            status = "ready"
            ready = True
            message = (
                "TTS runtime is ready"
                if pointer or running
                else "external GPT-SoVITS API is ready"
            )
        elif pointer is None:
            first_failure = next(item for item in checks if item["id"] == "runtimeInstalled")
            status = "runtime_not_installed"
            ready = False
            message = first_failure["message"]
        elif not running:
            first_failure = next(item for item in checks if item["id"] == "runtimeProcess")
            status = "runtime_not_running"
            ready = False
            message = first_failure["message"]
        else:
            first_failure = next(item for item in checks if item["id"] == "api")
            runtime_log = self._read_log_tail(self.runtime_log_path).lower()
            port_conflict = any(
                marker in runtime_log
                for marker in ("address already in use", "winerror 10048", "10048")
            )
            status = "api_port_conflict" if port_conflict else "api_not_ready"
            ready = False
            message = (
                "GPT-SoVITS API port is already in use"
                if port_conflict
                else first_failure["message"]
            )
        return {
            "ready": ready,
            "status": status,
            "message": message,
            "checks": checks,
            "apiUrl": settings["apiUrl"],
            "activeVoiceId": settings["activeVoiceId"],
            "runtime": {
                "installed": pointer,
                "running": running,
                "managed": bool(pointer or running),
                "pid": process.pid if running else None,
                "probe": probe,
                "logTail": self._read_log_tail(self.runtime_log_path),
            },
        }

    def runtime_smoke(self, params: dict[str, Any]) -> dict[str, Any]:
        text = str(params.get("text") or "你好，这是 Fantareal TTS Studio 的测试声音。").strip()
        if not text or len(text) > MAX_PREVIEW_TEXT_CHARS:
            raise RpcFailure(
                -32602, f"smoke text must contain 1-{MAX_PREVIEW_TEXT_CHARS} characters"
            )
        timeout_seconds = clamp_float(params.get("timeoutSeconds"), 1.0, 120.0, 30.0)
        auto_launch = params.get("autoLaunch", True) is not False
        started = False
        readiness = self.readiness()
        deadline = time.monotonic() + timeout_seconds

        if not readiness["ready"] and auto_launch and readiness["status"] in {
            "runtime_not_running",
            "api_not_ready",
        }:
            if not readiness["runtime"]["installed"]:
                return {
                    "ok": False,
                    "status": "runtime_not_installed",
                    "message": "GPT-SoVITS runtime is not installed",
                    "started": False,
                    "readiness": readiness,
                }
            self.launch_runtime()
            started = True

        while not readiness["ready"] and time.monotonic() < deadline:
            time.sleep(0.25)
            readiness = self.readiness()

        if not readiness["ready"]:
            return {
                "ok": False,
                "status": readiness["status"],
                "message": readiness["message"],
                "started": started,
                "readiness": readiness,
                "runtimeLog": self._read_log_tail(self.runtime_log_path),
            }

        try:
            result = self.preview(
                {
                    "text": text,
                    "voiceId": readiness["activeVoiceId"],
                    "requestId": safe_id(params.get("requestId"), f"smoke-{uuid4().hex}"),
                }
            )
        except RpcFailure as exc:
            return {
                "ok": False,
                "status": "synthesis_failed",
                "message": exc.message,
                "errorCode": exc.code,
                "started": started,
                "readiness": self.readiness(),
                "runtimeLog": self._read_log_tail(self.runtime_log_path),
            }
        return {
            "ok": True,
            "status": "ready",
            "message": "TTS smoke synthesis succeeded",
            "started": started,
            "readiness": readiness,
            "audio": result["audio"],
        }

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
        settings = self.get_settings()
        active_voice = next(
            (voice for voice in settings["voices"] if voice["id"] == settings["activeVoiceId"]),
            settings["voices"][0],
        )
        custom_config = self._prepare_runtime_config(settings, active_voice)
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
        if custom_config is not None:
            command.extend(["-c", str(custom_config)])
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

    def _prepare_runtime_config(
        self, settings: dict[str, Any], voice: dict[str, Any], *, persist: bool = True
    ) -> Path | None:
        active = self.active_model_pack()
        if active is None:
            self.runtime_model_pack_config_path.unlink(missing_ok=True)
            return None
        gpt = model_pack_file(self, voice.get("gptWeights"), {".ckpt"})
        sovits = model_pack_file(self, voice.get("sovitsWeights"), {".pth", ".pt"})
        root = Path(active["root"])

        def find_directory(name: str) -> Path:
            candidates = [
                root / "runtime" / "GPT-SoVITS" / "GPT_SoVITS" / "pretrained_models" / name,
                root / "GPT-SoVITS" / "GPT_SoVITS" / "pretrained_models" / name,
                root / "GPT_SoVITS" / "pretrained_models" / name,
                root / "pretrained_models" / name,
            ]
            for candidate in candidates:
                if (
                    candidate.is_dir()
                    and not candidate.is_symlink()
                    and not os.path.islink(candidate)
                ):
                    return candidate.resolve(strict=True)
            raise RpcFailure(-32057, f"active model pack is missing pretrained directory: {name}")

        version = str(voice.get("modelVersion") or "v4")
        device = "cuda" if settings["runtimeDevice"] in {"cu126", "cu128"} else "cpu"
        config = {
            "custom": {
                "bert_base_path": str(find_directory("chinese-roberta-wwm-ext-large")),
                "cnhuhbert_base_path": str(find_directory("chinese-hubert-base")),
                "device": device,
                "is_half": device == "cuda",
                "t2s_weights_path": str(gpt),
                "version": version,
                "vits_weights_path": str(sovits),
            }
        }
        if persist:
            atomic_write_json(self.runtime_model_pack_config_path, config)
        return self.runtime_model_pack_config_path

    def runtime_status(self) -> dict[str, Any]:
        process = self.runtime_process
        running = process is not None and process.poll() is None
        pointer = self._read_runtime_pointer()
        status = {
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
        status["readiness"] = self.readiness()
        return status

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
                "modelPack": self.active_model_pack(),
                "history": self.read_history(),
                "runtime": self.runtime_status(),
            }
        if method == "ttsStudio.saveSettings":
            return {"settings": self.save_settings(values.get("settings"))}
        if method == "ttsStudio.discover":
            return {"assets": self.discover_assets()}
        if method == "ttsStudio.importAsset":
            return {"item": self.import_asset(values), "assets": self.discover_assets()}
        if method == "ttsStudio.inspectModelPack":
            return {"manifest": self.inspect_model_pack(values)}
        if method == "ttsStudio.activateModelPack":
            return self.activate_model_pack(values)
        if method == "ttsStudio.deactivateModelPack":
            return self.deactivate_model_pack()
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
        if method == "ttsStudio.readiness":
            return self.readiness()
        if method == "ttsStudio.runtimeSmoke":
            return self.runtime_smoke(values)
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
        reference_audio = voice["referenceAudio"]
        if not reference_audio:
            raise RpcFailure(-32043, "voice reference audio is not configured")
        reference = model_pack_file(self, reference_audio, AUDIO_SUFFIXES)
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
                weight = model_pack_file(self, relative, suffixes)
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
