# AgentRelay Integration Analysis

## Decision

Build the MVP as a standalone TTS daemon/CLI with a thin Codex `notify` adapter.
Package the adapter, configuration, and instructions as a Codex plugin later.
Do not use MCP as the primary trigger and do not require `<tts>` tags for the
MVP.

The key architectural separation is:

```text
Codex event adapter -> text policy/cleanup -> TTS provider -> audio player
```

Only the first component should know how Codex emits a completed response. The
remaining components should work from a provider-neutral `SpeakRequest`.

## Current Runtime Boundary

The implementation now represents each queue item as a provider-neutral
`SpeakRequest` and dispatches it through `TTSProvider` and `AudioPlayer`
contracts. `SystemSayProvider` and `VolcengineProvider` share this boundary.

CLI and Codex adapters durably enqueue first, then signal an on-demand Unix
socket daemon at `~/.config/agentrelay/agentrelay.sock`. The daemon supports
`health`, `speak`, and `stop`, owns queue consumption, and exits after an idle
timeout. If daemon startup fails, the adapter launches a one-shot worker forced
to `system_say`; setting `daemon_enabled=false` retains the configured-provider
direct worker path. This preserves old installations while keeping future
model lifetime and transport details outside the notify adapter.

## Option Comparison

| Option | How it triggers | Automatic | Streaming potential | Codex UI coverage | Main issue | MVP fit |
|---|---|---:|---:|---:|---|---:|
| `notify` command | Codex invokes a host command when a turn ends | Yes | Low for response generation; high for TTS audio | Likely broad, subject to surface support | Turn-end only and payload contract must be tested | Best |
| Codex wrapper | Launches/owns `codex` and parses its event stream | Yes | High with structured events | CLI sessions launched through wrapper only | Tight process coupling; interactive TUI parsing is fragile | Good fallback |
| Plugin | Installs a bundle of config, skills, hooks, commands, and assets | Depends on bundled mechanism | Depends on bundled mechanism | Depends on plugin support per surface | Packaging mechanism, not itself a response event | Best distribution format |
| Hook | Runs at documented lifecycle points | Yes, for supported events | Depends on event | Depends on hook support per surface | Current hooks focus on lifecycle enforcement; a response-complete payload must not be assumed | Investigate later |
| MCP server/tool | Model chooses to call `speak(text)` | No guarantee | High after invocation | Any surface exposing that MCP server | Agent may omit/repeat calls; adds latency and tool noise | Poor primary trigger |
| Terminal/output observer | Scrapes displayed terminal output | Yes | Superficially high | Terminal only | ANSI, redraws, partial text, duplicate tokens, and UI changes | Avoid |
| Session-file watcher | Watches Codex persistence files | Yes | Usually low | Local persisted sessions only | Private format, race conditions, delayed writes | Avoid |

## Why `notify` Wins the MVP

The installed Codex CLI (`0.146.0`) recognizes a top-level `notify` command,
and this machine already uses it with a `turn-ended` integration. This places
the trigger on the host side, where side effects such as audio playback belong.
It does not depend on the model remembering to call a tool or produce exact
markup.

The adapter should:

1. Accept the event argument(s) from Codex.
2. Validate the event type and extract the final assistant text.
3. Return immediately after enqueueing a request, so notification handling does
   not block Codex.
4. Deduplicate by thread/turn ID when those fields are available.
5. Ignore empty responses and optionally skip code-heavy or very long output.

Before implementation, capture one real `notify` payload from the installed
Codex build. If it does not contain final assistant text, use one of these
fallbacks:

- For non-interactive use, wrap `codex exec --json` or consume
  `--output-last-message`.
- For interactive use, inspect app-server events rather than scrape terminal
  rendering.

## Why the Other Choices Differ

### Standalone program

This is an ownership boundary, not a trigger mechanism. It is still the right
shape for the core because it can support Codex today and other agents later.
It can expose subcommands such as `agentrelay notify`, `agentrelay speak`, and
`agentrelay doctor` while sharing one queue, provider interface, and player.

### Command or wrapper

A command called by `notify` is simple and native. A wrapper that launches
Codex has more control and can consume structured `codex exec --json` events,
but users must always start Codex through that wrapper. Wrapping an interactive
TUI and parsing its visible output is substantially less reliable than handling
structured events.

### MCP

MCP is designed to expose data and actions to the agent. A `speak` tool is
useful for explicit speech commands, previews, voice selection, or future
agent-directed audio. It is not a reliable event listener: the model controls
whether and when the call happens. Using MCP for every final response also
duplicates the response text in tool arguments and delays completion.

### Plugin

A plugin is the right installation and distribution unit, but not the TTS
runtime architecture. It can eventually bundle:

- an installer/configuration command;
- a skill describing voice-related workflows;
- the `notify` adapter or hook configuration;
- optional MCP tools for explicit controls;
- platform-specific player assets.

Keep the standalone binary usable without the plugin so it remains testable and
portable.

### `AGENTS.md` plus `<tts>` tags

Tags solve text selection but not observation: another component must still
receive the response. They are also probabilistic model output, leak protocol
markup into the visible answer, consume tokens, and can break around Markdown
or partial streaming.

For the MVP, speak the final assistant message from the host event and apply a
deterministic cleanup policy. Later, add an explicit structured speech directive
only if users need spoken text to differ from visible text. If markup is kept as
a compatibility mode, use it as an opt-in filter rather than the core protocol.

## Streaming Clarification

There are two distinct kinds of streaming:

1. **Response streaming:** begin synthesis while Codex is still generating.
2. **Audio streaming:** begin playback before synthesis of the complete text has
   finished.

A turn-end `notify` integration supports audio streaming but not response
streaming. That is acceptable for an MVP and avoids speaking sentences Codex
later revises. True response streaming requires structured delta events from a
wrapper or app-server integration, plus sentence segmentation, cancellation,
ordering, and backpressure.

## Recommended MVP Scope

- macOS first, using a simple local player and one TTS provider;
- `agentrelay speak TEXT` for direct testing;
- `agentrelay notify EVENT` for Codex integration;
- a single-worker queue with interruption/cancellation policy;
- deterministic Markdown/code/URL cleanup;
- config for enable/disable, voice, speed, maximum characters, and provider;
- `agentrelay doctor` to verify Codex config, provider availability, and audio;
- unit tests for payload parsing, cleanup, deduplication, and queue behavior.

Defer voice cloning, ASR, VAD, cross-device audio, and token-level response
streaming. Those require different privacy, latency, and interaction decisions
and should build on the same provider-neutral daemon rather than enter the first
vertical slice.

## Open Validation

The official Codex manual endpoint returned HTTP 403 during this analysis, so
the exact current `notify` payload was not taken from documentation. The
installed CLI and local configuration establish that `notify` exists, but its
payload and support across CLI, desktop app, and IDE must be confirmed with a
small capture command before coding the adapter.
