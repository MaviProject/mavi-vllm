---
name: mavi-inference
description: 修改 mavi-vllm 推理运行逻辑，包括 systemctl 启停、保存与应用配置、并发/上下文容量、KV 校准、GPU 指标、vLLM 启动参数和 GGUF/MTP 加载。
---

# 推理运行与容量

## 从保存到就绪

1. [backend/main.py](../../backend/main.py) 的 `Settings` 和 `settings` 严格校验整数并发 `1–8`、整数上下文及 `core.capacity_profile(n).max_context`。`core.config()` 返回期望配置。是否需要重载根据已应用的并发、上下文判断。模型在 `running` 或 `loading` 且 `apply_restart=true` 时才自动提交重启。
2. `main.control` 通过进程内 `CONTROL_LOCK` 串行执行 `sudo -n systemctl … --no-block`。启动或重启前重置 systemd 失败计数。开机加载开关对应 `enable` 或 `disable`，启动和停止是独立动作。
3. [backend/engine_launcher.py](../../backend/engine_launcher.py) 读期望配置，写 `applied_config`（并发、上下文、图像像素上限），再 `os.execv` 启动 vLLM。`applied_config` 表示交给启动器的参数。就绪以引擎 `/health` 为准。
4. `main.monitor` 结合 `/health` 与 systemd 状态产出 `running`、`loading`、`stopping`、`error` 或 `stopped`。采样来自 `nvidia-smi`、psutil 和 vLLM `/metrics`，更新 `CACHE`、SQLite `samples` 及容量实测。指标请求超时可以发生在引擎仍健康时。

## 容量与运行约束

- [backend/core.py](../../backend/core.py) 的 `context_envelope` 在 `1/2/4/8` 档的总 token 预算间插值，再除以并发数。档位上限为 `24576/12288/6144/1024`，属于这台部署机器的验证边界。
- `capacity_profile` 用 KV 实测或估算收紧上限，按 `85% / 并发` 计算上限，按 `75% / 并发` 计算建议值并向下取整到 1024 token，最后受档位和最小上下文约束。`calibration` 保存当前测量，`capacity_profiles` 保留达到档位边界的测量，避免小上下文实测覆盖大上下文参考。改算法时一起核对 `/api/recommendations`、`/api/settings` 及前端上限提示。
- `image_max_pixels` 按上下文缩放上限，`1K` 时为 `262144`，全局最多 `1048576`。启动器限制每请求一张图片、零视频，并配置 GGUF 视觉投影、MTP、`0.90` 显存预算、1024 token 分块预填充和 eager 模式。
- 公开模型名和思考开关由网关即时应用。内部名称和模型目录由 `core.py` 定义。启动器只将 vLLM 暴露在 `127.0.0.1:8000`，通过共享的内部 Key 与网关对接。

## 验证范围

- 配置、健康状态和启动参数先用 [tests/test_platform.py](../../tests/test_platform.py) 的 `PlatformTests` 验证，测试用 mock 捕获启动参数和服务操作。
- 冷启动峰值、GGUF 字节一致性或实测容量涉及 [scripts/patch-loader-memory.py](../../scripts/patch-loader-memory.py) 与 [tests/test_loader_memory.py](../../tests/test_loader_memory.py)。补丁针对固定 GGUF 插件源码，融合分片在 CPU 暂存并最终物化到目标设备。
- 真实 GPU 验证按 [mavi-test](../mavi-test/SKILL.md) 选择。改 unit、运行环境或模型资产布局时，补读 [mavi-deploy](../mavi-deploy/SKILL.md)。
