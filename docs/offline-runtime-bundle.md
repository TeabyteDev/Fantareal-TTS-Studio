# 完整离线 TTS 包

## 目标目录

插件接受下列兼容布局之一：

```text
tts studio/
  runtime/
    GPT-SoVITS/
      api_v2.py
      requirements.txt
      extra-req.txt
      GPT_SoVITS/pretrained_models/
    voices/
      gpt/*.ckpt
      sovits/*.{pth,pt}
      audio/*.{wav,mp3,flac,ogg,m4a,aac}
```

也兼容以 `GPT-SoVITS/` 为直接子目录，或直接选择 GPT-SoVITS 根目录。目录必须包含可识别的模型资源。

## 使用流程

1. 选择完整包目录并扫描。
2. 点击“使用这个包”，持久化只读目录引用。
3. 点击“配置本机环境”。
4. 插件自动选择算力环境并安装 Python 依赖。
5. 选择声线权重、参考音频和参考文本。
6. 启动 API，执行 readiness 和声音测试。

## 存储边界

- GPT-SoVITS、pretrained_models、声线和参考音频保持在原目录。
- Python/PyTorch 环境写入扩展 `assets/runtime/environments/`。
- 当前 runtime 指针写入扩展 `assets/runtime/current.json`。
- 安装失败或取消不会修改完整包，也不会覆盖当前可用环境。

## 自动算力

`auto` 与旧 WebUI 保持兼容：存在 `nvidia-smi` 时解析为 `cu126`，否则解析为 `cpu`。`cu128` 只作为高级手动选项。

## 在线备用

“在线下载 GPT-SoVITS”是显式备用能力。只有用户从高级设置主动触发时，插件才会下载固定 commit 的源码；主流程不会静默联网重下 runtime。

## Windows 依赖策略

插件会从 GPT-SoVITS requirements 中移除 `opencc` 的源码编译指令，并单独使用 `--only-binary=opencc` 安装 Windows wheel。用户不需要为了配置 TTS 环境额外安装 MSVC C++ Build Tools。每条 pip 命令的 stdout/stderr 会追加到 `runtime-install.log`，安装失败时页面可以显示具体 package 错误。

## NLTK 语言资源

GPT-SoVITS 的英文 G2P 会用到 `cmudict` 和词性标注数据。首次配置受管 Python 环境时，插件会额外下载约 9.5 MiB 的 `nltk_data.zip`，校验固定 SHA-256 后安全解压到该环境的 `nltk_data/`。下载包会缓存在扩展 `cache/runtime-downloads/`，后续重试可复用；缺失或校验失败时安装会明确失败，不会显示为已完成。

这里不会下载或复制 GPT-SoVITS 模型、声线权重和参考音频。若希望完全断网安装，后续发布的资源包还需要同时提供 Python wheels 与这份经过校验的 NLTK 缓存；当前流程只保证本地模型包不会被重新下载。
