# AgentRelay Push-to-Talk

Date: 2026-08-10

## Goal

Let a user speak an editable prompt to Codex without turning AgentRelay into an
always-listening assistant or automatically executing recognized text.

## Interaction

```text
user starts command -> explicit recording -> Enter or timeout stops recording
-> local transcription -> print + clipboard -> optional paste -> manual submit
```

The default command is:

```sh
python3 agentrelay.py push-to-talk
```

`--paste` sends Command-V to the currently focused application after copying
the transcript. It never sends Return. This keeps recognition errors visible
and gives the user a confirmation boundary before Codex acts.

## Architecture

`agentrelay_voice.swift` uses `AVAudioEngine` for ephemeral microphone capture
and `SFSpeechRecognizer` for transcription. `agentrelay.py` compiles the helper
on first use into `~/.config/agentrelay/bin/agentrelay-voice`, invokes it as a
child process, parses one JSON result, and handles clipboard or paste delivery.

The helper embeds an Info.plist section with microphone and Speech Recognition
usage descriptions. No recording is written to disk. AgentRelay logs only
metadata such as locale, character count, delivery state, and whether cloud
recognition was allowed; it does not log the transcript.

## Privacy And Safety

- Recording exists only while the command is active.
- On-device recognition is required by default.
- `--allow-cloud` is an explicit per-invocation opt-in to Apple cloud speech.
- Recognized text is never automatically submitted to Codex.
- Clipboard delivery is the default; simulated paste is an explicit option.
- Always-listening VAD, wake words, and voice command execution are out of
  scope for this version.

## Permissions

The first recording requests macOS Microphone and Speech Recognition access.
`--paste` additionally requires Accessibility permission for the terminal or
application that launches AgentRelay. Denied permissions produce structured
reasons and can be changed in System Settings > Privacy & Security.

## Failure Behavior

- Missing Swift toolchain or a failed first-use build stops before recording.
- Unsupported locale, unavailable on-device recognition, missing microphone,
  denied permission, and empty recognition return distinct failure reasons.
- Clipboard failure still prints the transcript to standard output.
- Paste failure leaves the transcript in the clipboard for manual recovery.

## Deferred Work

- configurable global hotkey or menu-bar launcher;
- partial transcript display;
- local Whisper fallback for unsupported on-device locales;
- vocabulary hints for technical terms;
- VAD, wake words, and hands-free command confirmation.

Those features should preserve the editable, non-submitting safety boundary.
