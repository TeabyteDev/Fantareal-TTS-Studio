# Phase 12：TTS runtime readiness 与声音自检

Phase 11 已经可以持久化激活外部模型包，并在启动 GPT-SoVITS 时生成 custom config。Phase 12 在此基础上增加“为什么现在不能出声”的结构化诊断，以及一次真正调用 `/tts` 的声音自检。

## `ttsStudio.readiness`

该 RPC 只做检查，不会启动 runtime，也不会复制或修改外部模型目录：

```json
{
  "ready": false,
  "status": "reference_audio_missing",
  "message": "active voice reference audio is not configured",
  "checks": [
    {"id": "modelPack", "ok": true, "code": "ok", "message": "active model pack is valid"},
    {"id": "referenceAudio", "ok": false, "code": "reference_audio_missing", "message": "..."},
    {"id": "runtimeInstalled", "ok": false, "code": "runtime_not_installed", "message": "..."},
    {"id": "api", "ok": false, "code": "api_not_ready", "message": "..."}
  ]
}
```

重点状态包括：

- `active_model_pack_unavailable`：激活记录存在，但外部目录或 manifest 已失效；
- `voice_not_configured`、`reference_audio_missing`、`reference_audio_unavailable`：声线或参考音频问题；
- `runtime_not_installed`、`runtime_not_running`、`api_not_ready`：runtime 生命周期问题；
- `pretrained_missing`、`runtime_config_invalid`：模型包无法生成 GPT-SoVITS custom config。

## `ttsStudio.runtimeSmoke`

调用参数：

```json
{
  "text": "你好，这是 Fantareal TTS Studio 的测试声音。",
  "timeoutSeconds": 30,
  "autoLaunch": true
}
```

当 readiness 仅因 runtime 未启动或 API 尚未就绪而失败时，`autoLaunch` 会复用现有 `runtimeLaunch` 链路，并在有限超时内轮询 `/openapi.json`。就绪后发送最小 `/tts` 请求，成功时返回受 preview 限制保护的音频 descriptor；失败时返回 `status`、readiness 快照和 runtime 日志尾部。

## Web UI

RUNTIME 面板现在提供：

1. `Readiness check`：只检查，不启动；
2. `Sound smoke test`：必要时启动并等待 API，然后把返回音频放入试听播放器；
3. 状态文本会区分 runtime、模型包、声线、参考音频和 API 合成错误。

真实旧 WebUI 模型目录仍然只读使用。Phase 12 dry-run 已确认该目录可扫描为 77 个文件、约 5.52 GB、3 个声线候选，并能生成 runtime custom config；由于当前没有本地安装 runtime 且声线尚未显式绑定参考音频，readiness 会如实返回未就绪，不把 dry-run 当作实际发声成功。
