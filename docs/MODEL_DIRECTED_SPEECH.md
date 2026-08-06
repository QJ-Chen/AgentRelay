# Model-Directed Speech Plan

## Status

The local `speak-update` command is the shipped MVP. The MCP tool described
below is the next integration option, not a prerequisite for using AutoTTS.

## Decision

Add an asynchronous MCP tool for model-selected speech updates. Keep Codex
`notify` only as a configurable final-turn fallback, not as the primary source
of spoken content.

```text
Codex commentary/final
        |
        | decides an update is worth hearing
        v
MCP speak_update(text, priority)
        |
        v
AutoTTS queue -> provider -> playback

Codex turn complete -> notify -> optional final fallback
```

This solves the three requirements together:

1. MCP can be called during a turn, so intermediate progress can be spoken.
2. The spoken text is a separate short summary, not the complete visible reply.
3. Codex decides whether an event is important enough to call the tool.

Watching every `commentary` message in session JSONL would satisfy only the
first requirement. It would read verbose operational text, increase API cost,
and require unstable Codex session parsing. It should not be the main design.

## Tool Contract

Expose one narrow tool initially:

```json
{
  "name": "speak_update",
  "arguments": {
    "text": "已完成接口接入，正在验证真实语音请求。",
    "priority": "normal",
    "replace": true
  }
}
```

Fields:

- `text`: required spoken text, plain text, normally Chinese;
- `priority`: `normal` or `important`;
- `replace`: whether this update supersedes queued normal updates, default true.

The call must enqueue and return immediately. Synthesis and playback must never
block Codex's next action.

Later controls such as `stop`, `replay`, and voice preview can be separate MCP
tools. They are not needed for the first increment.

## Speech Policy

Codex should call `speak_update` only when the information helps a user who is
not watching the screen:

- after settling on a meaningful plan for a substantial task;
- when a major milestone completes;
- when the approach materially changes;
- when blocked or when user action/approval is required;
- when a long-running operation starts or finishes;
- when the requested work is complete and the outcome is useful to hear.

Do not speak:

- routine file reads, searches, edits, or individual test commands;
- repeated status with no new user-relevant information;
- code, paths, URLs, stack traces, raw command output, or secrets;
- detailed findings that are better read on screen;
- every commentary message.

Spoken text requirements:

- one or two short sentences;
- target 20-60 Chinese characters;
- hard maximum 100 Chinese characters or 200 Unicode characters;
- lead with outcome, blocker, or next meaningful action;
- omit greetings, formatting, and phrases such as “我来为你”；
- summarize rather than quote the visible response;
- use Chinese by default, retaining only necessary English technical terms.

Examples:

Good:

```text
火山引擎语音已接入，真实请求和后台通知链路均验证成功。
```

Too verbose:

```text
我已经修改了三个 Python 文件，新增了 WebSocket 客户端、协议枚举、帧解析，
同时运行了十二项单元测试，下面将继续检查配置文件……
```

Not worth speaking:

```text
正在读取 README。
```

## Runtime Guardrails

Model judgment is necessary but not sufficient. Enforce deterministic limits
inside AutoTTS:

- reject empty text;
- hard truncate or reject text over the configured maximum;
- remove Markdown, URLs, code, paths, and credential-shaped values;
- deduplicate identical speech within 30 seconds;
- apply a 10-15 second cooldown to normal updates;
- allow `important` updates to bypass cooldown;
- keep at most one pending normal update in replace mode;
- discard updates older than 30 seconds;
- track daily cloud characters and fall back to `system_say` at a configured
  budget ceiling;
- interrupt stale playback when a newer update has `replace=true`.

Suggested configuration:

```json
{
  "speech_mode": "model_directed",
  "spoken_max_chars": 200,
  "normal_cooldown_seconds": 12,
  "max_queue_age_seconds": 30,
  "daily_cloud_char_budget": 10000,
  "final_notify_mode": "if_not_spoken",
  "fallback_provider": "system_say"
}
```

`final_notify_mode` options:

- `off`: final responses are spoken only when Codex calls the MCP tool;
- `if_not_spoken`: notify speaks a short fallback only if no model-directed
  update was made for that turn;
- `always`: preserve the current full final-message behavior.

Recommend `if_not_spoken` during rollout, then `off` after reliability is
proven. The fallback cannot independently generate a concise summary, so it
should enforce a short deterministic character cap rather than speak the full
answer.

## Codex Guidance

Install a short instruction in the project/plugin rather than asking Codex to
emit `<tts>` tags:

```text
Use the AutoTTS speak_update tool for user-relevant progress, blockers, major
milestones, and completion. Speak only information useful when the user is not
watching the screen. Write a separate concise Chinese summary of one or two
sentences; do not read normal commentary verbatim. Skip routine operations and
avoid code, paths, URLs, raw output, and secrets.
```

The instruction influences selection and phrasing. Runtime guardrails still
enforce cost, length, safety, and queue behavior if the model over-calls.

## Implementation Steps

1. Refactor the current queue into a reusable `SpeakRequest` path shared by
   notify, CLI, and MCP.
2. Add a local MCP stdio server with `speak_update` and register it in Codex
   config without changing the existing notify relay.
3. Add `source`, `priority`, `replace`, timestamp, and optional turn ID to queue
   records.
4. Implement length, cooldown, age, deduplication, and cloud-budget policies.
5. Add playback interruption for replace-mode updates.
6. Change final notify behavior to `if_not_spoken` with a conservative short
   fallback cap.
7. Add the Codex speech policy through the eventual plugin/project instruction.
8. Test with a long coding turn containing routine work, one milestone, one
   blocker, and completion; verify that only useful updates are spoken.

## Acceptance Criteria

- Codex can produce speech before a turn ends.
- Normal visible commentary remains detailed and is not read verbatim.
- Typical spoken updates are below 60 Chinese characters.
- Routine operations produce no speech.
- A meaningful blocker and final outcome are spoken.
- MCP calls return quickly and playback occurs asynchronously.
- Repeated updates do not accumulate stale audio.
- Cloud character usage is recorded without logging response text or API keys.
- Tool/server failure does not interrupt Codex and final fallback remains
  available.
