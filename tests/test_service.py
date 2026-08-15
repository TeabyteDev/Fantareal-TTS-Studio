from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from fantareal_tts_studio import service as service_module
from fantareal_tts_studio.runtime_installer import RUNTIME_COMMIT, InstallerConfig, RuntimeInstaller
from fantareal_tts_studio.service import PROVIDER_ID, TtsStudioService, handle_request, run
from fantareal_tts_studio.supervision import InterprocessFileLock, probe_windows_owner_endpoint


class FakeProcess:
    def __init__(self, command: list[str], **kwargs: object) -> None:
        self.command = command
        self.kwargs = kwargs
        self.pid = 4242
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


class FakeGptSovitsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/openapi.json":
            payload = json.dumps({"paths": {"/tts": {"post": {}}}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith(("/set_gpt_weights", "/set_sovits_weights")):
            self.send_response(200)
            self.end_headers()
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if self.path != "/tts":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        if request.get("text") != "你好":
            self.send_error(400)
            return
        payload = b"RIFF-http-generated"
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def fake_gpt_sovits() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGptSovitsHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def initialize(service: TtsStudioService, root: Path) -> dict:
    paths = {name: root / name for name in ("workspace", "settings", "data", "cache", "assets")}
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    response = handle_request(
        service,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "extension.initialize",
            "params": {
                "workspace": str(paths["workspace"]),
                "permissions": [
                    "storage.settings",
                    "storage.data",
                    "storage.cache",
                    "storage.assets",
                ],
                "storage": {
                    "paths": {
                        name: str(paths[name]) for name in ("settings", "data", "cache", "assets")
                    },
                    "quotas": {},
                },
            },
        },
    )
    assert response is not None
    assert "result" in response
    return paths


def install_runtime_pointer(paths: dict[str, Path]) -> dict[str, str]:
    version_root = paths["assets"] / "runtime" / "versions" / RUNTIME_COMMIT
    runtime_root = version_root / "GPT-SoVITS"
    python_path = version_root / "python" / "Scripts" / "python.exe"
    runtime_root.mkdir(parents=True)
    python_path.parent.mkdir(parents=True)
    (runtime_root / "api_v2.py").write_text("print('fixture')\n", encoding="utf-8")
    python_path.write_bytes(b"fixture-python")
    pointer = {
        "version": "fixture",
        "commit": RUNTIME_COMMIT,
        "runtimeRoot": str(runtime_root),
        "python": str(python_path),
        "device": "cpu",
    }
    current = paths["assets"] / "runtime" / "current.json"
    current.write_text(json.dumps(pointer), encoding="utf-8")
    return pointer


def test_requires_initialize() -> None:
    response = handle_request(
        TtsStudioService(),
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tts.listVoices",
            "params": {"providerId": PROVIDER_ID},
        },
    )
    assert response is not None
    assert response["error"]["code"] == -32001


def test_initialize_creates_namespaced_state_and_lists_voice(tmp_path: Path) -> None:
    service = TtsStudioService()
    paths = initialize(service, tmp_path)
    response = handle_request(
        service,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tts.listVoices",
            "params": {"providerId": PROVIDER_ID},
        },
    )
    assert response is not None
    assert response["result"]["activeVoiceId"] == "default"
    assert response["result"]["voices"][0]["name"] == "默认声线"
    assert (paths["settings"] / "settings.json").is_file()
    assert (paths["data"] / "history.json").is_file()
    assert (paths["assets"] / "voices" / "audio").is_dir()


def test_settings_survive_service_rebuild(tmp_path: Path) -> None:
    first = TtsStudioService()
    initialize(first, tmp_path)
    saved = handle_request(
        first,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "ttsStudio.saveSettings",
            "params": {
                "settings": {
                    "apiUrl": "http://localhost:9880",
                    "activeVoiceId": "hero",
                    "voices": [{"id": "hero", "name": "Hero", "locale": "zh-CN"}],
                }
            },
        },
    )
    assert saved is not None
    assert saved["result"]["settings"]["activeVoiceId"] == "hero"

    second = TtsStudioService()
    initialize(second, tmp_path)
    assert second.get_settings()["voices"][0]["name"] == "Hero"


def test_import_asset_from_workspace_and_reject_escape(tmp_path: Path) -> None:
    service = TtsStudioService()
    paths = initialize(service, tmp_path)
    source = paths["workspace"] / "input" / "reference.wav"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"RIFF-fixture")

    imported = handle_request(
        service,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "ttsStudio.importAsset",
            "params": {"kind": "audio", "path": "input/reference.wav", "name": "reference.wav"},
        },
    )
    assert imported is not None
    relative = imported["result"]["item"]["path"]
    assert relative == "voices/audio/reference.wav"
    assert (paths["assets"] / relative).read_bytes() == b"RIFF-fixture"

    escaped = handle_request(
        service,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "ttsStudio.importAsset",
            "params": {"kind": "audio", "path": "../outside.wav"},
        },
    )
    assert escaped is not None
    assert escaped["error"]["code"] == -32602


def test_inspect_model_pack_is_workspace_relative_and_read_only(tmp_path: Path) -> None:
    service = TtsStudioService()
    paths = initialize(service, tmp_path)
    pack = paths["workspace"] / "model-pack"
    (pack / "runtime/voices/gpt").mkdir(parents=True)
    (pack / "runtime/voices/sovits").mkdir(parents=True)
    (pack / "runtime/voices/audio").mkdir(parents=True)
    (pack / "runtime/voices/gpt/hero.ckpt").write_bytes(b"gpt")
    (pack / "runtime/voices/sovits/hero.pth").write_bytes(b"sovits")
    (pack / "runtime/voices/audio/hero.wav").write_bytes(b"audio")

    inspected = handle_request(
        service,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "ttsStudio.inspectModelPack",
            "params": {"path": "model-pack", "computeSha256": True},
        },
    )

    assert inspected is not None
    manifest = inspected["result"]["manifest"]
    assert manifest["summary"]["fileCount"] == 3
    assert manifest["voices"][0]["id"] == "hero"
    assert not (paths["assets"] / "model-packs").exists()

    escaped = handle_request(
        service,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "ttsStudio.inspectModelPack",
            "params": {"path": "../model-pack"},
        },
    )
    assert escaped is not None
    assert escaped["error"]["code"] == -32602


def test_inspect_model_pack_accepts_host_directory_grant_without_copying(
    tmp_path: Path,
) -> None:
    service = TtsStudioService()
    paths = initialize(service, tmp_path)
    pack = tmp_path / "legacy-webui-models"
    (pack / "voices/gpt").mkdir(parents=True)
    (pack / "voices/gpt/hero.ckpt").write_bytes(b"gpt")
    token = "12345678-1234-1234-1234-123456789abc"
    grant_dir = paths["workspace"] / "input-directory-grants"
    grant_dir.mkdir(parents=True)
    (grant_dir / f"{token}.json").write_text(
        json.dumps(
            {
                "kind": "fantareal.directory-grant",
                "token": token,
                "path": str(pack),
                "readOnly": True,
            }
        ),
        encoding="utf-8",
    )

    inspected = handle_request(
        service,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "ttsStudio.inspectModelPack",
            "params": {"directoryToken": token},
        },
    )

    assert inspected is not None
    assert inspected["result"]["manifest"]["summary"]["fileCount"] == 1
    assert not (paths["assets"] / "model-packs").exists()


def test_activate_model_pack_persists_external_references_and_runtime_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = TtsStudioService()
    paths = initialize(service, tmp_path)
    pack = tmp_path / "legacy-webui-models"
    for directory in (
        "pretrained_models/chinese-roberta-wwm-ext-large",
        "pretrained_models/chinese-hubert-base",
        "voices/gpt",
        "voices/sovits",
        "voices/audio",
    ):
        (pack / directory).mkdir(parents=True)
    (pack / "pretrained_models/chinese-roberta-wwm-ext-large/config.json").write_text("{}")
    (pack / "pretrained_models/chinese-hubert-base/config.json").write_text("{}")
    (pack / "voices/gpt/hero.ckpt").write_bytes(b"gpt")
    (pack / "voices/sovits/hero.pth").write_bytes(b"sovits")
    (pack / "voices/audio/ref.wav").write_bytes(b"audio")
    token = "12345678-1234-1234-1234-123456789abc"
    grant_dir = paths["workspace"] / "input-directory-grants"
    grant_dir.mkdir(parents=True)
    (grant_dir / f"{token}.json").write_text(
        json.dumps(
            {
                "kind": "fantareal.directory-grant",
                "token": token,
                "path": str(pack),
                "readOnly": True,
            }
        ),
        encoding="utf-8",
    )

    activated = handle_request(
        service,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "ttsStudio.activateModelPack",
            "params": {"directoryToken": token, "packId": "legacy-webui-models"},
        },
    )

    assert activated is not None
    assert activated["result"]["active"]["packId"] == "legacy-webui-models"
    assert "model-pack:voices/gpt/hero.ckpt" in activated["result"]["assets"]["gpt"]
    readiness = service.readiness()
    checks = {item["id"]: item for item in readiness["checks"]}
    assert checks["gptWeights"] == {
        "id": "gptWeights",
        "ok": False,
        "code": "gpt_weights_missing",
        "message": "active voice GPT weights are not configured",
    }
    assert checks["sovitsWeights"] == {
        "id": "sovitsWeights",
        "ok": False,
        "code": "sovits_weights_missing",
        "message": "active voice SoVITS weights are not configured",
    }
    with pytest.raises(
        service_module.RpcFailure, match="active voice GPT weights are not configured"
    ) as exc:
        service._prepare_runtime_config(
            service.get_settings(), service.get_settings()["voices"][0]
        )
    assert exc.value.code == -32058
    service.save_settings(
        {
            "activeVoiceId": "hero",
            "voices": [
                {
                    "id": "hero",
                    "name": "Hero",
                    "gptWeights": "model-pack:voices/gpt/hero.ckpt",
                    "sovitsWeights": "model-pack:voices/sovits/hero.pth",
                    "referenceAudio": "model-pack:voices/audio/ref.wav",
                    "promptText": "fixture",
                }
            ],
        }
    )
    config_path = service._prepare_runtime_config(
        service.get_settings(), service.get_settings()["voices"][0]
    )
    assert config_path is not None
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["custom"]["t2s_weights_path"].endswith("voices\\gpt\\hero.ckpt")
    assert config["custom"]["vits_weights_path"].endswith("voices\\sovits\\hero.pth")

    pointer = install_runtime_pointer(paths)
    nltk_data = Path(pointer["python"]).parent.parent / "nltk_data"
    nltk_data.mkdir()
    processes: list[FakeProcess] = []

    def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
        process = FakeProcess(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(service, "probe", lambda: {"available": False, "message": "offline"})
    monkeypatch.setattr(service_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        service, "_terminate_process", lambda process: setattr(process, "returncode", 0)
    )
    launched = service.launch_runtime()
    assert launched["running"] is True
    assert processes[0].command[-2:] == ["-c", str(config_path)]
    assert processes[0].kwargs["env"]["NLTK_DATA"] == str(nltk_data)
    service.stop_runtime()

    calls: list[str] = []

    def fake_request(url: str, **_kwargs: object) -> bytes:
        calls.append(url)
        return b"RIFF-external"

    monkeypatch.setattr(
        service_module.TtsStudioService, "_request_bytes", staticmethod(fake_request)
    )
    generated = service.synthesize(
        {
            "providerId": PROVIDER_ID,
            "voiceId": "hero",
            "requestId": "external-model-pack",
            "text": "fixture",
        }
    )
    assert Path(generated["audio"]["path"]).read_bytes() == b"RIFF-external"
    assert any("hero.ckpt" in call for call in calls)
    assert any("hero.pth" in call for call in calls)

    rebuilt = TtsStudioService()
    initialize(rebuilt, tmp_path)
    assert rebuilt.active_model_pack()["packId"] == "legacy-webui-models"
    deactivated = rebuilt.deactivate_model_pack()
    assert deactivated["active"] is None


def test_gpt_sovits_http_error_includes_backend_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = service_module.urllib.error.HTTPError(
        "http://127.0.0.1:9880/tts",
        400,
        "Bad Request",
        {},
        io.BytesIO(b'{"message":"tts failed","Exception":"Resource cmudict not found"}'),
    )
    monkeypatch.setattr(
        service_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )

    with pytest.raises(service_module.RpcFailure, match="Resource cmudict not found"):
        TtsStudioService._request_bytes(
            "http://127.0.0.1:9880/tts",
            method="POST",
            timeout=1,
            body=b"{}",
            headers={"Content-Type": "application/json"},
        )


def test_synthesize_writes_managed_cache_and_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = TtsStudioService()
    paths = initialize(service, tmp_path)
    reference = paths["assets"] / "voices" / "audio" / "ref.wav"
    reference.write_bytes(b"RIFF-reference")
    service.save_settings(
        {
            "voices": [
                {
                    "id": "hero",
                    "name": "Hero",
                    "referenceAudio": "voices/audio/ref.wav",
                    "promptText": "参考文本",
                }
            ],
            "activeVoiceId": "hero",
        }
    )
    monkeypatch.setattr(service, "_synthesize_audio", lambda *_args: b"RIFF-generated")

    response = handle_request(
        service,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tts.synthesize",
            "params": {
                "providerId": PROVIDER_ID,
                "voiceId": "hero",
                "requestId": "req-1",
                "text": "你好",
            },
        },
    )
    assert response is not None
    audio = Path(response["result"]["audio"]["path"])
    assert audio.read_bytes() == b"RIFF-generated"
    assert audio.parent == paths["cache"] / "audio"
    assert service.read_history()[0]["voiceId"] == "hero"


def test_synthesize_reports_unconfigured_reference_audio(tmp_path: Path) -> None:
    service = TtsStudioService()
    initialize(service, tmp_path)

    response = handle_request(
        service,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tts.synthesize",
            "params": {
                "providerId": PROVIDER_ID,
                "voiceId": "default",
                "requestId": "missing-reference",
                "text": "voice readiness check",
            },
        },
    )

    assert response is not None
    assert response["error"] == {
        "code": -32043,
        "message": "voice reference audio is not configured",
    }


def test_loopback_health_and_synthesis_contract(tmp_path: Path) -> None:
    service = TtsStudioService()
    paths = initialize(service, tmp_path)
    reference = paths["assets"] / "voices" / "audio" / "ref.wav"
    reference.write_bytes(b"RIFF-reference")
    with fake_gpt_sovits() as api_url:
        service.save_settings(
            {
                "apiUrl": api_url,
                "voices": [
                    {
                        "id": "hero",
                        "name": "Hero",
                        "referenceAudio": "voices/audio/ref.wav",
                        "promptText": "参考文本",
                    }
                ],
                "activeVoiceId": "hero",
            }
        )
        health = service.dispatch("tts.health", {"providerId": PROVIDER_ID})
        result = service.dispatch(
            "tts.synthesize",
            {
                "providerId": PROVIDER_ID,
                "voiceId": "hero",
                "requestId": "http-request",
                "text": "你好",
            },
        )

    assert health["available"] is True
    audio = Path(result["audio"]["path"])
    assert audio.read_bytes() == b"RIFF-http-generated"
    assert audio.parent == paths["cache"] / "audio"


def test_preview_returns_bounded_base64_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = TtsStudioService()
    paths = initialize(service, tmp_path)
    reference = paths["assets"] / "voices" / "audio" / "ref.wav"
    reference.write_bytes(b"RIFF-reference")
    service.save_settings(
        {
            "voices": [
                {
                    "id": "hero",
                    "name": "Hero",
                    "referenceAudio": "voices/audio/ref.wav",
                    "promptText": "fixture",
                }
            ],
            "activeVoiceId": "hero",
        }
    )
    monkeypatch.setattr(service, "_synthesize_audio", lambda *_args: b"RIFF-preview")

    result = service.preview({"voiceId": "hero", "requestId": "preview-1", "text": "preview"})

    assert result["requestId"] == "preview-1"
    assert base64.b64decode(result["audio"]["base64"]) == b"RIFF-preview"
    assert result["audio"]["size"] == len(b"RIFF-preview")


def test_preview_over_limit_removes_cache_and_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = TtsStudioService()
    paths = initialize(service, tmp_path)
    reference = paths["assets"] / "voices" / "audio" / "ref.wav"
    reference.write_bytes(b"RIFF-reference")
    service.save_settings(
        {
            "voices": [
                {
                    "id": "hero",
                    "name": "Hero",
                    "referenceAudio": "voices/audio/ref.wav",
                    "promptText": "fixture",
                }
            ],
            "activeVoiceId": "hero",
        }
    )
    monkeypatch.setattr(service, "_synthesize_audio", lambda *_args: b"012345678")
    monkeypatch.setattr(service_module, "MAX_PREVIEW_AUDIO_BYTES", 8)

    with pytest.raises(service_module.RpcFailure, match="transfer limit"):
        service.preview({"voiceId": "hero", "text": "preview"})

    assert service.read_history() == []
    assert list((paths["cache"] / "audio").iterdir()) == []


def test_readiness_classifies_missing_voice_configuration(tmp_path: Path) -> None:
    service = TtsStudioService()
    initialize(service, tmp_path)

    result = service.readiness()

    assert result["ready"] is False
    assert result["status"] == "reference_audio_missing"
    assert any(
        item["id"] == "referenceAudio" and item["code"] == "reference_audio_missing"
        for item in result["checks"]
    )


def test_runtime_smoke_waits_for_api_and_returns_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = TtsStudioService()
    paths = initialize(service, tmp_path)
    install_runtime_pointer(paths)
    (paths["assets"] / "voices" / "audio" / "hero.wav").write_bytes(b"RIFF-reference")
    service.save_settings(
        {
            "apiUrl": "http://127.0.0.1:9880",
            "activeVoiceId": "hero",
            "voices": [
                {
                    "id": "hero",
                    "name": "Hero",
                    "referenceAudio": "voices/audio/hero.wav",
                    "promptText": "fixture",
                }
            ],
        }
    )

    with fake_gpt_sovits() as api_url:
        service.save_settings({"apiUrl": api_url})
        service.runtime_process = FakeProcess([])
        result = service.runtime_smoke({"text": "你好", "autoLaunch": False})

    assert result["ok"] is True
    assert result["status"] == "ready"
    assert base64.b64decode(result["audio"]["base64"]) == b"RIFF-http-generated"


def test_external_api_is_ready_without_plugin_managed_runtime(tmp_path: Path) -> None:
    service = TtsStudioService()
    paths = initialize(service, tmp_path)
    (paths["assets"] / "voices" / "audio" / "hero.wav").write_bytes(b"RIFF-reference")

    with fake_gpt_sovits() as api_url:
        service.save_settings(
            {
                "apiUrl": api_url,
                "activeVoiceId": "hero",
                "voices": [
                    {
                        "id": "hero",
                        "name": "Hero",
                        "referenceAudio": "voices/audio/hero.wav",
                        "promptText": "fixture",
                    }
                ],
            }
        )
        readiness = service.readiness()
        result = service.runtime_smoke({"text": "你好", "autoLaunch": False})

    assert readiness["ready"] is True
    assert readiness["message"] == "external GPT-SoVITS API is ready"
    assert readiness["runtime"]["managed"] is False
    assert result["ok"] is True


def test_runtime_smoke_reports_timeout_and_preserves_runtime_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = TtsStudioService()
    paths = initialize(service, tmp_path)
    install_runtime_pointer(paths)
    (paths["assets"] / "voices" / "audio" / "hero.wav").write_bytes(b"RIFF-reference")
    service.save_settings(
        {
            "activeVoiceId": "hero",
            "voices": [
                {
                    "id": "hero",
                    "name": "Hero",
                    "referenceAudio": "voices/audio/hero.wav",
                    "promptText": "fixture",
                }
            ],
        }
    )
    service.runtime_process = FakeProcess([])
    service.runtime_log_path.write_text("boot failed\n", encoding="utf-8")
    monkeypatch.setattr(service, "probe", lambda: {"available": False, "message": "API booting"})

    clock = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(service_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(service_module.time, "sleep", lambda _seconds: None)

    result = service.runtime_smoke({"timeoutSeconds": 1, "autoLaunch": False})

    assert result["ok"] is False
    assert result["status"] == "api_not_ready"
    assert "boot failed" in result["runtimeLog"]


def test_readiness_classifies_runtime_port_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = TtsStudioService()
    paths = initialize(service, tmp_path)
    install_runtime_pointer(paths)
    (paths["assets"] / "voices" / "audio" / "hero.wav").write_bytes(b"RIFF-reference")
    service.save_settings(
        {
            "activeVoiceId": "hero",
            "voices": [
                {
                    "id": "hero",
                    "name": "Hero",
                    "referenceAudio": "voices/audio/hero.wav",
                    "promptText": "fixture",
                }
            ],
        }
    )
    service.runtime_process = FakeProcess([])
    service.runtime_log_path.write_text("[Errno 10048] address already in use\n", encoding="utf-8")
    monkeypatch.setattr(service, "probe", lambda: {"available": False, "message": "API offline"})

    readiness = service.readiness()

    assert readiness["ready"] is False
    assert readiness["status"] == "api_port_conflict"
    assert readiness["message"] == "GPT-SoVITS API port is already in use"


def test_runtime_status_reads_validated_current_pointer_and_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = TtsStudioService()
    paths = initialize(service, tmp_path)
    pointer = install_runtime_pointer(paths)
    processes: list[FakeProcess] = []

    def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
        process = FakeProcess(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(service, "probe", lambda: {"available": False, "message": "offline"})
    monkeypatch.setattr(service_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        service,
        "_terminate_process",
        lambda process: setattr(process, "returncode", 0),
    )

    status = service.runtime_status()
    launched = service.launch_runtime()

    assert status["runtimeRoot"] == pointer["runtimeRoot"]
    assert status["python"] == pointer["python"]
    assert launched["running"] is True
    assert processes[0].command[:2] == [pointer["python"], f"{pointer['runtimeRoot']}\\api_v2.py"]
    assert processes[0].command[-4:] == ["-a", "127.0.0.1", "-p", "9880"]
    assert service.stop_runtime()["running"] is False


def test_runtime_install_starts_async_and_cancel_preserves_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = TtsStudioService()
    paths = initialize(service, tmp_path)
    pointer = install_runtime_pointer(paths)
    current_path = paths["assets"] / "runtime" / "current.json"
    current_before = current_path.read_bytes()
    service.runtime_install_log_path.write_text("previous install log\n", encoding="utf-8")
    staging = paths["assets"] / "runtime" / ".staging-fixture"
    staging.mkdir()
    processes: list[FakeProcess] = []

    def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
        process = FakeProcess(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(service, "probe", lambda: {"available": False, "message": "offline"})
    monkeypatch.setattr(service_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(service, "_wait_for_runtime_install_handoff", lambda *_args: True)
    monkeypatch.setattr(
        service,
        "_runtime_install_owner_alive",
        lambda state: bool(state.get("installId")),
    )
    started = service.runtime_install({"device": "cu126", "source": "online"})

    assert started["running"] is True
    assert processes[0].command[:6] == [
        service_module.sys.executable,
        "-I",
        "-X",
        "utf8",
        "-m",
        "fantareal_tts_studio.runtime_installer",
    ]
    assert processes[0].command[-4:-2] == ["--device", "cu126"]
    assert processes[0].command[-2] == "--install-id"
    assert processes[0].command[-1] == started["installId"]
    assert service.get_settings()["runtimeDevice"] == "cu126"
    assert staging.is_dir()
    install_log = service.runtime_install_log_path.read_text(encoding="utf-8")
    assert install_log.startswith("previous install log\n")
    assert f"runtime install {started['installId']} started" in install_log

    new_staging = paths["assets"] / "runtime" / ".staging-running"
    new_staging.mkdir()
    cancelled = service.cancel_runtime_install()

    assert cancelled["status"] == "cancelling"
    assert cancelled["running"] is True
    assert cancelled["cancelRequested"] is True
    assert processes[0].poll() is None
    assert new_staging.is_dir()
    control = json.loads(
        service.runtime_install_control_path(started["installId"]).read_text(encoding="utf-8")
    )
    assert control["installId"] == started["installId"]
    assert current_path.read_bytes() == current_before
    assert cancelled["installed"]["runtimeRoot"] == pointer["runtimeRoot"]


def test_extension_shutdown_does_not_cancel_runtime_install(tmp_path: Path) -> None:
    service = TtsStudioService()
    paths = initialize(service, tmp_path)
    process = FakeProcess(["runtime-installer"])
    service.installer_process = process
    install_id = "a" * 32
    state = {
        "status": "running",
        "step": "installing_torch",
        "progress": 0.62,
        "installId": install_id,
        "pid": process.pid,
        "startedAt": service_module.utc_now(),
        "heartbeatAt": service_module.utc_now(),
        "lastOutputAt": service_module.utc_now(),
        "stagingRoot": str(paths["assets"] / "runtime" / f".staging-{install_id}"),
        "updatedAt": service_module.utc_now(),
        "error": "",
    }
    service_module.atomic_write_json(service.runtime_install_state_path, state)

    result = service.dispatch("extension.shutdown", {})

    assert result == {"stopping": True}
    assert process.poll() is None
    assert service.installer_process is process
    assert not service.runtime_install_control_path(install_id).exists()


def test_install_handoff_accepts_terminal_state_from_fast_installer(tmp_path: Path) -> None:
    service = TtsStudioService()
    initialize(service, tmp_path)
    install_id = "6" * 32
    now = service_module.utc_now()
    process = FakeProcess(["runtime-installer"])
    process.returncode = 0
    service_module.atomic_write_json(
        service.runtime_install_state_path,
        {
            "status": "completed",
            "step": "completed",
            "progress": 1.0,
            "installId": install_id,
            "ownerProtocolVersion": 1,
            "ownerAcquiredAt": now,
            "pid": process.pid,
            "startedAt": now,
            "heartbeatAt": now,
            "lastOutputAt": now,
            "updatedAt": now,
            "error": "",
        },
    )

    assert service._wait_for_runtime_install_handoff(process, install_id, timeout=0.1) is True


@pytest.mark.parametrize(
    "terminal_status",
    ["completed", "failed", "cancelled", "interrupted"],
)
def test_terminal_install_state_remains_authoritative_until_local_process_exits(
    tmp_path: Path,
    terminal_status: str,
) -> None:
    service = TtsStudioService()
    initialize(service, tmp_path)
    install_id = "7" * 32
    now = service_module.utc_now()
    process = FakeProcess(["runtime-installer"])
    service.installer_process = process
    terminal_state = {
        "status": terminal_status,
        "step": terminal_status,
        "progress": 1.0 if terminal_status == "completed" else 0.0,
        "installId": install_id,
        "ownerProtocolVersion": 1,
        "ownerPid": process.pid,
        "ownerAcquiredAt": now,
        "pid": process.pid,
        "startedAt": now,
        "heartbeatAt": now,
        "lastOutputAt": now,
        "updatedAt": now,
        "error": "fixture terminal state",
    }
    service_module.atomic_write_json(service.runtime_install_state_path, terminal_state)

    before_exit = service.runtime_install_status()
    cancelled = service.cancel_runtime_install()
    after_cancel = json.loads(service.runtime_install_state_path.read_text(encoding="utf-8"))
    control_exists = service.runtime_install_control_path(install_id).exists()
    process_retained_before_exit = service.installer_process is process
    process.returncode = 0
    after_exit = service.runtime_install_status()
    persisted_after_exit = json.loads(
        service.runtime_install_state_path.read_text(encoding="utf-8")
    )

    assert (
        before_exit["running"],
        cancelled["running"],
        cancelled["status"],
        control_exists,
        after_cancel,
        process_retained_before_exit,
        after_exit["status"],
        persisted_after_exit,
        service.installer_process,
    ) == (
        False,
        False,
        terminal_status,
        False,
        terminal_state,
        True,
        terminal_status,
        terminal_state,
        None,
    )


def test_rebuilt_service_recognizes_fresh_external_install_and_blocks_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = TtsStudioService()
    initialize(original, tmp_path)
    install_id = "b" * 32
    state = {
        "status": "running",
        "step": "installing_torch",
        "progress": 0.62,
        "installId": install_id,
        "pid": 9876,
        "startedAt": service_module.utc_now(),
        "heartbeatAt": service_module.utc_now(),
        "lastOutputAt": service_module.utc_now(),
        "stagingRoot": str(
            original._require_layout().assets / "runtime" / f".staging-{install_id}"
        ),
        "updatedAt": service_module.utc_now(),
        "error": "",
    }
    service_module.atomic_write_json(original.runtime_install_state_path, state)
    rebuilt = TtsStudioService()
    rebuilt.layout = original.layout
    monkeypatch.setattr(
        service_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("a duplicate installer must not start"),
    )

    status = rebuilt.runtime_install_status()
    duplicate = rebuilt.runtime_install({"device": "cpu", "source": "online"})

    assert status["running"] is True
    assert status["external"] is True
    assert status["installId"] == install_id
    assert duplicate["installId"] == install_id


def test_stale_external_install_becomes_interrupted_without_deleting_staging(
    tmp_path: Path,
) -> None:
    service = TtsStudioService()
    paths = initialize(service, tmp_path)
    install_id = "c" * 32
    staging = paths["assets"] / "runtime" / f".staging-{install_id}"
    staging.mkdir()
    service_module.atomic_write_json(
        service.runtime_install_state_path,
        {
            "status": "running",
            "step": "installing_torch",
            "progress": 0.62,
            "installId": install_id,
            "pid": 1234,
            "startedAt": "2000-01-01T00:00:00Z",
            "heartbeatAt": "2000-01-01T00:00:00Z",
            "lastOutputAt": "2000-01-01T00:00:00Z",
            "stagingRoot": str(staging),
            "updatedAt": "2000-01-01T00:00:00Z",
            "error": "",
        },
    )

    status = service.runtime_install_status()

    assert status["status"] == "interrupted"
    assert status["running"] is False
    assert status["external"] is False
    assert staging.is_dir()


def test_rebuilt_service_requests_install_id_bound_cooperative_cancel(tmp_path: Path) -> None:
    service = TtsStudioService()
    initialize(service, tmp_path)
    install_id = "d" * 32
    service_module.atomic_write_json(
        service.runtime_install_state_path,
        {
            "status": "running",
            "step": "installing_torch",
            "progress": 0.62,
            "installId": install_id,
            "pid": 2345,
            "startedAt": service_module.utc_now(),
            "heartbeatAt": service_module.utc_now(),
            "lastOutputAt": service_module.utc_now(),
            "stagingRoot": str(
                service._require_layout().assets / "runtime" / f".staging-{install_id}"
            ),
            "updatedAt": service_module.utc_now(),
            "error": "",
        },
    )

    status = service.cancel_runtime_install()

    control = json.loads(
        service.runtime_install_control_path(install_id).read_text(encoding="utf-8")
    )
    assert control["installId"] == install_id
    assert status["running"] is True
    assert status["external"] is True
    assert status["cancelRequested"] is True


def test_concurrent_services_share_one_runtime_install_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = TtsStudioService()
    paths = initialize(first, tmp_path)
    second = TtsStudioService()
    second.layout = first.layout
    services = [first, second]
    status_barrier = threading.Barrier(2)
    real_status = TtsStudioService.runtime_install_status
    status_calls: dict[int, int] = {}
    status_calls_lock = threading.Lock()
    real_atomic_write = service_module.atomic_write_json
    state_write_lock = threading.Lock()
    popen_commands: list[list[str]] = []
    popen_lock = threading.Lock()

    def synchronized_initial_status(service: TtsStudioService) -> dict:
        status = real_status(service)
        with status_calls_lock:
            count = status_calls.get(id(service), 0)
            status_calls[id(service)] = count + 1
        if count == 0:
            status_barrier.wait(timeout=5)
        return status

    def serialized_atomic_write(path: Path, value: object) -> None:
        with state_write_lock:
            real_atomic_write(path, value)

    def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
        del kwargs
        with popen_lock:
            popen_commands.append(command)
        return FakeProcess(command)

    monkeypatch.setattr(TtsStudioService, "runtime_install_status", synchronized_initial_status)
    monkeypatch.setattr(service_module, "atomic_write_json", serialized_atomic_write)
    monkeypatch.setattr(service_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        TtsStudioService,
        "_wait_for_runtime_install_handoff",
        lambda *_args, **_kwargs: True,
        raising=False,
    )
    monkeypatch.setattr(
        TtsStudioService,
        "_runtime_install_owner_alive",
        lambda _service, state: bool(state.get("installId")),
        raising=False,
    )
    for service in services:
        monkeypatch.setattr(service, "save_settings", lambda settings: settings)
        monkeypatch.setattr(service, "stop_runtime", lambda: {})

    results: list[dict | None] = [None, None]
    errors: list[BaseException] = []

    def launch(index: int) -> None:
        try:
            results[index] = services[index].runtime_install(
                {"device": "cpu", "source": "online"}
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    workers = [threading.Thread(target=launch, args=(index,)) for index in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    assert len(popen_commands) == 1
    assert results[0] is not None and results[1] is not None
    assert results[0]["installId"] == results[1]["installId"]
    assert (paths["data"] / "runtime-install-state.json").is_file()


def test_installer_heartbeat_cannot_overwrite_cancelling_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = TtsStudioService()
    paths = initialize(service, tmp_path)
    install_id = "3" * 32
    now = service_module.utc_now()
    state = {
        "status": "running",
        "step": "installing_torch",
        "progress": 0.62,
        "installId": install_id,
        "pid": 3456,
        "startedAt": now,
        "heartbeatAt": now,
        "lastOutputAt": now,
        "stagingRoot": str(paths["assets"] / "runtime" / f".staging-{install_id}"),
        "updatedAt": now,
        "error": "",
    }
    service_module.atomic_write_json(service.runtime_install_state_path, state)
    config = InstallerConfig(
        assets_root=paths["assets"],
        data_root=paths["data"],
        cache_root=paths["cache"],
        device="cpu",
        install_id=install_id,
    )
    installer = RuntimeInstaller(config)
    installer._state = state.copy()
    installer.started_at = now
    installer.last_output_at = now
    installer.staging_root = config.staging_root
    monkeypatch.setattr(
        TtsStudioService,
        "_runtime_install_owner_alive",
        lambda _service, current: current.get("installId") == install_id,
        raising=False,
    )

    service.cancel_runtime_install()
    installer._touch_heartbeat()
    status = service.runtime_install_status()
    persisted = json.loads(service.runtime_install_state_path.read_text(encoding="utf-8"))

    assert status["status"] == "cancelling"
    assert status["step"] == "cancelling"
    assert status["cancelRequested"] is True
    assert persisted["status"] == "cancelling"
    assert persisted["step"] == "cancelling"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows owner identity regression")
def test_owner_lock_holder_must_match_the_declared_owner_process(
    tmp_path: Path,
) -> None:
    service = TtsStudioService()
    initialize(service, tmp_path)
    install_id = "5" * 32
    ready = tmp_path / "owner-lock-ready"
    source_root = str(Path(__file__).parents[1] / "src")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = source_root
    holder_script = (
        "import sys, time; from pathlib import Path; "
        "from fantareal_tts_studio.supervision import InterprocessFileLock; "
        "lock=InterprocessFileLock(Path(sys.argv[1])); lock.acquire(); "
        "Path(sys.argv[2]).write_text('ready'); time.sleep(30)"
    )
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            holder_script,
            str(service.runtime_install_owner_lock_path),
            str(ready),
        ],
        env=environment,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    deadline = time.monotonic() + 10.0
    while not ready.is_file() and holder.poll() is None:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert holder.poll() is None
    declared_pid = holder.pid + 1_000_000
    state = {
        "status": "running",
        "step": "installing_torch",
        "progress": 0.62,
        "installId": install_id,
        "ownerProtocolVersion": 1,
        "ownerPid": declared_pid,
        "ownerAcquiredAt": "2000-01-01T00:00:00Z",
        "pid": declared_pid,
        "startedAt": "2000-01-01T00:00:00Z",
        "heartbeatAt": "2000-01-01T00:00:00Z",
        "lastOutputAt": "2000-01-01T00:00:00Z",
        "stagingRoot": str(
            service._require_layout().assets / "runtime" / f".staging-{install_id}"
        ),
        "updatedAt": "2000-01-01T00:00:00Z",
        "error": "",
    }
    service_module.atomic_write_json(service.runtime_install_state_path, state)
    service_module.atomic_write_json(
        service.runtime_install_owner_metadata_path,
        {
            "installId": install_id,
            "pid": declared_pid,
            "acquiredAt": state["ownerAcquiredAt"],
        },
    )
    try:
        status = service.runtime_install_status()
        assert status["ownerLockHeld"] is True
        assert status["ownerAlive"] is False
        assert status["ownerConflict"] is True
        assert status["external"] is False
        assert status["running"] is False
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows owner identity regression")
def test_named_pipe_owner_endpoint_authenticates_the_actual_child_process(
    tmp_path: Path,
) -> None:
    service = TtsStudioService()
    initialize(service, tmp_path)
    install_id = "6" * 32
    token = "7" * 64
    acquired_at = "2000-01-01T00:00:00Z"
    ready = tmp_path / "owner-endpoint-ready"
    source_root = str(Path(__file__).parents[1] / "src")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = source_root
    owner_script = (
        "import os, sys, time; from pathlib import Path; "
        "from fantareal_tts_studio.supervision import WindowsOwnerEndpoint; "
        "endpoint=WindowsOwnerEndpoint(Path(sys.argv[1]), install_id=sys.argv[2], "
        "owner_token=sys.argv[3]); endpoint.start(); "
        "Path(sys.argv[4]).write_text(str(os.getpid())); time.sleep(30)"
    )
    owner = subprocess.Popen(
        [
            sys.executable,
            "-c",
            owner_script,
            str(service._require_layout().data),
            install_id,
            token,
            str(ready),
        ],
        env=environment,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        deadline = time.monotonic() + 10.0
        while not ready.is_file() and owner.poll() is None:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert owner.poll() is None
        actual_owner_pid = int(ready.read_text(encoding="utf-8"))
        state = {
            "status": "running",
            "step": "installing_torch",
            "progress": 0.62,
            "installId": install_id,
            "ownerProtocolVersion": 2,
            "ownerPid": actual_owner_pid,
            "ownerAcquiredAt": acquired_at,
            "ownerToken": token,
            "pid": actual_owner_pid,
            "startedAt": acquired_at,
            "heartbeatAt": "2000-01-01T00:00:00Z",
            "lastOutputAt": "2000-01-01T00:00:00Z",
            "updatedAt": "2000-01-01T00:00:00Z",
            "error": "",
        }
        service_module.atomic_write_json(service.runtime_install_state_path, state)
        service_module.atomic_write_json(
            service.runtime_install_owner_metadata_path,
            {
                "ownerProtocolVersion": 2,
                "installId": install_id,
                "pid": actual_owner_pid,
                "acquiredAt": acquired_at,
                "ownerToken": token,
            },
        )

        direct_probe = probe_windows_owner_endpoint(service._require_layout().data)
        assert direct_probe.status == "verified"
        assert direct_probe.actual_pid == actual_owner_pid
        assert direct_probe.identity is not None
        assert direct_probe.identity.get("ownerToken") == token

        status = service.runtime_install_status()

        assert status["ownerAlive"] is True
        assert status["ownerConflict"] is False
        assert status["external"] is True
        assert status["running"] is True
        assert status["pid"] == actual_owner_pid
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)


@pytest.mark.parametrize(
    ("state_identity", "metadata_identity"),
    [
        ({"ownerPid": 4567}, {"pid": 7654}),
        (
            {"ownerAcquiredAt": "2000-01-01T00:00:00Z"},
            {"acquiredAt": "2000-01-01T00:00:01Z"},
        ),
    ],
    ids=["pid-mismatch", "acquired-at-mismatch"],
)
def test_owner_lock_does_not_validate_mismatched_owner_identity(
    tmp_path: Path,
    state_identity: dict[str, object],
    metadata_identity: dict[str, object],
) -> None:
    service = TtsStudioService()
    initialize(service, tmp_path)
    install_id = "8" * 32
    state = {
        "status": "running",
        "step": "installing_torch",
        "progress": 0.62,
        "installId": install_id,
        "ownerProtocolVersion": 1,
        "ownerPid": 4567,
        "ownerAcquiredAt": "2000-01-01T00:00:00Z",
        "pid": 4567,
        "startedAt": "2000-01-01T00:00:00Z",
        "heartbeatAt": "2000-01-01T00:00:00Z",
        "lastOutputAt": "2000-01-01T00:00:00Z",
        "updatedAt": "2000-01-01T00:00:00Z",
        "error": "",
        **state_identity,
    }
    owner = {
        "installId": install_id,
        "pid": 4567,
        "acquiredAt": "2000-01-01T00:00:00Z",
        **metadata_identity,
    }
    service_module.atomic_write_json(service.runtime_install_state_path, state)
    service_module.atomic_write_json(service.runtime_install_owner_metadata_path, owner)

    with InterprocessFileLock(service.runtime_install_owner_lock_path):
        status = service.runtime_install_status()

    assert status["ownerLockHeld"] is True
    assert status["ownerAlive"] is False
    assert status["ownerConflict"] is True
    assert status["external"] is False
    assert status["running"] is False


def test_runtime_install_uses_active_complete_bundle_and_auto_cuda(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = TtsStudioService()
    paths = initialize(service, tmp_path)
    pack = tmp_path / "complete-tts-studio"
    runtime = pack / "runtime" / "GPT-SoVITS"
    voice_root = pack / "runtime" / "voices" / "gpt"
    voice_root.mkdir(parents=True)
    (voice_root / "hero.ckpt").write_bytes(b"gpt")
    runtime.mkdir(parents=True)
    for name in ("api_v2.py", "requirements.txt", "extra-req.txt"):
        (runtime / name).write_text("# fixture\n", encoding="utf-8")
    token = "12345678-1234-1234-1234-123456789abc"
    grant_dir = paths["workspace"] / "input-directory-grants"
    grant_dir.mkdir(parents=True)
    (grant_dir / f"{token}.json").write_text(
        json.dumps(
            {
                "kind": "fantareal.directory-grant",
                "token": token,
                "path": str(pack),
                "readOnly": True,
            }
        ),
        encoding="utf-8",
    )
    service.activate_model_pack({"directoryToken": token, "packId": "complete-pack"})
    processes: list[FakeProcess] = []

    def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
        process = FakeProcess(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(service_module.shutil, "which", lambda name: "nvidia-smi.exe")
    monkeypatch.setattr(service_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(service, "_wait_for_runtime_install_handoff", lambda *_args: True)
    monkeypatch.setattr(
        service,
        "_runtime_install_owner_alive",
        lambda state: bool(state.get("installId")),
    )

    started = service.runtime_install({"device": "auto"})

    assert started["running"] is True
    command = processes[0].command
    assert command[command.index("--source-runtime-root") + 1] == str(runtime.resolve())
    assert command[command.index("--device") + 1] == "cu126"
    assert service.get_settings()["runtimeDevice"] == "auto"


def test_runtime_pointer_accepts_only_active_local_bundle(
    tmp_path: Path,
) -> None:
    service = TtsStudioService()
    paths = initialize(service, tmp_path)
    pack = tmp_path / "complete-tts-studio"
    runtime = pack / "runtime" / "GPT-SoVITS"
    voice_root = pack / "runtime" / "voices" / "gpt"
    voice_root.mkdir(parents=True)
    (voice_root / "hero.ckpt").write_bytes(b"gpt")
    runtime.mkdir(parents=True)
    for name in ("api_v2.py", "requirements.txt", "extra-req.txt"):
        (runtime / name).write_text("# fixture\n", encoding="utf-8")
    token = "12345678-1234-1234-1234-123456789abc"
    grant_dir = paths["workspace"] / "input-directory-grants"
    grant_dir.mkdir(parents=True)
    (grant_dir / f"{token}.json").write_text(
        json.dumps(
            {
                "kind": "fantareal.directory-grant",
                "token": token,
                "path": str(pack),
                "readOnly": True,
            }
        ),
        encoding="utf-8",
    )
    service.activate_model_pack({"directoryToken": token, "packId": "complete-pack"})
    python = (
        paths["assets"]
        / "runtime"
        / "environments"
        / "fixture-cu126"
        / "python"
        / "Scripts"
        / "python.exe"
    )
    python.parent.mkdir(parents=True)
    python.write_bytes(b"fixture-python")
    pointer = {
        "version": "local-bundle",
        "commit": "",
        "sourceType": "local-bundle",
        "runtimeKey": "fixture",
        "runtimeRoot": str(runtime),
        "python": str(python),
        "device": "cu126",
    }
    current = paths["assets"] / "runtime" / "current.json"
    current.write_text(json.dumps(pointer), encoding="utf-8")

    assert service.runtime_status()["installed"]["sourceType"] == "local-bundle"

    service.active_model_pack_path.unlink()
    assert service.runtime_status()["installed"] is None


def test_line_protocol_initialize_and_shutdown(tmp_path: Path) -> None:
    paths = {name: tmp_path / name for name in ("workspace", "settings", "data", "cache", "assets")}
    for path in paths.values():
        path.mkdir(parents=True)
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "extension.initialize",
            "params": {
                "workspace": str(paths["workspace"]),
                "permissions": [
                    "storage.settings",
                    "storage.data",
                    "storage.cache",
                    "storage.assets",
                ],
                "storage": {
                    "paths": {
                        name: str(paths[name]) for name in ("settings", "data", "cache", "assets")
                    }
                },
            },
        },
        {"jsonrpc": "2.0", "id": 2, "method": "extension.shutdown", "params": {}},
    ]
    input_stream = io.StringIO("".join(json.dumps(request) + "\n" for request in requests))
    output_stream = io.StringIO()
    assert run(input_stream, output_stream) == 0
    responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert responses[0]["result"]["providerId"] == PROVIDER_ID
    assert responses[1]["result"]["stopping"] is True
