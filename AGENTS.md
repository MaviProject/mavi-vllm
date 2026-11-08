# mavi-vllm 项目指引

mavi-vllm 是运行在 Ubuntu 单机上的 vLLM 管理平台。后台用 C++20、Boost.Asio/Beast、SQLite WAL 和 OpenSSL，构建入口为 `CMakeLists.txt`。前端使用原生 HTML/CSS/JavaScript。systemd 分别运行管理服务、网关及 NVIDIA GPU 上的 vLLM/GGUF 推理。Python venv 用于 vLLM 和运维，Node.js 用于 jsdom 测试。

## 任务路由

先按要修改的行为或要交付的结果选一个主技能，只读其 `SKILL.md` 和当前任务涉及的源码。跨边界修改时，再补读对应的技能。支持技能发现的工具可按 `name` 和 `description` 注册或索引仓库内的 `skills/`。也可以按下表直接打开文件，无需依赖特定工具的自动发现机制。

| 主任务 | 读取 | 源码入口 |
| --- | --- | --- |
| 管理页面、登录、配置表单、API Key 管理、在线调试、日志与 CSV | [mavi-console](skills/mavi-console/SKILL.md) | `static/`、`backend/admin.cpp` |
| OpenAI 兼容 API、模型别名、限流、思考策略、SSE 与请求统计 | [mavi-gateway](skills/mavi-gateway/SKILL.md) | `backend/gateway.cpp`、`backend/http.cpp` |
| 模型启动与启停、并发/上下文、KV 容量、GPU 指标、GGUF/MTP | [mavi-inference](skills/mavi-inference/SKILL.md) | `backend/runtime.cpp`、`backend/store.cpp` |
| 编写或选择测试、回归验证、定位测试失败 | [mavi-test](skills/mavi-test/SKILL.md) | `tests/`、`CMakeLists.txt`、`package.json` |
| 安装、迁移、升级、回滚、systemd/防火墙/桌面自启 | [mavi-deploy](skills/mavi-deploy/SKILL.md) | `scripts/install-system.sh`、`OPERATIONS.txt` |

- 配置表单改到容量校验或重载时，补读推理技能。在线调试改到 SSE、模型别名或请求策略时，补读网关技能。
- 需要选择验证命令时，读测试技能中的对应条目。只有任务需要真实服务验收时才展开其验收参考。

## 服务与数据边界

| 默认监听 | 入口 | 职责 |
| --- | --- | --- |
| `0.0.0.0:20204` | `mavi-backend admin` | 管理网页、`/api/*`、系统服务控制和监控 |
| `0.0.0.0:80` | `mavi-backend gateway` | 外部 `/v1/*`、API Key 验证、请求策略和用量 |
| `127.0.0.1:8000` | `mavi-backend engine` → `vllm serve` | 真实模型推理及 `/health`、`/metrics` |

管理在线调试走 `浏览器 → /api/playground → 网关 → vLLM`。`backend/store.cpp` 维护共享配置、鉴权数据、容量建议与统计。`MAVI_PLATFORM_ROOT` 决定应用根目录，默认使用进程工作目录。`data/` 保存 SQLite 库、加密密钥和验收产物。

## 修改与验证

- 以源码、测试和部署脚本判断当前行为。`README.md` 提供产品概览，`OPERATIONS.txt` 记录原部署参数与验收快照。现场配置以目标主机为准。
- 保持管理请求同源、网关鉴权和原生推理回环监听。涉及协议或数据变更时检查现有调用方及已有 SQLite 数据。
- 后台先用 CMake 构建，再运行 `ctest --test-dir build --output-on-failure`。前端使用 `npm ci` 后的 `npm run test:ui`。真实部署、故障注入、重载和压测需以任务授权的目标及维护范围为准。
- `build/`、`data/`、`backups/` 和 `node_modules/` 是本地产物。保护密码、完整 API Key、`secret.key`、提示词和图片，避免将其写入日志、测试夹具或提交。
