from __future__ import annotations

import base64
import io
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from fantareal_tts_studio import service as service_module
from fantareal_tts_studio.runtime_installer import RUNTIME_COMMIT
from fantareal_tts_studio.service import PROVIDER_ID, TtsStudioService, handle_request, run


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
    staging = paths["assets"] / "runtime" / ".staging-fixture"
    staging.mkdir()
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
        lambda process: setattr(process, "returncode", -1),
    )

    started = service.runtime_install({"device": "cu126"})

    assert started["running"] is True
    assert processes[0].command[:6] == [
        service_module.sys.executable,
        "-I",
        "-X",
        "utf8",
        "-m",
        "fantareal_tts_studio.runtime_installer",
    ]
    assert processes[0].command[-2:] == ["--device", "cu126"]
    assert service.get_settings()["runtimeDevice"] == "cu126"
    assert not staging.exists()

    new_staging = paths["assets"] / "runtime" / ".staging-running"
    new_staging.mkdir()
    cancelled = service.cancel_runtime_install()

    assert cancelled["status"] == "cancelled"
    assert cancelled["running"] is False
    assert not new_staging.exists()
    assert current_path.read_bytes() == current_before
    assert cancelled["installed"]["runtimeRoot"] == pointer["runtimeRoot"]


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
