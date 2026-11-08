# Ubuntu 真实服务验收

先检查目标服务、管理认证方式、测试凭据、当前 `config`/`applied`、模型就绪状态和可用 GPU。多数 Python 验收脚本假定应用位于 `/home/mavi/mavi-vllm-platform`，通过 Ubuntu 回环地址连接，且读取引导凭据或使用原验收账号。检查脚本实际前置条件和报告目录，用部署 venv 执行。对重载、故障注入、压力测试或其他现场写入，使用任务授权的目标和维护窗口。

| 目标 | 入口 | 副作用或验收要求 |
| --- | --- | --- |
| 完整平台和模型链路 | `tests/integration.py` | 需指定图像夹具。建临时 Key，切换别名与策略，停止/启动模型，检查管理服务独立在线。脚本以双并发推荐档启动，验收后核对原配置。写 `data/integration-validation.json` |
| 网关边界和断线 | `tests/gateway_edge_cases.py`、`tests/stream_cancel_validation.py` | 建临时 Key，产生预期的 `400/408/413` 等失败和取消统计。检查断线后资源释放与后续请求 |
| 地址变化及外部客户端 | `tests/address_validation.py`、`tests/Test-RemoteApi.ps1` | 前者用回环服务验证不同 Host/Origin，并创建测试 Key。后者在 Windows LAN 客户端用 `-ConnectionPath`、`-ImagePath`、`-OutputPath`，检查无 Key 的 401、模型名、文本和图片推理 |
| 自定义并发和 8 路容量 | `tests/custom_settings_validation.py`、`tests/profile8_validation.py` | 改配置并重载模型，使用并发图文请求验证应用状态、显存和容量。失败后也核对是否恢复原配置 |
| 压测与档位矩阵 | `tests/production_stability.py --soak-seconds 1200`，矩阵另加 `--matrix` | 产生过载请求、修改运行配置并可能多次重载。默认或矩阵的稳定性阶段使用 `2×12288`，运行后检查目标配置是否需要恢复。`--resume` 会读取已有报告状态 |
| 故障恢复与开机启动 | `tests/crash_recovery.py`、`tests/boot_validation.py` | 前者主动 `SIGKILL` 推理进程。后者用 `--before` 记录重启前基线，实际重启后去掉参数验证开机状态，预期三个服务均启用且应用 `2×12288` |

`data/production-qa/` 存专项结果。`tests/summarize_stability.py` 汇总数据库采样和指标，`tests/runtime_manifest.py` 记录 Python/驱动/包版本、插件源码与补丁。完成后核对临时 Key、配置、服务就绪和新日志。报告只包含所需的脱敏证据，数据库中有意产生的失败和取消应按测试 Key 与时间区分。
