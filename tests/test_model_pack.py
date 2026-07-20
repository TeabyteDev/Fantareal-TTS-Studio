from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from fantareal_tts_studio import model_pack
from fantareal_tts_studio.model_pack import (
    MODEL_PACK_KIND,
    ModelPackError,
    scan_model_pack,
    validate_model_pack_manifest,
)


def write_fixture_pack(root: Path) -> None:
    files = {
        "runtime/GPT-SoVITS/GPT_SoVITS/pretrained_models/s2G.pth": b"pretrained",
        "runtime/voices/gpt/hero.ckpt": b"gpt-weights",
        "runtime/voices/sovits/hero.pth": b"sovits-weights",
        "runtime/voices/audio/hero.wav": b"RIFF-fixture",
        "runtime/ignored.txt": b"not a model asset",
    }
    for relative, payload in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)


def test_scan_model_pack_discovers_roles_and_voice(tmp_path: Path) -> None:
    write_fixture_pack(tmp_path)

    manifest = scan_model_pack(tmp_path)

    assert manifest["kind"] == MODEL_PACK_KIND
    assert manifest["summary"] == {
        "fileCount": 4,
        "bytes": 47,
        "roles": {"pretrained": 1, "gpt": 1, "sovits": 1, "audio": 1},
    }
    assert manifest["voices"] == [
        {
            "id": "hero",
            "name": "hero",
            "gptWeights": "runtime/voices/gpt/hero.ckpt",
            "sovitsWeights": "runtime/voices/sovits/hero.pth",
            "referenceAudio": "runtime/voices/audio/hero.wav",
        }
    ]


def test_scan_model_pack_can_include_sha256(tmp_path: Path) -> None:
    write_fixture_pack(tmp_path)

    manifest = scan_model_pack(tmp_path, compute_sha256=True)
    hero = next(item for item in manifest["files"] if item["path"].endswith("hero.ckpt"))

    assert hero["sha256"] == hashlib.sha256(b"gpt-weights").hexdigest()
    assert validate_model_pack_manifest(manifest, tmp_path, verify_hash=True) == manifest


def test_manifest_rejects_size_mismatch(tmp_path: Path) -> None:
    write_fixture_pack(tmp_path)
    manifest = scan_model_pack(tmp_path)
    manifest["files"][0]["sizeBytes"] += 1

    with pytest.raises(ModelPackError, match="size mismatch"):
        validate_model_pack_manifest(manifest, tmp_path)


def test_manifest_rejects_hash_mismatch(tmp_path: Path) -> None:
    write_fixture_pack(tmp_path)
    manifest = scan_model_pack(tmp_path, compute_sha256=True)
    manifest["files"][0]["sha256"] = "0" * 64

    with pytest.raises(ModelPackError, match="sha256 mismatch"):
        validate_model_pack_manifest(manifest, tmp_path, verify_hash=True)


def test_scan_rejects_symlink_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_fixture_pack(tmp_path)
    target = tmp_path / "runtime/voices/gpt/hero.ckpt"
    real_islink = model_pack.os.path.islink

    def fake_islink(value: object) -> bool:
        return Path(value) == target or real_islink(value)

    monkeypatch.setattr(model_pack.os.path, "islink", fake_islink)

    with pytest.raises(ModelPackError, match="symbolic-link"):
        scan_model_pack(tmp_path)


def test_scan_rejects_oversized_pack(tmp_path: Path) -> None:
    write_fixture_pack(tmp_path)

    with pytest.raises(ModelPackError, match="size limit"):
        scan_model_pack(tmp_path, max_bytes=10)


def test_manifest_json_is_serializable(tmp_path: Path) -> None:
    write_fixture_pack(tmp_path)

    manifest = scan_model_pack(tmp_path)

    assert json.loads(json.dumps(manifest, ensure_ascii=False))["packId"] == tmp_path.name.lower()
