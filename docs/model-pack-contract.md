# TTS 模型包契约（Phase 10）

Phase 10 将本地目录和未来网盘下载统一为只读扫描结果。两种来源最终都应产出同一个 `fantareal.tts-model-pack` manifest，再由后续阶段负责 staging 和激活。

## 目录角色

扫描器识别以下相对目录角色：

- `pretrained`：路径包含 `pretrained_models` 的 GPT-SoVITS 基础模型；
- `gpt`：路径包含 `voices/gpt` 且后缀为 `.ckpt`；
- `sovits`：路径包含 `voices/sovits` 且后缀为 `.pth` 或 `.pt`；
- `audio`：路径包含 `voices/audio` 且后缀为 `.wav`、`.mp3`、`.flac`、`.ogg`、`.m4a` 或 `.aac`。

## Manifest 最小结构

```json
{
  "schemaVersion": 1,
  "kind": "fantareal.tts-model-pack",
  "packId": "local-model-pack",
  "version": "local",
  "source": "local",
  "files": [
    {
      "path": "runtime/voices/gpt/hero.ckpt",
      "role": "gpt",
      "sizeBytes": 123
    }
  ],
  "summary": {
    "fileCount": 1,
    "bytes": 123,
    "roles": {"gpt": 1}
  },
  "voices": []
}
```

`sha256` 是可选字段。真实大模型目录默认只扫描路径和大小，只有显式开启 hash 校验时才读取文件内容。

## 声线绑定规则

GPT/SoVITS 权重和参考音频不保证同名。扫描器只生成权重候选和独立 audio library；正式声线的 `referenceAudio` 必须由用户选择或后续 manifest 显式声明，不能依据文件名强行绑定。

## 当前 RPC

`ttsStudio.inspectModelPack` 支持两种来源：extension workspace-relative 目录，或宿主通过 `files.pickDirectory` 返回的一次性 `directoryToken`。

```json
{
  "path": "model-pack",
  "packId": "local-model-pack",
  "version": "local",
  "computeSha256": false
}
```

目录授权调用示例：

```json
{
  "directoryToken": "12345678-1234-1234-1234-123456789abc",
  "packId": "legacy-webui-models",
  "version": "local",
  "computeSha256": false
}
```

`directoryToken` 对应宿主写入 session workspace 的 `input-directory-grants/<token>.json`。grant 只包含宿主确认过的绝对目录引用和 `readOnly: true` 标记；service 不接受页面直接传入的任意绝对路径，也不会复制、移动或删除该目录。

Phase 11 通过 `ttsStudio.activateModelPack` 将本次用户选择持久化为 active model pack；后续 session 不复用临时 token，而是重新校验已激活目录和 manifest。声线设置可以使用 `model-pack:<relative-path>` 引用包内权重或参考音频。

当前 RPC 是只读扫描，不会复制、移动、删除或启动 runtime。目录选择器、模型激活和网盘下载分别属于后续 Phase 10-B、11 和 13。
