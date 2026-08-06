# Local TTS Model Plan

## Objective

Replace macOS `say` with a more natural local Mandarin voice while preserving
the properties that make the notify MVP useful:

- speech starts soon after a Codex turn completes;
- Chinese is the default and mixed Chinese/English technical text works;
- no response text or audio leaves the machine;
- installation and recovery are automated;
- the existing Computer Use notify relay continues to work;
- `say` remains an always-available fallback.

Target machine: Apple M3 MacBook Air, 16 GB unified memory, arm64. A local TTS
engine must work comfortably alongside Codex and normal development tools, not
merely fit when it is the only active process.

## Recommendation

Use **MeloTTS Chinese** as the first neural provider to implement and benchmark.
Keep **Qwen3-TTS 0.6B CustomVoice** as a second, optional quality tier if it
proves practical on Apple Silicon. Do not make CosyVoice, Fish Speech, or voice
cloning part of the default installation yet.

This is a benchmark-led recommendation, not a final model lock-in. MeloTTS has
the best fit on paper because upstream explicitly supports Chinese-English
mixing, claims CPU real-time inference, uses the MIT license, and is much
smaller operationally than current large speech-generation models. Its voice
quality must still beat `say` on the project corpus before it becomes default.

## Candidate Comparison

| Engine | Relevant strengths | Local M3 concerns | Role |
|---|---|---|---|
| MeloTTS | Mandarin plus mixed English; CPU real-time claim; MIT; mature project | Older Python/dependency stack; limited expressiveness and voice choice | First implementation candidate |
| Qwen3-TTS 0.6B CustomVoice | Strong Mandarin voices; streaming architecture; natural speech; Apache 2.0 | Upstream examples are CUDA-oriented; MPS support and 16 GB latency are unproven | Optional quality tier after a feasibility spike |
| CosyVoice 3 0.5B | Mandarin strength, instruction control, dialects, cloning, streaming; Apache 2.0 | Conda/submodules/native resources; upstream deployment emphasizes GPU; high integration cost | Revisit for cloning or remote GPU mode |
| Fish Speech | High quality and cloning; strong Chinese claims | Research license rather than a standard permissive model license; heavier runtime | Exclude from default distribution |
| Kokoro | Small, fast, excellent ONNX deployment ecosystem | Mandarin quality and mixed technical text are not its strongest documented path | Benchmark only if MeloTTS quality disappoints |
| Piper/system voices | Very light and dependable | Quality ceiling; weak Mandarin model choice compared with neural candidates | Fallback, represented today by `say` |

Upstream references checked on 2026-08-06:

- [MeloTTS](https://github.com/myshell-ai/MeloTTS): MIT; Chinese-English mixing and CPU real-time inference are explicit upstream claims.
- [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS): Apache 2.0; 0.6B and 1.7B models, Mandarin voices, streaming, and cloning.
- [CosyVoice](https://github.com/FunAudioLLM/CosyVoice): Apache 2.0; 0.5B current model, Mandarin/dialects, streaming, and cloning.
- [Fish Speech](https://github.com/fishaudio/fish-speech): custom Fish Audio Research License.

Project popularity, demos, and vendor latency numbers are not acceptance
evidence. Measurements must be taken on the target M3 using this workload.

## Target Architecture

```text
Codex notify
    |
    v
AutoTTS queue -> persistent local daemon -> text segmentation
                                         -> provider
                                            |-- system_say
                                            |-- melotts
                                            `-- qwen3_tts (optional)
                                         -> PCM/WAV chunks -> player
```

The neural model must live in a persistent sidecar process. Loading a model for
every notification would dominate latency and would couple model dependencies
to the dependency-free notify adapter. The adapter should continue returning
quickly even if the daemon is starting, unavailable, or unhealthy.

Use a dedicated `uv` environment and model cache outside the repository. Do not
install model packages into the system Python and do not commit model weights.
The daemon protocol should carry a provider-neutral request:

```json
{
  "id": "turn-id-or-content-id",
  "text": "cleaned response",
  "language": "zh-CN",
  "voice": "default",
  "speed": 1.0,
  "interrupt": true
}
```

Start with a Unix domain socket or loopback HTTP endpoint. The protocol choice
is less important than keeping provider imports and model lifetime out of
`autotts.py`.

## Benchmark Gate

Create a fixed corpus of 25-40 utterances before selecting the provider:

- short Mandarin completion confirmations;
- multi-sentence explanations;
- Chinese mixed with `Python`, `Codex`, `JSON`, file paths, versions, and CLI commands;
- numbers, dates, percentages, units, acronyms, and punctuation;
- Markdown-cleaned lists and headings;
- uncommon technical terms and proper nouns;
- one near the configured maximum spoken length.

Record these metrics for `say`, MeloTTS, and every later candidate:

- cold model startup time;
- warm synthesis time and real-time factor;
- time to first playable audio;
- peak resident memory;
- generated duration and output size;
- crashes or malformed output;
- pronunciation errors on a written checklist;
- blinded preference score for naturalness, clarity, and listening fatigue.

Promotion requirements for the default local provider:

- warm first audio at or below 800 ms for a typical 1-2 sentence response;
- real-time factor below 0.5 on the M3;
- daemon memory below 2.5 GB for the lightweight tier;
- no critical Mandarin text-normalization errors in the corpus;
- mixed Chinese/English is intelligible without manual markup;
- clear subjective improvement over `say` in at least 80% of paired samples;
- 20 consecutive requests complete without daemon restart or queue loss.

Qwen3-TTS may use a separate `quality` profile with a higher memory ceiling,
but it must leave enough unified memory for Codex. Reject it for this laptop if
peak process memory exceeds 6 GB, warm first audio exceeds 1.5 seconds, MPS
falls back to impractically slow CPU execution, or installation requires
fragile local patches.

## Implementation Phases

### Phase 1: Provider Boundary and Benchmark Harness

1. Extract the current `say` invocation behind a `TTSProvider` interface.
2. Add a persistent daemon with health, synthesize, stop, and shutdown actions.
3. Make the player own a cancellable process so `queue_mode=replace` interrupts
   current audio, not only queued requests.
4. Add `autotts benchmark`, `autotts voices`, and expanded `doctor` diagnostics.
5. Add the fixed corpus and emit JSON/Markdown benchmark reports.
6. Keep `system_say` as the default throughout this phase.

Exit condition: current notify behavior passes regression tests through the new
provider boundary, including daemon-down fallback to `say`.

### Phase 2: MeloTTS Spike

1. Pin MeloTTS and transitive dependencies in an isolated `uv` environment.
2. Download weights through `autotts model install melotts`; verify checksums
   where upstream publishes them and record model/license metadata.
3. Keep the model warm in the daemon and synthesize sentence-sized chunks.
4. Normalize Mandarin numbers, paths, code identifiers, and abbreviations before
   synthesis; do not ask Codex to emit pronunciation markup.
5. Benchmark the official implementation first. Consider ONNX conversion only
   if official inference passes quality but misses latency or packaging goals.
6. Add provider-specific voice and speed validation to `doctor`.

Exit condition: MeloTTS meets every default-provider promotion requirement.
If it fails quality, retain the provider work and move to the Qwen spike. If it
only fails packaging or latency, evaluate a maintained ONNX runtime before
discarding it.

### Phase 3: Playback and Perceived Latency

1. Segment cleaned text at Chinese and English sentence boundaries.
2. Synthesize the first short segment immediately while later segments queue.
3. Play ordered PCM/WAV chunks without gaps where the provider permits it.
4. Cancel outstanding synthesis and playback when a newer replace-mode turn
   arrives.
5. Bound queue age so a delayed daemon never reads stale Codex responses aloud.

This is audio-generation streaming after Codex turn completion. Response-token
streaming remains outside this plan because `notify` only fires at turn end.

Exit condition: first audio meets the latency gate and cancellation works under
rapid consecutive Codex turns.

### Phase 4: Qwen3-TTS Feasibility

1. Test only the 0.6B CustomVoice model first, in a separate environment.
2. Verify native MPS execution, unsupported operators, actual fallback behavior,
   cold/warm latency, memory, and long-text stability.
3. Compare its Mandarin preset voices against MeloTTS using the same corpus.
4. Expose it as `provider=qwen3_tts` or profile `quality`; do not silently
   replace the lightweight default.
5. Attempt the 0.6B Base cloning model only after preset-voice synthesis passes.

Exit condition: ship the optional profile only if it passes its higher resource
gate without local source patches. Otherwise document it as suitable for the
restricted remote GPU workspace, not this laptop.

### Phase 5: Voice Cloning, Separately Gated

Voice cloning requires explicit consent, local reference-audio management, and
clear deletion controls. Add it only after ordinary local synthesis is stable:

- `autotts voice import` validates and copies a reference clip locally;
- reference audio and derived embeddings never leave the machine;
- `autotts voice delete` removes both source and cached artifacts;
- cloned voices are never enabled implicitly;
- the UI/CLI identifies synthetic output and records the model/license version.

## Configuration Evolution

Keep old configuration valid and add fields incrementally:

```json
{
  "provider": "system_say",
  "fallback_provider": "system_say",
  "language": "zh-CN",
  "voice": "default",
  "speed": 1.0,
  "model_profile": "lightweight",
  "daemon_idle_seconds": 900,
  "max_queue_age_seconds": 30
}
```

Provider installation should be explicit. `autotts install` must continue to
install only the notify integration; `autotts model install melotts` may
download the neural runtime and weights. This keeps the base MVP reversible and
prevents a notification setup command from unexpectedly downloading gigabytes.

## Tests Required

- provider contract tests using a fake synthesizer;
- daemon startup, health, idle shutdown, and crash recovery;
- fallback to `say` on timeout, invalid audio, or missing weights;
- ordered chunk playback and cancellation;
- queue age and deduplication across daemon restarts;
- Chinese/English segmentation and normalization snapshots;
- configuration migration from the current MVP;
- install/uninstall behavior without touching unrelated Codex settings;
- a 20-request local soak test and recorded benchmark report.

## Main Risks

- **Dependency compatibility:** speech projects often pin older Python/Torch
  versions. Isolated environments and subprocess boundaries are mandatory.
- **Apple Silicon support:** CUDA claims do not imply MPS performance. Treat any
  unmeasured M3 latency as unknown.
- **Unified memory pressure:** a model that technically loads can still degrade
  Codex and the rest of the laptop.
- **Text normalization:** natural timbre does not compensate for misread paths,
  versions, symbols, and mixed-language terms.
- **Licensing:** verify both repository code and downloaded model-weight terms;
  they can differ.
- **Stale speech:** neural generation adds delay, making cancellation and queue
  age limits correctness requirements rather than polish.

## Immediate Next Work

Implement Phase 1 and the MeloTTS spike as one development increment, but do
not switch the active configuration automatically. Generate a benchmark report
and paired audio samples, then promote MeloTTS only when the measured gate is
met. This preserves the working notify MVP while producing evidence for the
model decision.
