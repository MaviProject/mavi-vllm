---
name: mavi-console
description: 开发或改造 mavi-vllm 管理网页（admin UI）与管理 API，覆盖会话、CSRF、配置表单、API Key 管理、在线调试、概览、日志和 CSV 导出。
---

# 管理网页与管理 API

## 按改动定位

| 行为 | 入口和对应实现 |
| --- | --- |
| 页面结构、样式和事件 | [static/index.html](../../static/index.html)、[static/styles.css](../../static/styles.css)、[static/app.js](../../static/app.js)。`S` 保存前端状态，`enter`/`refresh`/`showTab` 管理登录后初始化、轮询与导航 |
| 登录、会话、改密 | [backend/admin.cpp](../../backend/admin.cpp) 的 `login`、`session`、`new_session`、`password`，以及 [backend/crypto.cpp](../../backend/crypto.cpp) 的 `password_hash` |
| 推理配置 | 前端 `loadSettings`、`profileFor`、`renderContext`、`settings-form`，后端 `Admin::settings` 和 `Store::recommendations` |
| API Key | `renderKeys` 和密钥弹窗对应 `/api/keys`、`/api/keys/{kid}/reveal` 等路由。持久化和查找由 `Store` 负责 |
| 在线调试 | `chat-image`、`chat-form` 和 `Admin::playground`。浏览器处理图文历史和 SSE，管理服务选最早启用的 Key 转发至本机网关 |
| 概览、审计和导出 | `renderOverview`、`loadLogs`，后端 `Store::usage`、`Runtime::logs` 和 `Admin::export_csv` |

## 保持的行为

- UI 由 `mavi-backend admin` 提供，静态资源、管理请求和下载使用同源路径。`app.js` 的外部 API Base URL 基于当前访问主机，使用 HTTP 80 的 `/v1`。切换管理主机地址后可能需要重新登录。
- 登录、配置等普通管理 JSON 请求上限为 16 KiB。在线调试请求保留 25 MiB 上限。
- `/api/login` 校验提供的 `Origin`，并按来源 IP 对失败尝试限流。管理会话使用 `mavi_session` 的 HttpOnly/SameSite cookie，数据库保存 token 摘要。受保护的写请求同时校验 `X-CSRF-Token`，改密使旧会话失效并返回新 CSRF 值。
- Key 列表仅显示尾号等元数据。完整值在创建或经认证的 reveal 时返回。数据库保存摘要和 Fernet 密文，启停和撤销在后续网关认证时即时生效。
- 配置表单从 `/api/recommendations` 读取 `max_context`，切并发自动填推荐值，允许手填范围内的任意整数 token。判断重载时用 `/api/overview.applied` 比较并发和上下文，缺省才回退到 `config`。
- 调试请求最多保留一张图片，换图时保留历史文字。默认输出预算为 `min(1024, floor(已应用上下文 / 4))`。内容和动态标签保持现有文本节点或转义边界。`/api/playground` 以固定缓冲区转发流，客户端断线时关闭网关连接。

## 开发验证

变更 DOM 时同步核对 `index.html` 的选择器和 `frontend.cjs` 的 fetch 夹具。变更管理 API 时同步检查浏览器调用及 `tests/test_platform.py`。容量、重载和指标的实现读 [mavi-inference](../mavi-inference/SKILL.md)，请求策略或 SSE 协议读 [mavi-gateway](../mavi-gateway/SKILL.md)。测试命令见 [mavi-test](../mavi-test/SKILL.md)。
