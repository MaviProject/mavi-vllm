---
name: mavi-gateway
description: 修改 mavi-vllm 的 OpenAI 兼容网关，包括 Bearer/API Key 认证、模型别名、请求校验与准入、思考策略、SSE、断线清理和用量统计。
---

# OpenAI 兼容网关

## 请求路径

1. [backend/gateway.py](../../backend/gateway.py) 的 `authenticated` 读取 Bearer 或 `X-API-Key`，并通过 [backend/core.py](../../backend/core.py) 的 `lookup_key` 查询启用状态。`/v1/models` 返回公开别名及已应用上下文长度。
2. `inference` 允许 POST 到 `chat/completions`、`completions` 和 `responses`。单进程 `app.state.admitted` 限制在途与等待请求，阈值为 `min(16, 已应用并发 + 6)`。队满时返回 `429` 和 `Retry-After: 3`。
3. `forward` 限制请求体为 25 MiB、读取期限为 30 秒，超限返回 `413` 或 `408`。`core.read_body` 按实际读取量兜住分块上传。JSON 必须是对象，公开 `model` 必须与当前别名一致。
4. `enforce_policy` 校验候选数、输出 token 和模板参数，将 `model` 换为 `core.INTERNAL_MODEL`。httpx 使用独立内部 Key 调用 `core.BACKEND_URL`。`rename_model` 将 JSON/SSE 及嵌套 Responses 中的模型名换回公开别名。

## 策略与流生命周期

- 单请求只允许一个候选，拒绝 beam search 及原始补全的批量 prompt。显式输出参数上限为 `min(4096, 已应用上下文)`。请求自带模板或续写控制参数会被拒绝。
- `thinking_enabled=false` 时，覆盖模板思考参数，并分别为 Chat Completions 和 Responses 设置关闭推理的字段。此时原始 `/v1/completions` 返回 `400`。别名和思考策略读当前 `config`，容量边界优先读 `applied_config`。
- 非 Responses 的 SSE 在上游请求中启用 `include_usage`，供网关统计。客户端未要求用量时，仅过滤追加的纯用量帧。处理 `[DONE]`、`response.completed`、错误/截断、断线和首个有效增量的 TTFT。
- `forward.finalize` 与 [backend/streaming.py](../../backend/streaming.py) 的 `ManagedStreamingResponse` 覆盖迭代中及响应头发送时的断线。保留幂等收尾、计数归还、单次记录和取消屏蔽下的上游 `aclose()`。管理端 `main.playground` 也使用该响应类。
- `core.record_request` 保存状态、结果、耗时、token 和 Key 元数据。保持记录中无请求正文、图像和完整密钥。涉及并发模式变化时，检查进程内准入计数与 Uvicorn 运行方式是否匹配。

## 开发验证

优先用 [tests/test_platform.py](../../tests/test_platform.py) 中的 `GatewayTests` 和 HTTP mock 覆盖请求结构、策略、别名、过载、SSE 及提前断线。变更流式格式时核对 `static/app.js` 的调试端消费逻辑。需要在 Ubuntu 做真实协议验证时，按 [mavi-test](../mavi-test/SKILL.md) 选择用例。
