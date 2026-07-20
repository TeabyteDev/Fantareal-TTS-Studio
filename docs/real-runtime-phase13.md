# Phase 13：真实外部 runtime 闭环

## 外部 runtime 语义

TTS Studio 不要求所有 GPT-SoVITS API 都由插件启动。只要满足以下条件，就可以连接旧 WebUI 或其他宿主已经启动的 loopback API：

- active model pack 仍然有效；
- 当前声线的 GPT/SoVITS 权重和 reference audio 可解析；
- `/openapi.json` 存在 `/tts`；
- TTS 请求返回非空音频。

插件自己的 runtime pointer、进程 PID 和安装状态仍会显示在诊断中，但它们是管理能力，不再是外部 API 合成的硬前置条件。

## 本机真实验证

2026 年 7 月 19 日使用旧 WebUI 本地目录执行了只读闭环：

- 模型目录：旧 `mods/tts studio`；
- runtime：目录内 `runtime/GPT-SoVITS`；
- Python：本地 Python 3.11 + 已存在的 GPT-SoVITS 环境；
- API：`127.0.0.1:9880`；
- 声线：`御姐绝色`；
- 权重：旧目录中的 GPT `.ckpt` 与 SoVITS `.pth`；
- 参考音频：旧目录 `reference_audios/Chinese/emotions` 下的本地 WAV。

新 service 完成了模型包 manifest 校验、custom config 生成、权重切换、`/tts` 请求和 preview 音频返回。最终结果：

```text
ok: true
status: ready
audioBytes: 277804
header: 52 49 46 46 ... 57 41 56 45  (RIFF/WAVE)
readiness: external GPT-SoVITS API is ready
```

输出 WAV 只写入临时目录，旧模型目录没有复制、移动或写回。

## 端口冲突

当插件启动的 runtime 日志包含 `address already in use`、`WinError 10048` 或 `10048` 时，readiness 返回：

```text
status: api_port_conflict
message: GPT-SoVITS API port is already in use
```

这与普通 API 尚未启动分开显示，方便用户先停止占用 9880 的旧进程，或改用已经运行的外部 API。

## 环境提示

本机 CUDA 环境中的 `onnxruntime-gpu` 报告 CUDA 13 DLL 缺失，但本次 GPT-SoVITS 仍返回了有效音频。这类 warning 会保留在 runtime 日志中，不应被 UI 误判为合成失败；后续安装器阶段再统一处理 CUDA/PyTorch/ONNX Runtime 版本匹配。
