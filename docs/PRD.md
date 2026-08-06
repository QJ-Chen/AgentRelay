I want to add a tts layer to codex.

Naive plan:
- stand-alone program （Is tool/command/mcp possible?）
- append instruction to ~/.codex/AGENTS.md like: "For direct response to the user, output a informative, precise message wrapped with <tts>.....</tts> "
- The program detect the <tts> tag and send the content to tts service (local deploy or cloud service)
- play the generated audio somehow -- realtime, streamly

Requirement:
- The projects should be easy for codex to self-apply
- brainstorming the overall plan first
- Finish the MVP first.
- Consider more: voice clone, voice input (asr, vad....) etc.
- support language selection, default to chinese