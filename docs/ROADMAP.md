# AgentRelay Roadmap

更新时间：2026-08-06

这份文档把当前 MVP、后续头脑风暴和可验证的交付顺序放在一起。路线图不是
承诺所有想法都会实现；每个阶段都有退出条件，先用真实使用数据决定是否继续。

## 北极星

让用户在不盯着 Codex 屏幕时，也能及时听到少量真正有用的信息：重要进展、
阻塞、需要用户操作的事项和最终结果。语音应该是低打扰的第二通道，而不是把
终端内容逐字朗读出来。

## 当前基线

已完成的 MVP 能力：

- Codex `notify` 适配器，回合结束后异步入队；
- 保留并异步转发既有 Computer Use 通知；
- macOS `say` 默认 provider，火山引擎 Seed TTS 2.0 可选；
- 模型主动选择的 `speak-update` 本地命令，可在回合中播报；
- 普通更新冷却、重要更新旁路、去重、队列替换、过期丢弃和文本清理；
- 最终 notify 在近期已有主动播报时自动抑制，避免重复；
- 单元测试、provider 协议测试和 `doctor` 诊断。

当前明确不保证：

- Codex token 生成过程中的实时响应语音；
- 播放过程中强制中断已经运行的 `say` 或 `afplay`；
- MCP 工具集成；
- 神经 TTS 模型的默认安装；
- ASR、VAD、语音对话或跨设备播放。

## 优先级总览

| 优先级 | 主题 | 目标 | 建议阶段 |
| --- | --- | --- | --- |
| P0 | 可靠性和可观测性 | 失败不影响 Codex，能解释为什么播或不播 | Phase 1 |
| P0 | 播放中断 | 新的重要消息能停止过时音频 | Phase 1 |
| P1 | 持久 daemon | 避免每条通知重复创建 worker 和加载模型 | Phase 2 |
| P1 | 本地神经 TTS | 在 M3 16 GB 上取得可感知的音质提升 | Phase 3 |
| P1 | 精确 turn 关联 | 正确抑制同一回合的 final fallback | Phase 2 |
| P2 | MCP 控制面 | 提供显式播报、停止、试听等工具 | Phase 4 |
| P2 | 响应流接入 | 在 Codex 生成过程中按句合成 | Phase 5 |
| P3 | ASR/VAD/语音交互 | 从单向提醒扩展到免手操作 | Explore |
| P3 | 插件和跨平台 | 降低安装成本，支持 Linux/Windows | Explore |

## 分阶段计划

### Phase 1：MVP 稳定化

状态：已于 2026-08-06 实现；退出条件仍需长期运行和真实音频中断验证。

目标：让当前本地命令在长时间使用和异常情况下仍然可预测。

交付：

1. 将队列记录、状态文件和日志格式固定为带版本号的 schema；
2. 为 enqueue、worker、provider 和 notify 增加结构化的无敏感信息日志；
3. 为 `doctor` 增加运行目录权限、播放命令、云依赖和配置合法性检查；
4. 把当前播放器封装成可取消进程，`replace=true` 时中断正在播放的旧消息；
5. 增加安装、重复安装、卸载和已有 notify 配置的集成测试；
6. 添加一个安全的 `agentrelay status` 或等价诊断入口，显示队列长度和最近结果。

实现说明：运行时记录、状态和日志使用 `schema_version=1`；旧版无版本号状态仍可
读取。`events.jsonl` 只记录元数据，播放器 PID 支持 replacement 中断，`doctor`
检查配置、目录、播放命令和云依赖，`status` 显示队列、播放状态和最近结果。

退出条件：

- 20 条连续更新不会丢失锁或留下僵尸 worker；
- 新的重要更新到达后，旧音频在可接受时间内停止；
- provider 故障、权限故障和坏配置都不会阻塞 Codex 回合；
- 用户可以仅靠 `doctor` 输出定位常见安装问题。

### Phase 2：Provider 边界和常驻服务

状态：已于 2026-08-06 实现；daemon 重启压力测试和真实 Codex turn ID payload
仍需在长期使用中验证。

目标：把当前脚本从“通知脚本”演进为可替换的本地语音运行时。

交付：

1. 抽象 `TTSProvider`、`AudioPlayer` 和 provider-neutral 的 `SpeakRequest`；
2. 增加本地 Unix domain socket 或 loopback 服务，支持 health、speak、stop；
3. daemon 启动失败时自动回退到 `system_say`，空闲后退出；
4. 用 turn ID、内容 hash 和 source 关联主动更新与 final notify，而不是只看时间；
5. 将云字符统计、失败次数和延迟写入本地指标文件，不记录正文和 key；
6. 保持直接运行 `agentrelay.py` 的兼容路径，迁移配置时不破坏现有安装。

实现说明：`SpeakRequest`、`TTSProvider` 和 `AudioPlayer` 已成为运行时契约；按需
Unix socket daemon 支持 `health`、`speak` 和 `stop`，socket 权限为 `0600`，
默认空闲 60 秒退出。daemon 失败时使用 `system_say` 一次性 worker。主动更新和
final notify 优先按 turn ID、其次按内容 hash 关联；旧 payload 继续使用时间窗口。
云指标写入不含正文的 `metrics.json`，旧配置和 `_worker` 路径保持兼容。

退出条件：

- 适配器在 daemon 未启动时仍能快速返回；
- daemon 重启后队列不会重复播报或无限堆积；
- system_say、Volcengine 和 fake provider 通过同一份契约测试；
- 一次完整 Codex 回合可精确判断是否需要 final fallback。

### Phase 3：本地神经 TTS 评估与接入

目标：在不牺牲响应速度和机器可用性的前提下，提供比 `say` 更自然的中文声音。

顺序：

1. 先固定 25-40 条中英文混合技术语料和人工发音检查表；
2. 记录 `say` 基线，再单独评估 MeloTTS；
3. 只有通过基准门槛后，才将 MeloTTS 暴露为可选 lightweight profile；
4. MeloTTS 失败时再评估 Qwen3-TTS 0.6B，不默认下载大模型；
5. 模型、权重和 Python 依赖放在仓库外的独立 `uv` 环境；
6. 在 daemon 中常驻模型，按句分段合成，允许取消和 fallback。

建议门槛：

- M3 上典型 1-2 句的 warm 首音不超过 800 ms；
- real-time factor 小于 0.5；
- lightweight profile 峰值内存低于 2.5 GB；
- 中英文技术文本没有关键读法错误；
- 盲听样本中至少 80% 明显优于 `say`；
- 20 次连续请求无需重启模型进程。

详细候选比较见 [本地 TTS 模型计划](LOCAL_TTS_MODEL.md)。

### Phase 4：MCP 控制面

目标：让 Codex 能通过结构化工具主动播报，同时不把 MCP 变成唯一触发源。

首批工具建议只有：

- `speak_update(text, priority, replace)`：异步入队并立即返回；
- `stop()`：停止当前和等待中的普通播报；
- `preview(text, voice)`：明确由用户发起的试听。

原则：

- MCP server 与 CLI 共用同一队列和策略；
- 工具失败只返回工具错误，不让 Codex 任务失败；
- 重要消息可绕过冷却，但仍受长度、隐私和频率限制；
- `notify` 保留为可靠的 host-side final fallback；
- 经过真实长任务验证后，再考虑把 `final_notify_mode` 从
  `if_not_spoken` 调整为 `off`。

设计背景见 [模型主动播报设计](MODEL_DIRECTED_SPEECH.md)。

### Phase 5：响应流和分段播放

目标：在 Codex 仍生成内容时，尽早播放已稳定的句子。

前提是 Codex 提供可靠的结构化 delta 或 app-server 事件；不解析终端重绘，
不读取私有 session 文件作为长期协议。

交付：

- 中文和英文句界检测、短句缓冲和顺序保证；
- 新 token 到达时只提交已经稳定的句子；
- 新回合、取消和错误会终止未完成合成；
- 背压和最大延迟防止音频追不上用户交互；
- 没有结构化事件时自动回退到当前 turn-end 模式。

退出条件是“首句更早可听”而不是“每个 token 都发音”。后者会导致重复、
修订内容被朗读和队列爆炸。

## 头脑风暴：值得探索但暂不承诺

### 1. 语音内容如何选择

- **模型主动选择**：最符合“有必要才通知”，但可能漏播或过度调用；用冷却和
  重要级别兜底。
- **规则选择**：检测阻塞、权限请求和长任务，可靠但缺少上下文；适合作为
  MCP/模型失效时的 fallback。
- **混合选择**：模型给候选摘要，运行时做风险、长度、预算和重复检查；当前
  路线最值得继续。

### 2. 语音输出的产品形态

- 默认只说结果和下一步，不说过程细节；
- 重要消息可以使用不同声音或提示音，但不要用过多音效制造打扰；
- 提供“安静模式”和工作时段，避免会议或深夜自动播放；
- 支持按项目、事件类型和 provider 分别配置；
- 未来可以输出字幕/通知历史，但不保存完整敏感正文。

### 3. 本地模型策略

- lightweight：MeloTTS 或维护良好的 ONNX 变体，优先低延迟；
- quality：Qwen3-TTS 0.6B，仅在内存和 MPS 基准通过后启用；
- cloud：Volcengine，作为质量或冷启动 fallback，但有成本和网络依赖；
- system：macOS `say`，始终保留为故障恢复路径。

不要在同一阶段同时引入模型下载器、声音克隆、模型量化和跨平台打包；每项
都需要单独的性能、许可证和恢复验证。

### 4. 语音输入的边界

ASR、VAD 和语音命令会把项目从“提醒层”变成“交互层”，新增麦克风权限、
隐私提示、误触发和命令确认问题。建议在单向 TTS 的延迟、可靠性和静音策略
稳定后，再用独立实验验证，而不是直接并入核心队列。

## 衡量指标

每个阶段都记录以下指标，避免凭感觉替换 provider：

- 从事件/命令到首个可听音频的延迟；
- 更新接受率、冷却跳过率、重复率和过期丢弃率；
- provider 成功率、fallback 率、播放中断时间；
- 每天云字符数和估算成本；
- 用户主动关闭语音或重播的次数；
- 中英文技术词汇的发音错误清单。

## 发布和分支策略

- `main` 保持可用，默认 provider 永远是 `system_say`；
- 每个 provider 或协议改动单独提交，提交信息使用清晰的动词开头；
- 大模型实验只提交配置、脚本、基准报告和文档，不提交权重或运行环境；
- 破坏性配置迁移先提供读取旧配置的兼容期；
- 每次发布前运行单元测试、编译检查、`doctor` 和一次本地试听。

## 下一步执行顺序

1. 用真实长任务验证 Phase 1/2 的连续更新、音频中断和 daemon 重启退出条件。
2. 捕获真实 Codex notify turn ID，验证精确 final fallback 抑制和旧 payload 兼容。
3. 固定语料，生成 `say` 基准记录，明确 MeloTTS 的实际首音和内存数据。
4. 按 Phase 3 门槛评估本地神经 TTS，不改变默认 `system_say`。
5. 再决定 MCP server 是直接实现，还是先以 plugin/command 形式打包。

## 需要在真实使用中回答的问题

- 一次工作回合用户愿意听几条播报，12 秒冷却是否合适？
- 完成通知应该默认播报，还是模型主动调用后完全关闭 fallback？
- 用户更在意首音延迟、声音自然度，还是中英文术语准确度？
- 云 provider 的质量提升是否值得其成本和联网边界？
- 语音历史、静音时段和多项目配置是否属于核心产品，而非便利功能？
