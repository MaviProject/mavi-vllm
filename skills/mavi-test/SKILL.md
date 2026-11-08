---
name: mavi-test
description: 为 mavi-vllm 编写或选择测试、运行本地回归、定位失败，区分 mock 后端、jsdom 前端、GGUF 加载器与 Ubuntu 真实模型验收。
---

# 测试与回归验证

命令从仓库根目录执行。优先用与改动匹配的隔离测试，再按任务范围选择真实服务验收。

| 变更 | 命令 | 覆盖范围与依赖 |
| --- | --- | --- |
| 管理 API、SQLite、网关、启动参数或监控逻辑 | 构建后运行 `ctest --test-dir build --output-on-failure` | Python 3 `unittest` 启动 C++ 服务、回环 HTTP 假引擎和 systemctl 替身。数据位于临时目录。旧版 Fernet 互操作测试另需 Python cryptography |
| DOM、配置交互、地址适配、调试界面 | `npm ci` 后运行 `npm run test:ui` | `package-lock.json` 锁定前端开发依赖。[tests/frontend.cjs](../../tests/frontend.cjs) 用 jsdom 与模拟 fetch。可设置 `MAVI_TEST_PAGE_URL` 测试不同管理入口地址 |
| GGUF 加载器补丁 | `/home/mavi/inference/venv-vllm/bin/python tests/test_loader_memory.py` | 需要 PyTorch、vLLM 和已打补丁的插件。检查量化数据逐字节一致、embedding 切分与 padding、单个及多个融合分片的物化 |

## 回归用例定位

- [tests/test_platform.py](../../tests/test_platform.py) 的 `PlatformTests` 覆盖鉴权、配置和回滚、容量校准、网关策略、SSE、准入及断线清理。可用 `python3 tests/test_platform.py PlatformTests.test_admission_and_disconnect_before_headers` 缩小范围。默认二进制为 `build/mavi-backend`，也可用 `MAVI_BACKEND_BINARY` 指定。
- 按行为验证边界。配置检查合法值、非法值及落库后的再读取。网关检查拒绝响应及上游请求体。流式清理同时检查上游关闭、准入计数和数据库结果。前端检查实际 DOM 及发出的请求。
- 后端测试为子进程设置临时 `MAVI_PLATFORM_ROOT`、回环地址及临时端口，避免接触现场服务。真实部署脚本位于同一个 `tests/` 目录，应按文件选择测试。

## 需要真实服务时

仅当任务要求真实服务、GPU 或运维验证时，读 [Ubuntu 真实验收](references/live-validation.md) 并按场景选脚本。记录实际命令、目标环境、结果和缺失的依赖。分别报告 mock、jsdom、真实浏览器与真实推理的覆盖情况。
