---
name: mavi-deploy
description: 在 Ubuntu 上安装、迁移、升级、验证或回滚 mavi-vllm，覆盖 venv 和 GGUF 资产、systemd、sudoers、监听端口、桌面自启、SQLite 与密钥备份。
---

# Ubuntu 部署与回滚

## 部署前核对

- [scripts/install-system.sh](../../scripts/install-system.sh) 假定系统用户为 `mavi`，应用位于 `/home/mavi/mavi-vllm-platform`，Python 与 vLLM 位于 `/home/mavi/inference/venv-vllm/bin`。启动器中的 CUDA Python 路径基于 Python 3.12。迁移时统一核对脚本、unit、启动器及测试中的硬编码路径。
- 目标需有可用的 NVIDIA 驱动、适配的 vLLM/PyTorch/GGUF 插件与插件源码、`nvidia-smi` 和现有 `/etc/systemd/system/qwen3.8-vllm.service`。模型目录由 `backend/core.py` 指定，默认 `/home/mavi/inference/models/liuxing`，包含 GGUF 主权重、视觉投影、MTP 权重和 `hf-config/`。运行时依赖、插件和模型资产需预先准备。已部署主机如有 `data/production-qa/runtime-manifest.json`，用它核对版本和插件文件哈希。
- 记录当前启用状态与期望/已应用配置。备份应用、unit 与 drop-in、sudoers 和插件。SQLite WAL 下使用 SQLite backup API 备份 `data/platform.sqlite3`，并配套保存 `data/secret.key`。保留 `data/` 权限与服务用户归属。
- 安装前核对端口 `20204/80/8000`、可信网络访问范围、sudoers 和服务命令。安装脚本会改写系统文件、打插件补丁并中断现有模型请求，按任务授权的目标执行。

## 安装或更新

1. 核对目标代码和资产后，在指定 Ubuntu 主机的应用目录执行 `sudo bash scripts/install-system.sh`。脚本先调用 [scripts/patch-loader-memory.py](../../scripts/patch-loader-memory.py) 修改匹配版本的插件，再配置桌面自启、备份原推理 unit、写 sudoers 与三个 unit，最后重载 systemd 并启停服务。插件源码不匹配时应检查已发生的修改和备份，逐项核对版本后再继续。
2. 管理服务 `mavi-vllm-platform.service` 以 `main:app` 监听 `20204`，通过受限 sudoers 控制推理。网关 `mavi-vllm-gateway.service` 以 `gateway:app` 监听 `80`，由 unit 的 `CAP_NET_BIND_SERVICE` 授权。推理 `qwen3.8-vllm.service` 由启动器监听 `127.0.0.1:8000`，失败后等待 15 秒，10 分钟最多启动 3 次。三个服务按现有 unit 以 `mavi` 运行。
3. 普通更新按影响面选择服务。管理代码与网关分别有独立服务，修改 `core.py` 时检查三个进程的消费者。并发、上下文和启动器参数在引擎重载时应用。更新 unit 后需要 `systemctl daemon-reload`，并核对部署前期望的开机状态。桌面入口 [scripts/open-platform.sh](../../scripts/open-platform.sh) 使用回环 URL。

## 验证与恢复

- `systemctl status mavi-vllm-platform mavi-vllm-gateway qwen3.8-vllm --no-pager` 和对应 `journalctl -u` 检查状态与启动日志。用 `ss -lnt` 核对监听，结合防火墙确认仅从预期网段访问管理与网关。安装脚本在 UFW 已启用时只添加管理端口规则，API 端口依赖目标主机的既有配置。
- 分别检查 `http://127.0.0.1:20204/api/health`、`http://127.0.0.1/health` 与 `http://127.0.0.1:8000/health`。管理及网关存活后，仍需等推理就绪，再用测试 Key 核对 `/v1/models` 和一次最小推理。首次初始化的管理凭据在 `data/bootstrap-credentials.json`，通过受控渠道获取。现场验收见 [mavi-test](../mavi-test/SKILL.md)。
- 恢复时按变更范围还原代码、插件、unit/drop-in 和 sudoers。`backups/qwen3.8-vllm.original.service` 是首次安装前的引擎 unit，更新回滚用对应版本备份。涉及数据库时停用写入进程，再成对还原数据库和密钥及权限。校验 sudoers、重载 systemd、按记录恢复服务与启用状态，并复跑健康和 API 检查。
- `apply-address-update.sh`、`apply-stability.sh`、`fix-control.sh` 和 `set-default-admin.sh` 是针对现场状态的专项脚本。先读其变更内容再选择。安装参数及历史现场说明见 [OPERATIONS.txt](../../OPERATIONS.txt)。
