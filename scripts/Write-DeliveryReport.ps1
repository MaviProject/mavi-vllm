param([Parameter(Mandatory)][string]$Directory)
$ErrorActionPreference = 'Stop'
function Read-QA([string]$Name) { Get-Content -LiteralPath (Join-Path $Directory $Name) -Raw | ConvertFrom-Json }
$qaSummary = Read-QA 'stability-summary.json'
$qaProfile = Read-QA 'profile8-1k-validation.json'
$qaCancel = Read-QA 'stream-cancel-validation.json'
$qaCrash = Read-QA 'crash-recovery.json'
$qaBoot = Read-QA 'boot-validation.json'
$qaIntegration = Read-QA 'integration-validation.json'
$qaLan = Read-QA 'lan-api-validation.json'
$qaRuntime = Read-QA 'runtime-manifest.json'
foreach ($qaItem in @($qaSummary,$qaProfile,$qaCancel,$qaCrash,$qaBoot,$qaLan)) { if ($qaItem.status -ne 'passed') { throw '存在未通过的验收，不能生成通过报告。' } }
if ($qaIntegration.final_state -ne 'running') { throw '最终集成验收未完成。' }
$qaElapsed = $qaSummary.soak.elapsed_seconds
$qaRequests = $qaSummary.soak.requests
$qaTokens = $qaSummary.soak.output_tokens
$qaRate = $qaSummary.soak.aggregate_output_tokens_per_second
$qaMedian = [math]::Round($qaSummary.soak.median_latency_seconds,3)
$qaP95 = [math]::Round($qaSummary.soak.p95_latency_seconds,3)
$qaGpuPeak = [math]::Round($qaSummary.soak_resources.gpu_peak_used_mb,1)
$qaGpuFree = [math]::Round($qaSummary.soak_resources.gpu_min_free_mb,1)
$qaTemperature = $qaSummary.soak_resources.gpu_max_temperature_c
$qaRamFree = [math]::Round($qaSummary.soak_resources.ram_min_available_mb,1)
$qaHealthSamples = $qaSummary.soak_health_checks.samples
$qaHealthFailures = $qaSummary.soak_health_checks.failures
$qaVersionLines = ($qaRuntime.packages | Where-Object { $_.name -in @('vllm','torch','transformers','vllm-gguf-plugin') } | ForEach-Object { $_.name + ' ' + $_.version }) -join '；'
$qaReport = @"
mavi_vllm管理平台 — 交付测试报告
生成时间：$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz'))

结论
本机指定配置已完成以下服务端、推理和自动化验收。实际浏览器操作仍被客户端站点权限策略拒绝，不能将网页人工验收记为通过。结论适用于可信局域网和已测负载范围，不等于任意负载、无限运行或完整生产安全审计。

部署
历史验收 IP：192.168.31.132；实际访问管理页：http://<Ubuntu实际IP>:20204/；API：http://<Ubuntu实际IP>/v1。
默认：2 并发、每路 12,288 token，单图最多约 100 万像素，视觉与 MTP 开启，思考默认强制关闭。
运行库：$qaVersionLines。
测试插件 commit：e454affe0ca23c9dc8754ec3033270cb30622ed5，并应用交付包内的兼容与内存补丁。

持续负载
最终连续运行 $qaElapsed 秒，共 $qaRequests 次成功请求、$qaTokens 个输出 token，失败 0 次；稳态期间未出现 OOM、异常退出或自动重启。
双路非流式负载混合短文本、512/2048/4096 token 输入及约 1MP 图片，固定生成 128 token（忽略 EOS），用于保持持续负载。
总输出速率约 $qaRate token/s；请求中位耗时 $qaMedian 秒，P95 $qaP95 秒。数值包含预填充时间，仅对应本次测试工作负载。
NVML 每 0.5 秒采样：显存峰值 $qaGpuPeak MiB，最低剩余 $qaGpuFree MiB，GPU 最高 $qaTemperature °C；系统最低可用内存 $qaRamFree MiB，交换空间未使用。
健康检查 $qaHealthSamples 次，失败 $qaHealthFailures 次。进程 RSS、文件描述符与 PID 轨迹详见 stability-summary.json / process-samples.jsonl。

并发与上下文
历史边界测试：1×24K、4×6K、8×2K、2×12K，近上限输入和图片请求通过。
交付上限：1×24K、2×12K、4×6K、8×1K；各档建议还会参考单独保存的 KV 实测值，保留容量缓冲。
8×2K 会挤压实测约 15K 的共享 KV 空间，因此交付上限收紧到 8×1K。
最终 8×1K 另行真实加载，8 路长输入和 8 路图片均通过，随后恢复双并发 12K。1K 上下文自动将图片降至约 26 万像素，网页输出预算降为 256 token。

过载、边界及接口
24 路突发请求：8 路成功，16 路正确返回 429，并携带 Retry-After。
非法结构、非有限 JSON 数值、候选扩展、beam search、图片数量超限、超长输出与上下文均被拒绝；超大请求返回 413，慢速上传返回 408。
隐藏后端路径、公开 metrics、私密数据文件访问均被拒绝。
Windows 经局域网验证无 Key 返回 401；有效 Key 可列出模型，正确回答 42，并从测试图片读取 7319。
模型别名、多 Key 即时撤销、强制关闭思考、Chat Completions、Responses、SSE 用量、CSV 和持久统计通过集成测试。
流式专项：12 次 API 响应头阶段/首内容后断线及 2 次网页调试代理断线被记录，随后普通请求仍成功；Responses 流式输出通过。响应头发送前断线的资源清理另有隔离回归测试。
隔离后端测试 20 项、量化加载字节/分区/补齐一致性测试 4 项、前端 DOM 登录、配置、聊天、Key、安全转义和导航测试通过。

故障、释放显存与自启
主动 SIGKILL 推理 API 主进程后，管理平台和网关保持在线，未就绪推理返回 503；systemd 自动恢复用时 $($qaCrash.recovery_seconds) 秒，恢复后算术请求正确。
停止模型后显存由 $($qaIntegration.gpu_before_stop_mb) MiB 降到 $($qaIntegration.gpu_after_stop_mb) MiB，管理平台仍在线；再次启动与并发请求通过。
整机重启验证 boot_id 改变，管理、网关、推理三项服务均 enabled / active，默认配置和请求统计保留，驱动正常加载。
开机日志另有一次 Python multiprocessing 信号量清理 FileNotFoundError（Exception ignored）。本次启动未受阻、NRestarts=0、后续接口与推理测试通过，作为长期运行观察项保留；未将其描述为“启动日志零告警”。
桌面自启文件已配置为 Ubuntu 图形登录后打开平台。实际弹窗和网页点击尚未通过浏览器工具观察。

发现并修复
加载试验中出现过真实 OOM，已撤回 expandable_segments 试验并减少 GPU 临时副本，将待合并分片暂存系统内存，清理无用初始化缓存。
修复跨并发容量误估、数据库连接关闭、健康检查空闲连接误判、指标抓取失败干扰状态，以及流式开始前断线遗留队列名额。
修复前失败与未完成持续测试保留在 stability-results.json 的 previous_attempts / previous_soaks；最终成功阶段单独汇总。
20 分钟持续测试覆盖非流式图文；最后的流式清理补丁使用专项回归验证，未将其描述为 20 分钟全流式测试。

未完成与生产边界
内嵌浏览器工具明确拒绝读取该地址，原因是已保存权限或管理策略禁止 origin。没有通过其他浏览器、原始 CDP 或间接方法绕过；实际登录、点击和截图验收仍待权限问题解除。
默认 admin/admin 和 HTTP 按需求保留。对外生产接入前需变更默认凭据并配置传输保护。
vLLM 官方仍将 GGUF 支持标为实验性，可能与其他特性不兼容；当前验证不能替代更长时间的真实业务试运行。
官方说明：https://docs.vllm.ai/en/latest/features/quantization/gguf/
为推理保留 GPU；其他程序占用显存、升级运行库/驱动、改变模型或图片/并发范围后需重新验收。

证据与恢复材料
stability-results.json / stability-summary.json；profile8-1k-validation.json；gateway-edge-cases.json；stream-cancel-validation.json；crash-recovery.json；boot-validation.json；integration-validation.json；lan-api-validation.json。
原始资源轨迹位于 Ubuntu 的 /home/mavi/mavi-vllm-platform/data/production-qa/。
runtime-manifest.json、GGUF 插件兼容补丁、内存补丁、实际测试的插件源码及平台源码包已随交付目录保存。
平台用量保留验收时故意产生的 400/408/413/429 与取消记录，可按测试 Key 名称和审计时间区分业务错误。
"@
$qaTarget = Join-Path $Directory '交付测试报告.txt'
$qaCustomPath=Join-Path $Directory 'custom-settings-validation.json'
if (Test-Path -LiteralPath $qaCustomPath) {
    $qaCustom=Get-Content -LiteralPath $qaCustomPath -Raw | ConvertFrom-Json
    if ($qaCustom.status -ne 'passed' -or -not $qaCustom.restored_original) { throw '自定义设置增量验收尚未完成。' }
    $qaCustomLines=($qaCustom.profiles | ForEach-Object { '- '+$_.concurrency+' 并发 / '+$_.context_length+' token：文本 '+$_.near_context.Count+' 路、图片 '+$_.vision.Count+' 路均成功；最低显存余量 '+[math]::Round($_.gpu_min_free_mb,1)+' MiB。' }) -join "`n"
    $qaReport+="`n`n自定义设置增量验收`n现在支持手动输入 1–8 任意整数并发数和非整 K 上下文，保留 1/2/4/8 快捷档位；调整并发自动填建议值，服务端拒绝超出容量上限的输入。`n低于 4K 的上下文按预算自动降低图像分辨率，为文字和输出留空间。容量参考不会被较小上下文的测量覆盖。`n后端回归现为 23 项全部通过；前端 DOM 已验证 3/5001 自动填充、确认保存、精确回读，以及小数和超限输入拒绝。`n$qaCustomLines`n已恢复原配置，默认双并发 12K；本次增量结果不代表所有自定义组合均已逐一压测。`n证据：custom-settings-validation.json。`n"
}
$qaAddressPath=Join-Path $Directory 'address-validation.json'
if (Test-Path -LiteralPath $qaAddressPath) {
    $qaAddress=Get-Content -LiteralPath $qaAddressPath -Raw | ConvertFrom-Json
    if ($qaAddress.status -ne 'passed') { throw 'IP 地址适配验收尚未通过。' }
    $qaReport+="`n`nIP 地址变化适配增量验收`n网页静态占位、API 地址生成和新建凭据说明已去除固定旧 IP 依赖。管理/网关监听所有 IPv4 接口，内部推理与桌面自启保留回环地址。`n前端在原 IP、新网段 IP、10 网段 IP、主机名和另一回环 IP 下验证：导航与请求保持相对地址，展示、复制及示例中的 API 地址使用当前访问主机的 HTTP 80 端口。`n后端 24 项回归通过；真实 HTTP 在 127.0.0.1 与 127.0.0.2 验证登录、会话、概览、建议、日志、导出、API Key 鉴权、文本推理和网页调试图片识别。未来 Host / Origin 模拟值通过，同源登录可用，异源登录继续返回 403。`n测试没有改动生产网卡地址；监听检查确认 20204 / 80 为 0.0.0.0，UFW 当前未启用。跨网络访问仍需实际网络路由允许。`n更换 IP 后，用新 IP 打开网页；浏览器可能需要重新登录，模型、数据、配置和现有 API Key 保留。外部客户端与 SSH 的连接地址需相应更新。`n证据：address-validation.json。`n"
}
[System.IO.File]::WriteAllText($qaTarget,$qaReport,[System.Text.UTF8Encoding]::new($false))
Write-Output ('已生成报告：' + $qaTarget)
