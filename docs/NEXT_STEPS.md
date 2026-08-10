# AgentRelay Next Steps

Date: 2026-08-07

## Objective

Move AgentRelay from a feature-complete local MVP to a dependable daily tool.
The immediate focus is proving the selected Volcengine path under real use,
then fixing reliability and observability gaps before adding new speech modes.

## Stage 1: Validate the Active Volcengine Path

1. Run one explicit `volcengine-test` with a short, non-sensitive Chinese
   sentence and confirm that cloud audio plays without using the fallback.
2. Run one MCP `preview` and one `speak_update`, then inspect `status`,
   `events.jsonl`, and `metrics.json` to verify provider attribution, latency,
   character accounting, and queue completion.
3. Simulate a cloud failure by temporarily removing the API key in an isolated
   `AGENTRELAY_HOME`; confirm fallback to `system_say` and a useful diagnostic.
4. Record baseline measurements for first audible latency, total latency,
   fallback rate, and Chinese technical-term pronunciation.

Exit criteria:

- Volcengine synthesis and playback succeed for five consecutive requests.
- Metrics identify Volcengine use and do not contain text or credentials.
- A provider failure falls back once, remains visible in status/logs, and does
  not block the calling Codex turn.

## Stage 2: Close Reliability Defects

Address the findings in `docs/REVIEW.md` in this order:

1. Make CLI and MCP results distinguish accepted, queued, started, skipped,
   fallback, and failed states; return failure when no consumer can start.
2. Prevent stale playback state from signaling a reused, unrelated PID by
   recording and verifying process ownership before cancellation.
3. Define and test request-level speed precedence for `system_say`.
4. Resolve default voice from the requested language when voice is omitted.
5. Validate configuration types at load time and use safe effective defaults.
6. Make installation and uninstall updates atomic and protect user-modified
   Codex configuration from accidental overwrite.

Each fix should include focused unit tests. Process lifecycle changes should
also include subprocess-level integration tests with fake players/providers.

Exit criteria:

- All high- and medium-severity review findings are closed.
- Startup failure, stale playback state, malformed config, cancellation,
  provider fallback, speed, and language selection have regression tests.
- The full test and compile checks pass on macOS.

## Stage 3: Long-Task Operational Validation

1. Exercise 20 or more updates across normal, important, duplicate, replace,
   and expired-message cases.
2. Verify replacement stops active audio promptly and does not leave player or
   worker processes behind.
3. Restart and idle-expire the daemon repeatedly while requests are arriving;
   verify no duplicate or stranded queue items.
4. Capture real Codex turn IDs and confirm that MCP updates suppress only the
   matching final notification.
5. Tune cooldown, queue age, and final-notify policy from observed behavior,
   keeping `if_not_spoken` until the evidence supports disabling the fallback.

Exit criteria:

- A representative long Codex task completes without duplicate, lost, or
  stale speech.
- Important updates interrupt within an agreed latency target.
- Final-notification suppression is tied to the correct turn.

## Stage 4: Improve Operator UX

1. Add `status --json` with the last request outcome, effective provider,
   fallback state, queue age, and non-sensitive failure reason.
2. Add `config get/set`, `voice list`, and a provider-neutral `test` command.
3. Make `doctor` distinguish required failures from optional provider checks
   and clearly state when text will be sent to a cloud service.
4. Use sentence-aware truncation and indicate when spoken content was
   shortened.

Exit criteria:

- Common setup and failure diagnosis require no direct JSON editing.
- Codex and scripts can inspect state without parsing human output.

## Stage 5: Refactor Without Behavior Changes

After the reliability contract is covered by tests, split `agentrelay.py` into
configuration, text policy, queue/state, runtime, providers/players,
integrations, and CLI modules. Preserve CLI commands, MCP schemas, runtime file
formats, and direct-script compatibility during the move.

Do not start streaming, ASR/VAD, voice cloning, or default local neural-model
work until Stages 1-3 pass. Local TTS evaluation can continue later using the
existing benchmark gates in `docs/LOCAL_TTS_MODEL.md`.

## Verification Commands

```sh
python3 -m unittest discover -v
python3 -m py_compile agentrelay.py volcengine_protocol.py volcengine_tts.py
python3 agentrelay.py doctor
python3 agentrelay.py status
```

The first action requiring explicit user intent is the live Volcengine smoke
test because it sends text to a cloud service and may incur usage cost.
