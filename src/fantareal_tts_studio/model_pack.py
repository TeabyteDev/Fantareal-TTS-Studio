from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MODEL_PACK_KIND = "fantareal.tts-model-pack"
DEFAULT_MAX_FILES = 4096
DEFAULT_MAX_BYTES = 8 * 1024 * 1024 * 1024
AUDIO_SUFFIXES = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac"}
ROLE_SUFFIXES = {
    "gpt": {".ckpt"},
    "sovits": {".pth", ".pt"},
    "audio": AUDIO_SUFFIXES,
    "pretrained": {".ckpt", ".pth", ".pt", ".bin", ".onnx"},
}
ROLES = frozenset(ROLE_SUFFIXES)
LOCAL_RUNTIME_KIND = "gpt-sovits-local-runtime"
LOCAL_RUNTIME_FILES = {
    "entrypoint": "api_v2.py",
    "requirements": "requirements.txt",
    "extraRequirements": "extra-req.txt",
}


class ModelPackError(ValueError):
    """Raised when a local TTS model pack is unsafe or incomplete."""


def _is_link(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or os.path.islink(path) or bool(is_junction and is_junction())


def _safe_id(value: Any, fallback: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]", "-", str(value or "").strip()).strip("-.")
    return (cleaned[:80] or fallback).lower()


def _safe_relative_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    path = Path(text)
    if not text or path.is_absolute() or ".." in path.parts:
        raise ModelPackError("model pack path must be relative")
    return "/".join(part for part in path.parts if part not in ("", "."))


def _safe_relative_directory(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    return "." if text == "." else _safe_relative_path(text)


def _resolve_directory(root: Path | str) -> Path:
    candidate = Path(root).expanduser()
    if _is_link(candidate):
        raise ModelPackError("model pack root must not be a symbolic link")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ModelPackError(f"model pack root is unavailable: {exc}") from exc
    if not resolved.is_dir():
        raise ModelPackError("model pack root must be a directory")
    return resolved


def _normalize_runtime(
    source_root: Path, raw: dict[str, Any] | None = None
) -> dict[str, str] | None:
    if raw is None:
        candidates = ("runtime/GPT-SoVITS", "GPT-SoVITS", ".")
        runtime_relative = next(
            (
                relative
                for relative in candidates
                if all(
                    (source_root / relative / filename).is_file()
                    for filename in LOCAL_RUNTIME_FILES.values()
                )
            ),
            None,
        )
        if runtime_relative is None:
            return None
    else:
        if raw.get("kind") != LOCAL_RUNTIME_KIND:
            raise ModelPackError("model pack runtime kind is invalid")
        runtime_relative = _safe_relative_directory(raw.get("root"))

    runtime_root = (source_root / runtime_relative).resolve(strict=True)
    try:
        runtime_root.relative_to(source_root)
    except ValueError:
        raise ModelPackError("model pack runtime is outside the source root") from None
    if _is_link(runtime_root) or not runtime_root.is_dir():
        raise ModelPackError("model pack runtime root is not a regular directory")

    descriptor = {"kind": LOCAL_RUNTIME_KIND, "root": runtime_relative}
    for key, filename in LOCAL_RUNTIME_FILES.items():
        path = runtime_root / filename
        if _is_link(path) or not path.is_file():
            raise ModelPackError(f"model pack runtime is missing {filename}")
        descriptor[key] = filename if runtime_relative == "." else f"{runtime_relative}/{filename}"
    return descriptor


def _role_for(path: Path) -> str | None:
    parts = [part.lower() for part in path.parts]
    if "pretrained_models" in parts:
        return "pretrained" if path.suffix.lower() in ROLE_SUFFIXES["pretrained"] else None
    for marker, role in (("gpt", "gpt"), ("sovits", "sovits"), ("audio", "audio")):
        if marker in parts and path.suffix.lower() in ROLE_SUFFIXES[role]:
            return role
    return None


def _iter_files(root: Path) -> list[tuple[Path, str]]:
    result: list[tuple[Path, str]] = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for directory in directories:
            directory_path = current_path / directory
            if _is_link(directory_path):
                raise ModelPackError("model pack contains a symbolic-link directory")
        for filename in filenames:
            path = current_path / filename
            if _is_link(path):
                raise ModelPackError("model pack contains a symbolic-link file")
            relative = path.relative_to(root)
            role = _role_for(relative)
            if role is not None:
                result.append((path, role))
    return sorted(result, key=lambda item: item[0].relative_to(root).as_posix().lower())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _voice_entries(files: list[dict[str, Any]]) -> list[dict[str, str]]:
    grouped: dict[str, dict[str, str]] = {}
    for item in files:
        role = str(item["role"])
        if role not in {"gpt", "sovits", "audio"}:
            continue
        stem = Path(str(item["path"])).stem
        group = grouped.setdefault(stem, {})
        group.setdefault(role, str(item["path"]))
    voices: list[dict[str, str]] = []
    for name in sorted(grouped, key=str.casefold):
        group = grouped[name]
        if "gpt" not in group and "sovits" not in group:
            continue
        fallback_id = f"voice-{len(voices) + 1}"
        voices.append(
            {
                "id": _safe_id(name, fallback_id),
                "name": name[:120],
                "gptWeights": group.get("gpt", ""),
                "sovitsWeights": group.get("sovits", ""),
                "referenceAudio": group.get("audio", ""),
            }
        )
    return voices[:128]


def validate_model_pack_manifest(
    manifest: dict[str, Any],
    root: Path | str,
    *,
    verify_hash: bool = False,
) -> dict[str, Any]:
    source_root = _resolve_directory(root)
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != SCHEMA_VERSION:
        raise ModelPackError("model pack schemaVersion must be 1")
    if manifest.get("kind") != MODEL_PACK_KIND:
        raise ModelPackError("model pack kind is invalid")
    pack_id = _safe_id(manifest.get("packId"), "")
    version = str(manifest.get("version") or "").strip()
    if not pack_id or not version:
        raise ModelPackError("model pack packId and version are required")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ModelPackError("model pack must contain files")

    normalized_files: list[dict[str, Any]] = []
    seen: set[str] = set()
    total_bytes = 0
    role_counts: dict[str, int] = {}
    for raw in raw_files:
        if not isinstance(raw, dict):
            raise ModelPackError("model pack file entry is invalid")
        relative = _safe_relative_path(raw.get("path"))
        if relative in seen:
            raise ModelPackError("model pack contains duplicate file paths")
        seen.add(relative)
        role = str(raw.get("role") or "")
        if role not in ROLES:
            raise ModelPackError("model pack file role is invalid")
        candidate = source_root.joinpath(*Path(relative).parts)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(source_root)
        except (OSError, ValueError):
            raise ModelPackError(
                f"model pack file is outside the source root: {relative}"
            ) from None
        if _is_link(candidate) or not resolved.is_file():
            raise ModelPackError(f"model pack file is not a regular file: {relative}")
        size = resolved.stat().st_size
        declared_size = raw.get("sizeBytes")
        if not isinstance(declared_size, int) or declared_size != size:
            raise ModelPackError(f"model pack file size mismatch: {relative}")
        declared_hash = raw.get("sha256")
        if declared_hash is not None:
            if not isinstance(declared_hash, str) or not re.fullmatch(
                r"[0-9a-f]{64}", declared_hash
            ):
                raise ModelPackError(f"model pack file sha256 is invalid: {relative}")
            if verify_hash and _sha256(resolved) != declared_hash:
                raise ModelPackError(f"model pack file sha256 mismatch: {relative}")
        entry = {"path": relative, "role": role, "sizeBytes": size}
        if declared_hash is not None:
            entry["sha256"] = declared_hash
        normalized_files.append(entry)
        total_bytes += size
        role_counts[role] = role_counts.get(role, 0) + 1

    summary = manifest.get("summary")
    if not isinstance(summary, dict):
        raise ModelPackError("model pack summary is required")
    if summary.get("fileCount") != len(normalized_files) or summary.get("bytes") != total_bytes:
        raise ModelPackError("model pack summary does not match files")
    if summary.get("roles") != role_counts:
        raise ModelPackError("model pack role summary does not match files")
    normalized = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": MODEL_PACK_KIND,
        "packId": pack_id,
        "version": version,
        "source": str(manifest.get("source") or "local"),
        "files": normalized_files,
        "summary": {"fileCount": len(normalized_files), "bytes": total_bytes, "roles": role_counts},
        "voices": manifest.get("voices") if isinstance(manifest.get("voices"), list) else [],
    }
    runtime = _normalize_runtime(
        source_root,
        manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else None,
    )
    if runtime is not None:
        normalized["runtime"] = runtime
    return normalized


def scan_model_pack(
    root: Path | str,
    *,
    pack_id: str | None = None,
    version: str = "local",
    compute_sha256: bool = False,
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    source_root = _resolve_directory(root)
    if not version.strip():
        raise ModelPackError("model pack version is required")
    files: list[dict[str, Any]] = []
    total_bytes = 0
    for path, role in _iter_files(source_root):
        size = path.stat().st_size
        total_bytes += size
        if len(files) >= max_files:
            raise ModelPackError("model pack contains too many recognized files")
        if total_bytes > max_bytes:
            raise ModelPackError("model pack exceeds the size limit")
        entry: dict[str, Any] = {
            "path": path.relative_to(source_root).as_posix(),
            "role": role,
            "sizeBytes": size,
        }
        if compute_sha256:
            entry["sha256"] = _sha256(path)
        files.append(entry)
    if not files:
        raise ModelPackError(
            "model pack contains no recognized GPT, SoVITS, audio or pretrained files"
        )
    role_counts: dict[str, int] = {}
    for item in files:
        role = str(item["role"])
        role_counts[role] = role_counts.get(role, 0) + 1
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": MODEL_PACK_KIND,
        "packId": _safe_id(pack_id or source_root.name, "local-model-pack"),
        "version": version.strip(),
        "source": "local",
        "files": files,
        "summary": {"fileCount": len(files), "bytes": total_bytes, "roles": role_counts},
        "voices": _voice_entries(files),
    }
    runtime = _normalize_runtime(source_root)
    if runtime is not None:
        manifest["runtime"] = runtime
    return validate_model_pack_manifest(manifest, source_root, verify_hash=compute_sha256)


def write_model_pack_manifest(path: Path | str, manifest: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, target)
