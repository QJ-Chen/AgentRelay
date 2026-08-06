# Codex Speech Guidance

## AutoTTS model-directed speech

Use the AutoTTS speak-update command only when the information is useful to a
user who may not be watching the screen. Good cases are a meaningful milestone,
a material change of approach, a blocker or required user action, the start or
finish of a long-running operation, and the final outcome of substantial work.

Do not speak routine reads, searches, edits, individual test commands, repeated
status, code, paths, URLs, logs, raw output, secrets, or detailed explanations.

Generate a separate concise Chinese summary rather than reading commentary
verbatim. Use one or two sentences, usually 20-60 Chinese characters, with a
hard maximum of 200 characters. Lead with the result, blocker, or next action.

Run from the AutoTTS checkout:
python3 autotts.py speak-update "简短播报"

For a blocker or user action that should be heard despite the normal cooldown,
add `--priority important`. The command is asynchronous; continue working after
it returns. If it reports `skipped`, do not immediately retry the same update.

The command enforces length, cooldown, deduplication, queue age, and cleanup.
The instruction controls model judgment, while the runtime remains the final
cost and safety guard.
