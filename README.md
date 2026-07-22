# Fantareal TTS Studio

Fantareal TTS Studio 是面向 Fantareal Extension Platform v1 的 GPT-SoVITS TTS Provider。插件提供独立配置页和 JSON-RPC service；Fantareal 主程序负责聊天朗读、播放控制、消息切换与错误反馈。

## 当前能力

- 使用 `fantareal-extension.json` 声明 Page、Command 和 TTS Provider。
- 将设置、历史、合成缓存、模型和 runtime 分别写入宿主分配的 namespaced storage。
- 检查本地 GPT-SoVITS API，枚举声线、切换权重并生成语音。
- 从宿主 session workspace 导入 `.ckpt`、`.pth`、`.pt` 和参考音频。
- 识别用户提供的完整 TTS 包，直接复用其中的 GPT-SoVITS、预训练模型、声线和参考音频。
- 只为当前电脑创建、修复 Python/PyTorch 环境，并启动或停止 GPT-SoVITS API。
- 在配置页生成声线试听；试听复用 `tts.synthesize` 链路。

## 仓库边界

本仓库只包含插件源码、Web UI、runtime installer、测试和许可证声明，不包含：

- Python 虚拟环境；
- GPT-SoVITS runtime 实体；
- GPT、SoVITS 或其他预训练模型；
- 用户声线、参考音频、合成历史和缓存；
- Fantareal 主程序或构建产物。

这些内容由 Fantareal 在用户本机的扩展存储中管理，不会写回插件仓库。

## 安装与使用

Fantareal 支持从本地目录或 GitHub 仓库安装扩展。仓库发布后可使用：

```text
https://github.com/cOkieeman/Fantareal-TTS-Studio
```

安装插件后，主流程是在 TTS Studio 页面选择一个完整本地 TTS 包。插件不会复制其中的大模型和 GPT-SoVITS，只会在 Fantareal 的扩展存储中创建当前电脑专用的 Python 环境。也可以连接已经运行的 GPT-SoVITS loopback API。

## Runtime 与模型

- 完整包应包含 `runtime/GPT-SoVITS`、预训练模型和 `runtime/voices`。
- 默认使用自动算力模式：检测到 `nvidia-smi` 时选择 CUDA 12.6，否则选择 CPU；CUDA 12.8 和强制 CPU/CUDA 位于高级设置。
- 环境先在 staging 中安装并验证，再原子更新当前 runtime 指针；完整包目录保持只读。
- 备用在线修复仍固定下载 `20250606v2pro` 对应 commit `d7c2210da8c013e81a94bfc7b811a477c99fd506`，不会自动追随上游 `main`。
- 安装失败或取消不会覆盖仍可用的旧 runtime。
- 插件更新不会覆盖用户声线、模型、历史或合成缓存。

### 模型下载

模型文件不会进入 Git 仓库。后续会在这里补充经过整理的网盘下载地址、文件大小、SHA-256、来源和许可证说明。

在正式下载包发布前，用户可以选择自己已有的完整 GPT-SoVITS/TTS Studio 目录，也可以分别导入 GPT/SoVITS 权重和参考音频。

## 本地开发

需要 Python 3.11 和 [uv](https://docs.astral.sh/uv/)：

```powershell
uv sync --locked --dev
uv run ruff check src tests
uv run pytest
```

自动测试只使用本地 fixture，不会下载 GPT-SoVITS、PyTorch、模型或其他大型依赖。

## 致谢

Fantareal TTS Studio 的本地语音能力建立在 [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) 的研究与工程成果之上。

衷心感谢 RVC-Boss 团队以及所有 GPT-SoVITS 贡献者，为少样本语音合成、跨语言推理和本地部署所付出的长期努力。本项目是面向 Fantareal 的独立集成项目，并非 GPT-SoVITS 官方项目，也不代表上游团队对本项目的认可或背书。

上游版权与许可证信息见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 和 [third_party/licenses/GPT-SoVITS-MIT.txt](third_party/licenses/GPT-SoVITS-MIT.txt)。

## License

Fantareal TTS Studio 自身代码采用 [MIT License](LICENSE)。通过安装器取得的 GPT-SoVITS、Python packages、模型和其他第三方内容继续受各自许可证约束。
