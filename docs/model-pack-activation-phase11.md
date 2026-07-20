# Phase 11：模型包激活与真实合成

Phase 10 只生成扫描结果。Phase 11 增加持久化激活和真实 GPT-SoVITS 使用链路。

## 激活

`ttsStudio.activateModelPack` 接收当前 session 的 `directoryToken` 和 Phase 10 manifest：

```json
{
  "directoryToken": "12345678-1234-1234-1234-123456789abc",
  "manifest": { "schemaVersion": 1, "kind": "fantareal.tts-model-pack" }
}
```

service 会重新校验 manifest 和文件大小，然后在 extension `data/active-model-pack.json` 中保存经过用户授权的模型根目录引用。不会复制模型目录；token 只用于激活当次选择，后续 session 使用持久化的激活状态。

## 模型引用

声线设置中的 GPT、SoVITS 和参考音频可以使用：

```text
model-pack:voices/gpt/hero.ckpt
model-pack:voices/sovits/hero.pth
model-pack:voices/audio/reference.wav
```

service 会将其解析到已激活模型包，并再次检查 manifest 声明、路径包含关系、symlink 和后缀。停用模型包后，这些引用不会被当作任意绝对路径执行，会在合成时报告模型包不可用。

## Runtime

启动 GPT-SoVITS 时，如果当前存在激活模型包，service 会按当前 active voice 生成 `data/runtime-model-pack-config.json`，并通过 `api_v2.py -c` 传入 custom 配置。配置包含 GPT/SoVITS 权重、`chinese-roberta-wwm-ext-large`、`chinese-hubert-base`、device 和 version。

runtime 进程仍然只允许 loopback API；切换或停用模型包会停止由插件启动的 runtime，避免旧模型继续驻留。

## Web UI 操作顺序

1. 选择本地模型目录。
2. 扫描完成后点击“激活模型包”。
3. 对权重候选点击“应用权重候选”。
4. 在声线资产中选择参考音频并保存设置。
5. 安装/启动 runtime，点击“生成试听”。
