# AgentRelay

AgentRelay 是一个 macOS 优先的 Codex 语音通知适配器。它把 Codex 的回合结束
通知交给本地队列，默认使用 macOS `say` 播放，并允许 Codex 通过一个本地
命令选择值得听到的简短进展播报。

## 当前状态

- **MVP 可用**：Codex `notify`、异步播放、已有通知转发、去重和队列替换。
- **默认 provider**：`system_say`，无需网络和额外 Python 依赖，调试不产生云成本。
- **默认语言**：简体中文（`zh-CN`），也可切换到美式英语（`en-US`）。
- **可选 provider**：火山引擎豆包语音合成模型 2.0，按需安装并启用。
- **播报范围**：当前回合结束通知 + Codex 主动调用的中间进展命令；还不是 token 级响应流。
- **本地服务**：按需启动 Unix socket daemon，空闲后退出；启动失败时回退到一次性
  `system_say` worker。
- **MCP 控制面**：提供 `speak_update`、`stop` 和 `preview` 三个 stdio 工具，共用
  同一队列、冷却、去重和播放策略。
- **Push-to-talk 输入**：使用 macOS 原生 Speech framework 录音转写，默认复制到剪贴板，
  可选粘贴到当前 Codex 输入框；不会自动提交。

## 快速开始

### 前置条件

- macOS，且系统存在 `/usr/bin/say` 和 `/usr/bin/afplay`；
- Python 3.11 或更高版本；
- 已安装并配置 Codex CLI，且存在 `~/.codex/config.toml`。

### 安装并测试

在本仓库目录执行：

```sh
python3 agentrelay.py install
python3 agentrelay.py doctor
python3 agentrelay.py speak "AgentRelay 已准备就绪。"
```

然后重启 Codex。`install` 会：

1. 备份原始的 `~/.codex/config.toml`；
2. 将原有 `notify` 命令保存为转发目标，避免覆盖已有 Computer Use 通知；
3. 把当前 checkout 的 `agentrelay.py codex-notify` 写入 Codex 配置；
4. 创建运行配置 `~/.config/agentrelay/config.json`。

安装是可逆的：

```sh
python3 agentrelay.py uninstall
```

卸载会从 `~/.codex/config.toml.agentrelay-backup` 恢复配置。修改 Codex 配置后
需要重启 Codex 才会生效。

## 中间进展播报

Codex 可以在一个回合尚未结束时，单独发送一条适合语音的摘要：

```sh
python3 agentrelay.py speak-update "接口接入完成，正在验证通知链路。"
```

需要立即提醒用户处理的阻塞事项可以绕过普通冷却：

```sh
python3 agentrelay.py speak-update --priority important \
  "需要你批准权限后才能继续。"
```

支持 turn ID 的调用可精确抑制同一回合的最终 fallback：

```sh
python3 agentrelay.py speak-update --turn-id TURN_ID "阶段结果已完成。"
```

命令会立即返回 JSON，合成和播放在后台执行。运行时会拒绝空文本、过长文本、
重复内容和过于频繁的普通更新，并清理 Markdown、代码、URL、路径和疑似凭据。
返回 `skipped` 时不要立即重复发送同一条消息。

## Push-to-talk 输入

录音转写一个可编辑的 Codex 提示词：

```sh
python3 agentrelay.py push-to-talk
```

默认使用当前语言配置（中文为 `zh-CN`），并要求 macOS Speech framework 支持设备端
识别。开始录音后按 Enter 停止，转写结果会打印并复制到剪贴板；将结果粘贴到 Codex
后请手动检查和提交。

直接粘贴到当前获得焦点的 Codex 输入框：

```sh
python3 agentrelay.py push-to-talk --paste
```

`--paste` 只发送 Command-V，不会按 Enter，也不会自动执行 Codex 命令。首次使用时
macOS 会请求麦克风和 Speech Recognition 权限；`--paste` 还需要在系统设置中允许
终端控制键盘（Accessibility）。

可用参数：

- `--locale zh-CN` 或 `--locale en-US`：临时覆盖语言；
- `--max-seconds 60`：录音最长时长，按 Enter 可提前结束；
- `--allow-cloud`：设备端识别不可用时，明确允许 Apple Speech 使用云识别。

首次运行会把 Swift helper 编译到 `~/.config/agentrelay/bin/`。录音只在命令运行期间
进行，不保存音频；转写正文不会写入 AgentRelay 日志、指标或队列。

如果 macOS 拒绝权限或 helper 更新后不再弹出授权窗口，先运行诊断提示：

```sh
python3 agentrelay.py voice-reset-permissions
```

然后在 `System Settings > Privacy & Security > Microphone` 和 `Speech Recognition`
中启用你运行命令的终端应用，再运行 `push-to-talk`。由于 helper 是命令行程序，
macOS 通常把权限归属于启动它的终端，而不是 AgentRelay helper 本身。

如确实需要让 macOS 清空这两个类别的全部应用授权，可显式执行：

```sh
python3 agentrelay.py voice-reset-permissions --all
```

`--all` 会影响所有应用，之后 macOS 会重新询问相关权限。

要让 Codex 知道何时值得播报，可将
[Codex Speech Guidance](docs/CODEX_SPEECH_GUIDANCE.md) 合并到相应的
`AGENTS.md`。指导内容只影响模型选择，长度、冷却和隐私规则仍由 AgentRelay
运行时强制执行。

## 语言

AgentRelay 默认使用中文和 macOS `Tingting` 声音。切换语言会在当前声音仍为旧语言
默认值时同步选择合适的系统声音；自定义声音不会被覆盖。

```sh
python3 agentrelay.py language zh-CN
python3 agentrelay.py language en-US
```

语言保存在 `~/.config/agentrelay/config.json` 的 `language` 字段中，并随队列请求
传递。当前支持 `zh-CN` 和 `en-US`。

## Provider

### 本地 macOS `say`（默认，推荐调试阶段）

无需额外安装。查看当前配置和依赖：

```sh
python3 agentrelay.py doctor
```

切换 provider：

```sh
python3 agentrelay.py provider system_say
```

### 火山引擎豆包语音合成模型 2.0（可选）

云 provider 需要 Python WebSocket 依赖和 API key。先安装依赖：

```sh
python3 -m pip install -r requirements-volcengine.txt
cp .env.example .env
```

编辑 `.env`，只保留本机使用的 key：

```dotenv
VOLCENGINE_TTS_API_KEY=your_api_key
```

测试并启用：

```sh
python3 agentrelay.py volcengine-test "你好，AgentRelay。"
python3 agentrelay.py volcengine-enable
```

调试或节约成本时切回本地 provider：

```sh
python3 agentrelay.py provider system_say
```

API key 只从环境变量或项目 `.env` 读取，不写入 AgentRelay 配置，也不会输出到
日志。`.env`、音频和运行时目录已加入 `.gitignore`，不要把真实 key 提交到 Git。

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `python3 agentrelay.py install` | 接入 Codex `notify`，并保留已有通知转发 |
| `python3 agentrelay.py uninstall` | 恢复安装前的 Codex 配置 |
| `python3 agentrelay.py doctor` | 检查系统语音、provider 和转发配置 |
| `python3 agentrelay.py status` | 显示队列长度、当前播放和最近结果 |
| `python3 agentrelay.py stop` | 停止当前播放并清空等待队列 |
| `python3 agentrelay.py speak TEXT` | 手动测试一次语音 |
| `python3 agentrelay.py speak-update TEXT` | 排队一条模型选择的简短进展 |
| `python3 agentrelay.py push-to-talk` | 录音并将转写复制到剪贴板 |
| `python3 agentrelay.py provider NAME` | 在 `system_say` 和 `volcengine` 间切换 |
| `python3 agentrelay.py language NAME` | 在 `zh-CN` 和 `en-US` 间切换语言 |
| `python3 agentrelay.py volcengine-test TEXT` | 不改变 provider，测试火山引擎请求 |
| `python3 agentrelay.py volcengine-enable` | 启用火山引擎并保留本地 fallback |

## 配置

运行配置位于 `~/.config/agentrelay/config.json`。安装后通常只需修改：

- `provider`：`system_say` 或 `volcengine`；
- `language`：`zh-CN`（默认）或 `en-US`；
- `voice`、`rate`：macOS `say` 的声音和语速；
- `daemon_enabled`、`daemon_idle_seconds`：启用按需服务及设置空闲退出时间；
- `spoken_max_chars`、`normal_cooldown_seconds`：中间播报的长度和频率；
- `final_notify_mode`：`off`、`if_not_spoken` 或 `always`；
- `forward_notify`：已有通知命令，安装时自动保存。
- push-to-talk：默认设备端识别；`--allow-cloud` 才允许云识别；`--paste` 需要辅助功能权限。

完整示例见 [examples/config.example.json](examples/config.example.json)。
运行数据默认写入 `~/.config/agentrelay/`；测试时可设置 `AGENTRELAY_HOME` 使用隔离目录。

## 本地服务协议

CLI 默认按需启动 `~/.config/agentrelay/agentrelay.sock`，socket 权限为 `0600`。协议为
每行一个 JSON 请求，支持 `health`、`speak` 和 `stop`。`speak` 可以消费现有持久
队列，也可携带 provider-neutral request：`id`、`text`、`language`、`voice`、
`speed`、`interrupt`、`source` 和 `turn_id`。云调用计数、字符数、失败数和累计
延迟写入 `metrics.json`，不记录正文。

## MCP 控制面

安装命令会在 `~/.codex/config.toml` 注册 `agentrelay` MCP server。也可以直接
运行以下命令供支持 stdio MCP 的客户端启动：

```sh
python3 agentrelay.py mcp-server
```

工具行为：

- `speak_update(text, priority, replace, turn_id)`：异步提交模型选择的简短进展，
  复用长度、隐私、冷却、去重和重要级别策略；
- `stop()`：停止当前播放并清空待播队列；
- `preview(text, voice)`：明确由用户发起的试听，不修改持久化声音配置。

工具失败只返回 MCP 工具错误，不阻塞 Codex；`notify` 仍保留为可靠的 final
fallback。

## 故障排查

1. 先运行 `python3 agentrelay.py doctor`，确认 `speech: available`。
2. 用 `python3 agentrelay.py speak "测试"` 区分系统播放问题和 Codex 集成问题。
3. 检查 `~/.config/agentrelay/events.jsonl`，结构化日志不会记录正文或 API key。
4. 确认 Codex 已重启，且 `~/.codex/config.toml` 的 `notify` 指向当前 checkout。
5. 云 provider 失败时会自动 fallback 到 `system_say`；调试期间可直接运行
   `python3 agentrelay.py provider system_say`。

## 开发

基础 provider 没有第三方运行时依赖。执行测试和编译检查：

```sh
python3 -m unittest discover -v
python3 -m py_compile agentrelay.py volcengine_protocol.py volcengine_tts.py
```

项目按“薄 Codex 适配层 + 本地队列 + 可替换 provider”组织。当前实现刻意不
安装大型神经模型；本地 MeloTTS/Qwen3-TTS 评估和 MCP 方案都在后续 roadmap 中。

## 文档与路线图

- [产品需求](docs/PRD.md)
- [架构分析](docs/ARCHITECTURE.md)
- [模型主动播报设计](docs/MODEL_DIRECTED_SPEECH.md)
- [Push-to-talk 设计](docs/PUSH_TO_TALK.md)
- [本地 TTS 模型计划](docs/LOCAL_TTS_MODEL.md)
- [Codex 播报指导](docs/CODEX_SPEECH_GUIDANCE.md)
- [Roadmap 与头脑风暴](docs/ROADMAP.md)

## 许可

本仓库尚未选定开源许可证。发布到公共仓库前，需要明确项目代码、第三方
依赖和模型权重的许可证边界。
