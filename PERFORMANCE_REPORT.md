# Capri voice performance report

Measured on this workstation during the implementation session: Python 3.11.6, 16 logical CPU processors, RTX 3060 with 12,288 MiB VRAM, Ollama server 0.34.3, Ollama Python SDK 0.6.3, faster-whisper 1.2.1, sherpa-onnx 2.0.1. Windows WMI did not permit reading the exact CPU model. Other applications were not controlled, so these are local observations rather than production latency guarantees.

The final serial replay improved from **4.881 s to 2.692 s p50**, about **45% lower latency to first audio ready**. It used real Silero VAD, Whisper, Ollama and Kokoro with paced synthesized speech. It did not open the microphone or play the response. Both runs used the existing automatic model choice, `qwen3:8b`; this is not a 32B performance claim. A clean archive of the original committed code was used for the replay baseline, with the same neutral question, model, prompt, seed and empty memory fixture. Intended optimization settings changed: two to four TTS threads, native TTS chunks, and CPU int8 recognition for the default small Whisper preset.

## Measured before and after

All values below are measured wall time in milliseconds. Percentiles use the project's existing bounded-sample order statistics. Three to six voice trials are too few to characterize real production tails; p95/p99 can coincide. Maximum and sample counts are included explicitly. Run longer local sessions for representative tails.

| Measurement | Samples before/after | p50 ms before -> after | p95 ms before -> after | p99 ms before -> after | Max ms before -> after |
|---|---:|---:|---:|---:|---:|
| Real-engine replay: fixture speech end to first audio ready | 3/3 | 4881.385 -> 2691.619 | 4881.385 -> 2691.619 | 4881.385 -> 2691.619 | 5820.909 -> 2849.413 |
| Real Ollama: warm fixed-prompt first token | 5/5 | 205.065 -> 26.718 | 207.825 -> 26.809 | 207.825 -> 26.809 | 228.890 -> 30.445 |
| Real Kokoro: warm fixed-line complete WAV ready | 6/6 | 1134.375 -> 495.762 | 1385.975 -> 497.656 | 1385.975 -> 497.656 | 1392.571 -> 558.475 |
| Synthetic: first chunk ready | 6/6 | 203.978 -> 87.317 | 204.197 -> 90.095 | 204.197 -> 90.095 | 204.452 -> 91.426 |
| Synthetic: stalled HTTP cancellation | 6/6 | 31.808 -> 15.410 | 32.386 -> 17.004 | 32.386 -> 17.004 | 32.388 -> 17.269 |

The first automatic-model request in the original run took 4.567 s to its first token with no resident model. The first request in the later voice benchmark used an already resident model, so its 0.567 s is **not** a comparable cold-start improvement. Startup now loads the selected conversation model in the existing warm-up worker using an empty chat request and the existing 15-minute keep-alive. This follows the [Ollama API's model loading interface](https://github.com/ollama/ollama/blob/main/docs/api.md). Residency remains subject to available memory.

For the longer native TTS test phrase, audio became available at 651.871 ms p50 while complete generation took 1766.619 ms p50 (six actual native synthesis trials). Native callbacks are genuine generated audio, not a simulated streaming indicator. Kokoro chooses callback boundaries; it does not expose phoneme-by-phoneme audio.

## Pipeline and changes

Microphone input still goes through 32 ms Silero frames and the existing 500 ms silence endpoint, then the existing Whisper model. Cached League answers and verified spatial actions keep their direct routes. Other conversation streams through the existing selected Ollama model, natural text boundaries, the resident Kokoro worker, and the speaker device.

- Replaced per-request Ollama client/event-loop creation with one managed transport thread and a persistent connection pool. Stream callbacks execute on the requesting worker. Acknowledged, bounded delivery preserves ordering and cancellation without blocking the transport loop on TTS backpressure. Server token counts, load/prefill/decode times and final metadata are retained.
- Kept personality, useful memory, context freshness, model selection, tool behavior, 256-token conversation output budget and 15-minute keep-alive. Only immutable persona instructions are cached. Current screen evidence, memories and dialogue remain fresh.
- Added early first-clause submission when punctuation provides a natural boundary. Tiny interjections still group together; words, numbers and code blocks are not chopped to manufacture a low onset time. The 80-character setting is a preferred first-clause window, not a forced mid-sentence cut.
- Connected Kokoro's native audio callbacks to the existing preparation/playback threads. Native chunks can play while the same utterance is still being generated. Continuations are ordered and cannot suppress themselves as duplicate speech.
- Reused a lazy output-device stream across chunks and sentences in one reply. The audio callback copies PCM and publishes device timing; it performs no synthesis, network work, file access, GUI updates or telemetry aggregation. Remaining driver buffering and slow generation can still cause gaps; subjective device listening remains a manual check.
- Bounded the text queue at 16 entries, prepared audio at two entries, native worker messages at eight, transport delivery at 32, and optional input capture at one latest utterance. Cleanup and cancellation release queued files, workers and device ownership.
- Added a small foreground admission policy. Speech detection reserves foreground priority before STT. It cancels active background model requests and prevents new background inference until the user turn ends and a two-second quiet period passes. Background model work is serialized. Vision capture remains live; the analysis loop waits before acquiring a frame so a scheduler wait does not queue an old image.
- Added optional headphone interruption capture with one input/VAD thread and the existing STT consumer. A new utterance cancels the obsolete response and its chunks. No second recognizer is loaded. MIC still controls input only; muting it lets the current reply finish. STOP cancels output.
- Spatial and proactive replies retain full-WAV freshness/target validation before playback. Their existing verification and replacement behavior is preserved. Short deterministic lines benefit from faster synthesis; this route prioritizes verified speech over speculative early playback.

## Foreground inference and remaining GPU limits

With a generated test image and the actual local vision/text models, the affected text request's first token took **9360.975 ms before** and **3858.847 ms after** foreground preemption. This is one affected request per experiment, not a p95. The new vision request was cancelled about 504 ms after it started. Subsequent warm text requests were fast. Cancellation cannot undo an Ollama model load already underway, and model swapping still caused several seconds of delay.

The explicit `qwen2.5:32b` probe measured:

- First request TTFT: **26.146 s**, one nonresident-model trial.
- Subsequent TTFT: **415.042 ms p50**, two trials, maximum 415.271 ms.
- Decode rate: **2.541 tokens/second p50**, three trials.
- Ollama reported 21,181,822,400 total bytes versus 10,505,745,857 VRAM bytes for this model/context: about 19.73 GiB total and 9.78 GiB VRAM on a 12 GiB GPU.

Inference: model placement outside VRAM is a major constraint for 32B generation here. A low warm TTFT does not make the following tokens instantaneous. The configured primary model remains `qwen2.5:32b`, and automatic model selection is unchanged. No Ollama model was replaced, downloaded or requantized for this upgrade. Vision `qwen2.5vl:7b`, game rendering and 32B cannot be assumed to fit together.

## CPU and voice tradeoffs

Kokoro's model, pronunciation dictionary, speaker 53 and base speed 1.1 are preserved. Four native CPU threads were selected from actual measurements. On the long phrase, two versus four threads gave:

| Threads | First audio p50 ms | Generation wall p50 ms | Dedicated worker CPU p50 ms |
|---|---:|---:|---:|
| 2 | 993.583 | 2689.858 | 5359.375 |
| 4 | 681.023 | 1831.827 | 7265.625 |

Four threads reduced onset by about 31% but used about 36% more total CPU time in this probe. Set `CAPRI_TTS_THREADS=2` to favor the game's CPU budget. This changes computation parallelism, not voice pitch or speaking speed.

Whisper `base` CPU int8 and CUDA float16 produced the same neutral fixture transcript in all ten device-comparison trials. Subsequent trials were approximately 425-435 ms on CUDA and 430-459 ms on CPU. The first decode in that experiment was 1964.859 ms on CUDA and 442.846 ms on CPU. CPU is now the default for `tiny`/`base` presets to avoid their CUDA residency and initialization cost; larger custom presets keep automatic device selection. Override with `CAPRI_WHISPER_DEVICE=cuda` or `auto`. This is a small fixture comparison, not an accuracy corpus or noisy-microphone test.

The extra faster-whisper VAD pass was retained: disabling it showed no useful latency gain. The 500 ms silence setting was retained to protect natural pauses and recognition quality. These remain significant latency floors.

## Production profiling

Set `CAPRI_LATENCY_METRICS=1` before launching. On exit, Capri prints numeric aggregate voice and League reports. Samples are bounded to 2048 per metric, with total count, retained count, p50/p95/p99 and maximum. The disabled telemetry path reads no profiling clocks. Payloads, transcripts, audio, player identities and lockfile credentials are not recorded by telemetry.

An independent six-trial real-engine replay exercised the production stage profiler:

| Production stage, paced fixture | n | p50 ms | p95 ms | p99 ms | Max ms |
|---|---:|---:|---:|---:|---:|
| `speech_end_estimate_to_vad` | 6 | 588.787 | 589.096 | 589.096 | 589.444 |
| `vad_to_stt_wall` | 6 | 444.880 | 498.414 | 498.414 | 531.911 |
| `stt_to_dispatch` | 6 | 1.654 | 1.680 | 1.680 | 42.415 |
| `llm_ttft` | 6 | 223.839 | 320.167 | 320.167 | 383.524 |
| `first_token_to_tts_submit` | 6 | 213.076 | 215.484 | 215.484 | 313.580 |
| `tts_submit_to_first_audio_ready` | 6 | 1087.938 | 1111.786 | 1111.786 | 1125.065 |
| `speech_end_estimate_to_audio_ready` | 6 | 2604.617 | 2787.358 | 2787.358 | 2795.244 |

Additional metrics include parent-thread/process CPU time, dedicated Kokoro process CPU/wall time, server model load/prefill/decode duration and actual server-counted tokens/second. Parent process CPU includes other Capri threads; parent thread CPU excludes native decoder pools. Dedicated Kokoro CPU sums its worker threads. GPU compute time is not inferred from parent Python CPU time.

Playback is measured from PortAudio's DAC timestamp for the first non-silent PCM sample, not from TTS submission or `sounddevice.play()` returning. This estimates device presentation, not acoustic sound reaching a listener. The worker fires expression/start callbacks at presentation time. Native audio callbacks do not run Python application callbacks directly.

`audio_ready_to_device_playback`, `speech_end_estimate_to_device_playback`, and `league_detected_to_device_playback` are populated during real device playback. They were not populated by the no-speaker replay. Microphone speech end is estimated from Silero's unpadded sample index and delivered audio blocks; input driver latency is not known. Live event detection and notification enqueue have distinct timestamps, and game `EventTime` stays separate from local clocks.

## League audit and polling

The existing persistent Live Client connection, separate event/stats cadence, LCU WebSocket/bootstrap/reconnection, direct answers, baselining, event IDs, bounded history and speech priority were retained. No second League service or model call was added. Existing parsing was already fast enough that further protocol changes were not justified.

| League synthetic fixture | n | p50 ms | p95 ms | p99 ms | Max ms |
|---|---:|---:|---:|---:|---:|
| `lcu_message_to_snapshot` | 2000 | 0.0178 | 0.0184 | 0.0223 | 1.0959 |
| `question_to_direct_answer` | 2000 | 0.0048 | 0.0050 | 0.0058 | 1.0408 |
| `event_http_to_identified` | 500 | 0.8616 | 0.9733 | 1.0843 | 2.9560 |
| `event_to_callback` | 1000 | 0.0016 | 0.0017 | 0.0020 | 0.0062 |

These are in-process/mock-HTTP fixtures, not live Riot server latency. The synthetic polling experiment measured:

| Poll interval | Exposure-to-detection p50 ms | p95 ms | p99 ms | Max ms | New events | Process CPU seconds over ~5 s |
|---|---:|---:|---:|---:|---:|---:|
| 250 ms | 129.000 | 248.000 | 249.000 | 258.000 | 36 | 0.03125 |
| 500 ms | 257.000 | 475.000 | 483.000 | 507.000 | 33 | 0.015625 |

The default stays **250 ms**: lower detection delay for a small measured CPU cost. Exposure times in this table were simulated, not derived from Riot's game timestamps. The live checks found no lockfile and no running Live Client API. Actual match exposure/detection/voice latency therefore remains unmeasured.

## Settings and reversibility

| Setting | Default | Purpose |
|---|---|---|
| `CONVERSATION_PRELOAD_ON_STARTUP` / `CAPRI_PRELOAD_CHAT` | true / `1` | Empty-request model warm-up in the existing voice warm-up worker |
| `KOKORO_NUM_THREADS` / `CAPRI_TTS_THREADS` | 4 | Set 2 to reduce CPU use |
| `WHISPER_DEVICE` / `CAPRI_WHISPER_DEVICE` | CPU for tiny/base; auto otherwise | Override with cpu/cuda/auto; unavailable CUDA falls back to CPU |
| `TTS_NATIVE_STREAMING_ENABLED` | true | Set false for the earlier full-WAV prefetch path |
| `TTS_FIRST_CHUNK_MAX_CHARS` | 80 | Set 0 to use sentence-only boundaries |
| `TTS_TEXT_QUEUE_LIMIT` | 16 | Bounded text backpressure |
| `TTS_AUDIO_BLOCK_MS` | 10 | Device callback block size |
| `INFERENCE_PRIORITY_ENABLED` | true | Foreground admission and background preemption |
| `INFERENCE_BACKGROUND_QUIET_SECONDS` | 2.0 | Delay only background analysis after a user turn |
| `VOICE_BARGE_IN_ENABLED` / `CAPRI_BARGE_IN` | false / `0` | Headphones only; there is no acoustic echo canceller |
| `VOICE_LATENCY_METRICS_ENABLED` and `LEAGUE_LATENCY_METRICS_ENABLED` / `CAPRI_LATENCY_METRICS` | false / `0` | Optional aggregate profiling |
| `LEAGUE_EVENT_POLL_INTERVAL` | 0.25 s | Existing adjustable event polling |

Restart after editing configuration. No new dependencies or frameworks were added. The persistent model transport adds one mostly waiting I/O thread. Optional headphone mode adds one capture thread only when MIC is enabled. The existing synthesis subprocess and preparation/playback workers are reused.

## Validation and local commands

Final complete suite: **585 passed in 57.02 seconds**. A 250 ms offscreen `main.main()` smoke run exited successfully with optional capture/League/preload I/O disabled. Native voice generation, native chunks, pronunciation, ordering, cancellation, stale-response rejection, queue bounds, device reuse, worker shutdown, deterministic League responses, priority, vision recovery, spatial actions and microphone muting were covered. Fake-clock tests use isolated scheduling state.

Run in PowerShell from the project root:

```powershell
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider
.\.venv\Scripts\python.exe -B benchmark_voice.py --samples 10 --tts --ollama --contention --replay --profile --output artifacts/performance/local_voice.json
.\.venv\Scripts\python.exe -B benchmark_voice.py --samples 3 --ollama --model qwen2.5:32b --output artifacts/performance/local_qwen32.json
.\.venv\Scripts\python.exe -B benchmark_league.py --live --seconds 10 --output artifacts/performance/local_league.json
```

Run benchmarks serially, without the test suite running alongside them. Replay uses a temporary empty memory database, generated test speech and no microphone/speaker. Contention uses a generated image, never the desktop. `--audio` explicitly permits speaker playback of the fixed test line; omit it for silent measurements. A first request is cold only if the selected model is not already resident; residency is included in the JSON.

For normal-session profiling and optional headphone interruption:

```powershell
$env:CAPRI_LATENCY_METRICS = "1"
# Optional, only while wearing headphones:
$env:CAPRI_BARGE_IN = "1"
.\.venv\Scripts\python.exe -B main.py
```

Keep MIC OFF to verify that League/proactive replies still speak. With a running client, enter champion select, hover/lock and observe ally/enemy lock reactions. In a match, verify new events speak once, old events are silent on reconnect, and user questions outrank notifications. With headphones/MIC ON, interrupt a reply and confirm the new request replaces it; muting the microphone should let the current reply finish. Listen for natural clause boundaries and device underruns. These real-device/game checks remain manual.

## Files changed

| File | Change |
|---|---|
| `config.py` | Profiling, native chunks, CPU threads, recognition device, priority and optional interruption settings |
| `main.py` | Explicit model/synthesis shutdown and aggregate metrics output |
| `core/conversation.py` | Immutable persona-prefix cache and selected-model warm-up |
| `core/model_client.py` | Reusable cancellable transport, bounded callback delivery and server metadata |
| `core/streaming.py` | Early clauses, bounded queues, native continuations, context propagation and coalesced UI updates |
| `core/latency.py` (new) | Bounded numeric voice stages, CPU/wall spans and throughput |
| `core/inference_priority.py` (new) | Lightweight foreground/background admission policy |
| `core/league.py` | Separate detection/enqueue timestamps and opt-in voice timing |
| `core/league_voice.py` | Immutable announcement detection timestamp |
| `core/spatial.py` | Query whether a reply requires target confirmation |
| `gui/workers.py` | Warm-up, streaming callbacks, profiling, foreground reservations and optional interruptions |
| `gui/vision.py` | Background admission/preemption while live capture continues |
| `voice/voice.py` | VAD/STT stage timing and configurable recognition device |
| `voice/tts.py` | Native chunk ownership, cleanup and playback timing context |
| `voice/kokoro_worker.py` | Native callback WAVs, adjustable threads and optional process CPU timing |
| `voice/kokoro_session.py` | Bounded native messages, callback delivery, shutdown and profile results |
| `voice/playback.py` (new) | Reused device stream, ordered PCM, DAC timestamps and cancellation |
| `voice/capture.py` (new) | Optional latest-utterance capture during speech |
| `benchmark_voice.py` (new) | Silent fixtures, real TTS/Ollama, paced speech replay, contention and optional output |
| `benchmark_league.py` | Additional wall/CPU reporting with the existing polling comparisons |
| `tests/conftest.py` | Isolated scheduler/transport state and explicit native-feature opt-ins |
| `tests/test_tts.py` | Updated device cancellation coverage without real speaker I/O |
| `tests/test_voice_performance.py` (new) | Metrics, pool reuse, scheduling, streaming, interruptions, devices and shutdown regressions |
| `PERFORMANCE_REPORT.md` (new) | This report |

Numeric run artifacts are in `artifacts/performance/`: `before_voice.json`, `before_league.json`, `before_contention.json`, `before_stt.json`, `before_replay.json`, `tts_threads.json`, `tts_cpu_tradeoff.json`, `stt_devices.json`, `after_voice.json`, `after_league.json`, `after_replay.json` (intermediate), `after_profile.json`, `qwen32_voice.json`, and `final_replay.json` (final serial comparison). The temporary clean-code baseline was removed automatically; the empty fixture database remains under `artifacts/performance/fixture-data/`. Personal memory and voice-preview assets were not changed.

## Measurements still unavailable

No human microphone-to-acoustic-speaker loopback was recorded. Driver/DAC timing logic was tested with a fake device; real speech synthesis was verified without playing it. There was no active League match or LCU lockfile, so live League notification-to-speaker latency, in-game frame rate and game-under-load GPU contention were not measured. Noise/accent recognition quality and subjective speaker continuity require listening tests. Very long 32B generation, vision model loads, native chunk granularity and the preserved VAD endpoint remain material latency limits.
