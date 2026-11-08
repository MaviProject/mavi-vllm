---
name: mavi-deploy
description: 在 Ubuntu 上安装、迁移、升级、验证或回滚 mavi-vllm，覆盖 venv 和 GGUF 资产、systemd、sudoers、监听端口、桌面自启、SQLite 与密钥备份。
---

# Ubuntu 部署与回滚

## 部署前核对

- [scripts/install-system.sh](../../scripts/install-system.sh) 假定系统用户为 `mavi`，应用位于 `/home/mavi/mavi-vllm-platform`，推理 venv 位于 `/home/mavi/inference/venv-vllm/bin`。C++ 构建依赖见 [README.md](../../README.md)。启动器中的 CUDA 路径基于 Python 3.12，迁移时统一核对脚本、unit、启动器及测试中的路径。
- 目标需有 NVIDIA 驱动、适配的 vLLM/PyTorch/GGUF 插件与插件源码、`nvidia-smi` 和现有 `/etc/systemd/system/qwen3.8-vllm.service`。[backend/runtime.cpp](../../backend/runtime.cpp) 解析模型目录，默认 `/home/mavi/inference/models/liuxing`，包含 GGUF 主权重、视觉投影、MTP 权重和 `hf-config/`。现场可用 `data/production-qa/runtime-manifest.json` 核对版本和文件哈希。
- 记录当前启用状态与期望/已应用配置。备份应用、unit 与 drop-in、sudoers 和插件。SQLite WAL 下使用 SQLite backup API 备份 `data/platform.sqlite3`，并配套保存 `data/secret.key`。保留 `data/` 权限与服务用户归属。
- 安装前核对端口 `20204/80/8000`、可信网络访问范围、sudoers 和服务命令。安装脚本会改写系统文件、打插件补丁并中断现有模型请求，按任务授权的目标执行。

## 安装或更新

1. 核对目标代码和资产后，在指定 Ubuntu 主机的应用目录执行 `sudo bash scripts/install-system.sh`。脚本先构建 Release 二进制并检查结果，再调用 [scripts/patch-loader-memory.py](../../scripts/patch-loader-memory.py) 修改匹配版本的插件、安装二进制及桌面入口、更新 sudoers 和三个 unit，最后重载 systemd 并启停服务。已有部署时保留推理的启用和启停状态。
2. 三个 unit 共用 `/usr/local/bin/mavi-vllm-backend`。管理模式 `admin` 监听 `20204` 并使用受限 sudoers。网关模式 `gateway` 通过 `CAP_NET_BIND_SERVICE` 监听 `80`。推理模式 `engine` 将进程替换为监听 `127.0.0.1:8000` 的 vLLM。推理失败后等待 15 秒，10 分钟最多启动 3 次。
3. 普通更新按影响面重启对应服务。修改共享存储时检查三个模式。并发、上下文和启动器参数在引擎重载时应用。更新 unit 后需要 `systemctl daemon-reload`。桌面入口 [scripts/open-platform.sh](../../scripts/open-platform.sh) 使用回环 URL。

## 验证与恢复

- `systemctl status mavi-vllm-platform mavi-vllm-gateway qwen3.8-vllm --no-pager` 和对应 `journalctl -u` 检查状态与启动日志。用 `ss -lnt` 核对监听，结合防火墙确认仅从预期网段访问管理与网关。安装脚本在 UFW 已启用时只添加管理端口规则，API 端口依赖目标主机的既有配置。
- 分别检查 `http://127.0.0.1:20204/api/health`、`http://127.0.0.1/health` 与 `http://127.0.0.1:8000/health`。管理及网关存活后，仍需等推理就绪，再用测试 Key 核对 `/v1/models` 和一次最小推理。首次初始化的管理凭据在 `data/bootstrap-credentials.json`，通过受控渠道获取。现场验收见 [mavi-test](../mavi-test/SKILL.md)。
- 恢复时按变更范围还原应用、二进制、插件、unit/drop-in 和 sudoers。`backups/qwen3.8-vllm.original.service` 是首次安装前的引擎 unit，更新回滚用对应版本备份。Python 版回滚需要恢复该版本源码和 unit，SQLite 表、密码摘要与 Fernet 格式可继续使用。需要恢复数据时停用写入进程，再成对还原数据库和密钥及权限。校验 sudoers、重载 systemd、恢复服务与启用状态，并复跑健康和 API 检查。
- `apply-address-update.sh`、`apply-stability.sh`、`fix-control.sh` 和 `set-default-admin.sh` 是针对现场状态的专项脚本。先读其变更内容再选择。安装参数及历史现场说明见 [OPERATIONS.txt](../../OPERATIONS.txt)。
