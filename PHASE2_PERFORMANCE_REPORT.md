# Capri Phase 2 performance report

Measured on 2026-11-11 with Python 3.11.6, 16 logical CPUs and an RTX 3060 with 12 GiB VRAM. Kokoro remains CPU-based, speaker 53, base speed 1.1 and four threads. Whisper remains `base`, CPU int8, beam size 1. The primary model remains `qwen2.5:32b`; the pre-existing `auto` conversation selection chooses installed `qwen3:8b`. No models, sprites, personality instructions or dependencies were replaced.

These are real local CPU/GPU engine measurements using generated speech, fixed prompts and generated vision imagery. Microphone recording and speaker output were **not** used. Audio readiness is not audible speech. The League API was unavailable; League measurements use explicit mock API data. Hardware-independent unit tests are separate from these benchmarks.

## Before and after

Times are milliseconds. Each side has 20 samples. Percentages are reductions in p50 / p95, not guarantees for live use.

| Measurement | Before p50 / p95 | After p50 / p95 | Reduction p50 / p95 | Samples | Measurement type |
|---|---:|---:|---:|---:|---|
| Follow-up request to first text, three-second gap | 3355.82 / 3434.75 | 102.30 / 211.07 | 96.95% / 93.85% | 20 / 20 | Real GPU/Ollama; paced follow-up experiment |
| Cancellation racing final TTS chunk to next voice worker ready | 1677.56 / 1711.13 | 2.70 / 2.77 | 99.84% / 99.84% | 20 / 20 | Real CPU Kokoro; injected cancellation, no playback |
| Whisper transcription, paired replay | 460.45 / 486.11 | 448.70 / 465.22 | 2.55% / 4.30% | 20 / 20 | Real CPU Whisper; identical generated speech |
| Speech end estimate to first audio ready, paired replay | 2472.69 / 2690.95 | 2467.65 / 2545.59 | 0.20% / 5.40% | 20 / 20 | Real VAD/STT/Ollama/Kokoro; paced speech fixture |
| Cancellation during ongoing synthesis to generator stopped | 2.32 / 2.58 | 2.30 / 2.62 | No meaningful change | 20 / 20 | Real Kokoro cancellation; no speaker output |
| Human speech end to audible response | Unavailable | Unavailable | Unavailable | 0 | Requires microphone/acoustic measurement |
| Interruption to physical sound cessation | Unavailable | Unavailable | Unavailable | 0 | Requires output/loopback measurement |

The follow-up experiment uses the same models, test image, question and real three-second idle gap. With the old two-second policy, background vision entered in 20/20 trials and caused model swapping. With six seconds, it entered in 0/20 trials before the follow-up. This prevents that particular delay; a vision load already underway can still delay a new request. Background analysis now waits four additional seconds after a completed user turn. Live capture and explicit foreground screen questions remain available.

The cancellation gain applies when cancellation races Kokoro's final chunk and its completion acknowledgment. The worker now reports its actual final-chunk status. Only that case gets a bounded acknowledgment drain, preserving the resident voice in 20/20 trials. Incomplete generation still terminates immediately. Its next-worker readiness remained approximately 1.67 seconds median, reflecting a necessary reload. This does not mean a new spoken response starts in 2.7 ms.

The ordinary no-contention voice median barely changed. Its p95 improved in this sample, but 20 observations do not establish production tail behavior. The decoder comparison alternated settings on identical paced audio with the same prompt, seed, models and empty fixture memory. All 20 paired transcripts matched.

## Pipeline measurements and interpretation

After the decoder change, paired replay produced:

| Stage | p50 ms | p95 ms | p99 ms | Maximum ms | n |
|---|---:|---:|---:|---:|---:|
| PCM speech-end estimate to VAD endpoint | 588.95 | 589.29 | 589.29 | 589.41 | 20 |
| Whisper transcription | 448.70 | 465.22 | 465.22 | 470.82 | 20 |
| STT complete to model dispatch | 2.36 | 2.55 | 2.55 | 2.57 | 20 |
| Model dispatch to first non-whitespace text | 229.68 | 335.76 | 335.76 | 351.04 | 20 |
| First text to actual TTS request | 201.52 | 297.09 | 297.09 | 298.41 | 20 |
| Text queue waiting, within the preceding interval | 0.16 | 0.26 | 0.26 | 0.26 | 20 |
| TTS setup/lock waiting before worker dispatch | 2.47 | 2.70 | 2.70 | 16.92 | 20 |
| TTS request to first generated WAV available | 945.71 | 1003.31 | 1003.31 | 1019.39 | 20 |
| Speech-end estimate to first audio ready | 2467.65 | 2545.59 | 2545.59 | 2583.81 | 20 |

Intervals overlap; their percentiles should not be added. The existing percentile method selects bounded-sample order statistics. With 20 samples, p95 and p99 select the same observation; maximum is reported separately. Raw JSON includes all sample counts and aggregate tails.

The largest ordinary-path stages remain Kokoro first-audio generation, endpoint detection, and recognition. Prompt construction/dispatch and TTS queueing were already small. Persistent HTTP connections, model warm-up, chunk prefetch, native Kokoro streaming and bounded queues were retained.

Tracing now distinguishes text queue entry, TTS request, host dispatch to Kokoro, generated WAV availability, decoded PCM readiness, PortAudio buffer submission, estimated DAC presentation, driver abort return and cancelled-turn cleanup. Incoming interruption traces also measure the start of new STT work. Host dispatch is not claimed to be native computation start. Existing CPU/wall spans and native process CPU measurements remain available.

`device_playback` uses PortAudio's DAC timestamp for the first non-silent sample. It estimates presentation, not sound arriving at the listener. `interruption_to_output_abort` ends when the driver abort call returns, not when a microphone detects silence. Neither device metric was populated by these silent runs. Real input speech-end estimates also have unknown input-driver latency.

Tracing stays disabled by default, bounded to 2048 retained samples per metric, with no raw audio, transcripts, identities, credentials or high-frequency GUI updates.

## Other experiments

| Scenario | Results | Scope |
|---|---|---|
| Background vision already admitted | Text-only TTFT 23.90 / 28.39 ms p50/p95; concurrent TTFT 3624.15 / 3911.43 ms; preempted 20/20 vision calls | Real local models; generated image, no active game |
| Explicit `qwen2.5:32b` complex question | Cold first text 26495.99 ms, n=1; warm p50/p95/p99/max 1217.98 / 1259.15 / 1259.15 / 1282.14 ms, n=20 | TTFT only; cancelled after first text, not a full voice/decode benchmark |
| Normal desktop UI, deterministic League question | Speech-end-to-audio-ready p50/p95/p99/max 1460.23 / 1487.01 / 1487.01 / 1514.95 ms, n=20 | Real offscreen GUI, VAD/STT/TTS; mock champion select, no LLM answer call |
| League Companion UI, same question | 1471.08 / 1502.31 / 1502.31 / 1535.38 ms, n=20 | Same setup, docked; roughly 11 ms median difference, not proof of zero overhead |
| Consecutive conversation with retained history | 2729.13 / 2882.88 / 2882.88 / 3340.87 ms, n=20 | Real VAD/STT/Ollama/TTS; explicit repeat requests, no speakers |
| Varied recognition corpus | 111 reference words across 20 generated questions; timestamp mode 6 word-edit errors, text-only 5; one transcript disagreement | Small clean synthetic corpus, not human/noise/accent validation |
| TTS threads 2 / 4 / 6 / 8 | First-audio medians 1247.08 / 887.42 / 834.58 / 882.54 ms, n=20 each | Same sentence/voice. Four retained: six gave only ~6% improvement with a larger thread budget |
| VAD pause sweep | At a 350 ms inserted gap, 250/350 ms thresholds split the generated sentence; 500 ms preserved it | Real Silero, accelerated sample replay. Retained 500 ms; human pause corpus still needed |

Ollama reported 21,181,822,400 bytes total versus 10,505,745,857 bytes VRAM for 32B. Inference: its placement outside VRAM is a material constraint. No quantization or model selection was changed. An isolated first-token result does not establish fast subsequent generation.

The GUI benchmark constructs the window and loads SVGs before response timing, then reuses it. Earlier exploratory per-request-window runs and the exploratory cancellation run overlapping tests were excluded. Performance runs reported here were serial and separate from pytest.

An early unconditional cancellation-drain prototype delayed ongoing-generation cleanup. It was replaced with the final-chunk-gated version; final continuing-generation cancellation remained about 2.3 ms. No extra production thread, polling worker, GPU scheduler or playback system was added.

## Code and configuration

| File | Purpose |
|---|---|
| `config.py` | Text-only recognition option, final-ack drain budget, configurable six-second background quiet period |
| `core/latency.py` | Separate queue/synthesis/PCM/device/abort stages; new-STT timing; per-run collectors |
| `core/model_client.py` | Exclude whitespace from first useful text timing; preserve streaming order and metadata |
| `core/streaming.py` | Mark text queue admission separately from a real TTS request |
| `gui/workers.py` | Serialize playback-state signals; ignore late input callbacks after mute; associate interruption traces with old/new work |
| `voice/voice.py` | Avoid unused timestamp tokens; mark actual transcription start |
| `voice/kokoro_session.py` | Preserve completed warm synthesis on cancellation; terminate incomplete work; bounded acknowledgment wait |
| `voice/kokoro_worker.py` | Forward native final-chunk status; synthesis text, samples, speaker and speed unchanged |
| `voice/tts.py` | Distinguish decoded PCM from generated WAV readiness |
| `voice/playback.py` | Record buffer submission and driver abort separately from DAC presentation |
| `benchmark_voice.py` | Paired decoder replay, corpus/pause tests, consecutive turns, desktop/League UI, real-model contention/follow-ups, optional playback/abort probes |
| `tests/test_voice_phase2.py` | New timing, decoder, completion-race, signal-order, muting, scheduler, benchmark isolation and cleanup tests |
| `PHASE2_PERFORMANCE_REPORT.md` | This report |
| `PERFORMANCE_REPORT.md` | Link to current Phase 2 results; earlier measurements retained as history |

League session detection, docking, movement lock, SVG files, sprite selection and League polling were not rewritten. The existing speech signal remains the League talking-pose source.

| Setting | Default | Reversal or tuning |
|---|---|---|
| `WHISPER_WITHOUT_TIMESTAMPS` / `CAPRI_STT_TEXT_ONLY` | true / `1` | `0` restores timestamp decoding; timestamps were never consumed by Capri |
| `INFERENCE_BACKGROUND_QUIET_SECONDS` / `CAPRI_BACKGROUND_QUIET_SECONDS` | 6 seconds | `2` restores the prior follow-up/background freshness tradeoff |
| `TTS_CANCEL_DRAIN_SECONDS` | 0.05 seconds | Set 0 in config to restore immediate disposal even for completed native chunks |
| Existing `CAPRI_LATENCY_METRICS` | `0` | Set `1` for numeric production aggregates on exit |

There are no new dependencies. Voice settings, 500 ms endpointing and inference priority order remain intact. The added quiet time affects background analysis admission, not explicit user requests or League API ingestion.

## Validation and reproducible Windows commands

Final suite: **639 passed in 71.37 seconds**. Actual `main.main()` offscreen startup, League override, restoration and shutdown passed with optional network/model/microphone services disabled. The suite includes the existing 34 League Companion Mode cases. No test requires League, Ollama or a microphone; optional installed-engine tests ran with the local voice pack.

Run isolated benchmarks with Capri closed, one command at a time, from `F:\Neko`:

```powershell
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider

# Paired recognition and complete silent voice path: 20 samples per setting.
.\.venv\Scripts\python.exe -B benchmark_voice.py --samples 20 --replay --compare-stt --profile --output artifacts/performance/local_paired.json

# Varied recognition and retained conversation history.
.\.venv\Scripts\python.exe -B benchmark_voice.py --samples 20 --stt-corpus --output artifacts/performance/local_stt.json
.\.venv\Scripts\python.exe -B benchmark_voice.py --samples 20 --replay --consecutive --profile --output artifacts/performance/local_consecutive.json

# Same voice path, same mock League question, both UI modes.
.\.venv\Scripts\python.exe -B benchmark_voice.py --samples 20 --replay --league-fixture --desktop-ui --profile --output artifacts/performance/local_desktop.json
.\.venv\Scripts\python.exe -B benchmark_voice.py --samples 20 --replay --league-fixture --companion --profile --output artifacts/performance/local_companion.json

# Real GPU contention and paced follow-up policy comparison; generated images only.
.\.venv\Scripts\python.exe -B benchmark_voice.py --samples 20 --contention --output artifacts/performance/local_contention.json
.\.venv\Scripts\python.exe -B benchmark_voice.py --samples 20 --followups --output artifacts/performance/local_followups.json

# Cancellation race control and optimized case, no speakers.
.\.venv\Scripts\python.exe -B benchmark_voice.py --samples 20 --interrupt --cancel-drain-ms 0 --output artifacts/performance/local_cancel_zero.json
.\.venv\Scripts\python.exe -B benchmark_voice.py --samples 20 --interrupt --output artifacts/performance/local_cancel.json

# Explicit installed 32B: first-token test, then an optional full complex voice replay.
.\.venv\Scripts\python.exe -B benchmark_voice.py --samples 20 --ollama --model qwen2.5:32b --first-text-only --question "Explain how Bayesian updating handles conflicting evidence, using a concrete example." --output artifacts/performance/local_32b_ttft.json
.\.venv\Scripts\python.exe -B benchmark_voice.py --samples 20 --replay --model qwen2.5:32b --question "Explain how Bayesian updating handles conflicting evidence, using a concrete example." --profile --output artifacts/performance/local_32b_voice.json
```

The first model request is cold only if `/api/ps` showed it absent. Residency is saved in the JSON. To create an explicit cold trial, stop the chosen model with `ollama stop qwen2.5:32b` while Capri is closed, then run the test; subsequent samples measure warm requests. Full 32B voice replay can be substantially slower than TTFT-only probes and was not run here.

Explicit speaker-output and driver-abort measurements:

```powershell
.\.venv\Scripts\python.exe -B benchmark_voice.py --samples 20 --replay --audio --profile --output artifacts/performance/local_device_voice.json
.\.venv\Scripts\python.exe -B benchmark_voice.py --samples 20 --interrupt --audio --output artifacts/performance/local_device_abort.json

# Supply a representative 16-bit PCM WAV with natural pauses; no microphone is opened.
.\.venv\Scripts\python.exe -B benchmark_voice.py --samples 20 --replay --compare-stt --recording .\voice-pause-fixture.wav --profile --output artifacts/performance/local_recording.json
.\.venv\Scripts\python.exe -B benchmark_voice.py --vad-sweep --recording .\voice-pause-fixture.wav --output artifacts/performance/local_pauses.json
```

For real League matches, input devices and background vision ON/OFF, collect at least 20 turns in separate normal application sessions, then exit normally to print aggregates:

```powershell
$env:CAPRI_LATENCY_METRICS = "1"
$env:CAPRI_VISION = "1"
.\.venv\Scripts\python.exe -B main.py
$env:CAPRI_VISION = "0"
.\.venv\Scripts\python.exe -B main.py

# Optional headphone barge-in test; no acoustic echo canceller is present.
$env:CAPRI_BARGE_IN = "1"
$env:CAPRI_VISION = "1"
.\.venv\Scripts\python.exe -B main.py
Remove-Item Env:CAPRI_BARGE_IN, Env:CAPRI_VISION, Env:CAPRI_LATENCY_METRICS
```

## Limits and next recommendation

No active League match, human microphone corpus, game FPS comparison, real DAC playback trial or acoustic loopback was available/performed. Live League+vision ON/OFF, human interruption timing and full complex 32B voice latency remain local integration measurements. Offscreen drawing is not physical multi-monitor testing; the user-confirmed League behavior was preserved and its automated regressions passed.

The next largest ordinary-path target is Kokoro's approximately 946 ms first-audio time. Evaluate first-phrase synthesis/chunk choices with paired timing and listening tests before changing pronunciation or prosody. Endpointing adds another approximately 589 ms; the pause experiment shows why lowering it globally would be unreliable. Neither first-token nor synthesis-readiness timing should be presented as instantaneous audible conversation.

Final numeric artifacts are under `artifacts/performance/`: `phase2_paired_replay.json`, `phase2_stt_corpus.json`, `phase2_probes.json`, `phase2_followups_real.json`, `phase2_contention.json`, `phase2_interrupt_zero_clean.json`, `phase2_interrupt_final.json`, `phase2_qwen32_ttft.json`, `phase2_desktop_steady.json`, `phase2_companion_steady.json`, `phase2_consecutive.json`, and `phase2_vad_pauses.json`. The generated pause WAVs are also available for replay. Exploratory files are excluded from final comparisons.
