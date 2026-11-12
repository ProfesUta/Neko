# Capri Phase 3: Kokoro first-audio latency

Measured on November 12, 2026, Windows, Python 3.11.6, 16 logical CPUs (Intel64 Family 6 Model 165), RTX 3060 12 GiB. The native engine is sherpa-onnx 2.0.1 with its bundled ONNX Runtime 1.28.2, CPU provider. The separately installed Python ONNX Runtime 1.31.1 reports Azure/CPU providers; that list does **not** establish GPU support in sherpa's native runtime. No runtime, dependency, model, voice asset, precision, pronunciation dictionary, personality, or sprite was replaced.

The selected setting is **eight synthesis threads with dynamic work blocks and disabled thread spinning**. Speaker 53, base speed 1.1, style handling and native sentence streaming remain unchanged. Six-second background inference admission, Phase 2 cancellation, Whisper settings, League docking and SVG selection remain intact.

## Results and measurement boundaries

These are real installed CPU/GPU engine measurements on fixed fixtures, not simulated synthesis. The primary TTS comparison has one unmeasured utterance per fixture/setting followed by 20 measured warm samples. Baseline is four threads/plain CPU; optimized is eight threads with `voice/kokoro_cpu.conf`. Cohorts ran serially on the same machine. They are consecutive cohorts, not randomized trials, and operating-system load can influence them.

“First usable audio” means the native callback's completed PCM WAV is delivered to Python. It is **not** physical speaker output. All times below are milliseconds. The project's bounded-sample percentile convention makes p95 and p99 equal at n=20; maximum is shown separately. These small cohorts cannot establish production tail guarantees.

| Fixture | Before p50 / p95 / p99 / max | After p50 / p95 / p99 / max | p50 reduction | n before / after |
|---|---:|---:|---:|---:|
| Short greeting | 565.99 / 596.03 / 596.03 / 603.27 | 382.65 / 388.16 / 388.16 / 392.80 | 32.39% | 20 / 20 |
| Medium explanation | 1185.88 / 1245.69 / 1245.69 / 1257.10 | 788.11 / 845.38 / 845.38 / 860.01 | 33.54% | 20 / 20 |
| Long, three sentences | 1169.93 / 1234.92 / 1234.92 / 1236.60 | 785.68 / 846.77 / 846.77 / 873.56 | 32.84% | 20 / 20 |
| Deterministic League announcement | 886.67 / 939.68 / 939.68 / 942.15 | 636.28 / 671.24 / 671.24 / 677.93 | 28.24% | 20 / 20 |
| Emotional reply | 653.71 / 678.75 / 678.75 / 679.62 | 446.91 / 465.85 / 465.85 / 470.06 | 31.64% | 20 / 20 |
| Champion-name pronunciation fixture | 1217.62 / 1275.70 / 1275.70 / 1290.91 | 856.05 / 870.32 / 870.32 / 878.05 | 29.70% | 20 / 20 |

Raw results: [phase3_final_tts.json](artifacts/performance/phase3_final_tts.json). Fixture text is in `benchmark_tts.py`. Total generation of the long fixture fell from 3000.02 to 2017.13 ms median; playback duration is determined by the voice and was not accelerated.

| First-audio variance, ms squared | Before | After |
|---|---:|---:|
| Short | 3958.48 | 206.87 |
| Medium | 5112.74 | 675.68 |
| Long | 6685.45 | 802.10 |
| League | 2741.70 | 301.62 |
| Emotional | 389.91 | 77.73 |
| Names | 1665.09 | 174.02 |

### Full voice pipeline

The existing Phase 2 replay harness used the same question, VAD settings, model selection, seed and empty fixture memory. Input speech was independently synthesized from that same question in each cohort, rather than replaying a bit-identical human recording. These runs exercise paced VAD, real CPU Whisper, real Ollama and real Kokoro, with no microphone or speaker output. Existing `auto` selects `qwen3:8b` on this installation; `qwen2.5:32b` remains the primary configured model and was explicitly exercised in the separate load tests below. No automatic model-selection behavior changed.

| Stage | Before p50 / p95 | After p50 / p95 | p50 reduction | n / type |
|---|---:|---:|---:|---|
| Speech-end estimate to first audio ready | 3302.04 / 3434.16 | 2744.89 / 3017.54 | 16.87% | 20 each, real engines with paced generated speech |
| TTS submission to first audio ready | 1596.55 / 1662.95 | 1080.22 / 1177.15 | 32.34% | 20 each, first response chunk |
| Speech end to VAD | 587.10 / 589.04 | 587.65 / 588.97 | No meaningful change | 20 each |
| Whisper transcription | 568.63 / 593.02 | 570.93 / 601.23 | No improvement | 20 each |
| Model dispatch to first text | 325.19 / 370.67 | 238.70 / 361.36 | Observed variation, not an LLM optimization | 20 each |
| Human speech end to physical audible response | Unavailable | Unavailable | Unavailable | 0 |

End-to-end p99 was 3434.16 before and 3017.54 after; **maximum worsened** from 3981.61 to 4420.51 ms. A slow initial model request remained possible (after TTFT max 1020.54 ms). Stage percentiles overlap and must not be added. The 17% replay result is not entirely attributable to TTS, because model timing also varied. The isolated matched TTS fixtures provide the stronger evidence for the CPU setting.

The earlier Phase 2 report's approximately 2468 ms replay and 946 ms TTS measurements are historical, different cohorts. They were not used as the “before” values in this report. Raw current cohorts: [before](artifacts/performance/phase3_before.json), [after](artifacts/performance/phase3_after.json).

## Where Kokoro spends time

Instrumentation uses `perf_counter_ns()` with cross-process monotonic timestamps. It is opt-in through the existing `CAPRI_LATENCY_METRICS=1` setting. No raw speech, transcripts or game information is added to application telemetry. The native worker does not read profiling clocks when profiling is disabled.

Medium fixture, 20 warm samples per setting:

| Stage | Before p50 / p95 | After p50 / p95 |
|---|---:|---:|
| Text cleaning | 0.039 / 0.041 | 0.030 / 0.087 |
| Pronunciation preprocessing | 0.014 / 0.015 | 0.011 / 0.028 |
| Native request dispatch to `generate()` entry | 0.588 / 0.618 | 0.511 / 1.326 |
| Native `generate()` entry to first audio callback | 1171.57 / 1233.50 | 767.34 / 837.65 |
| Callback validation and float-to-PCM16 conversion | 0.207 / 0.231 | 0.165 / 0.187 |
| PCM ready to WAV closed | 14.144 / 14.789 | 14.180 / 15.241 |
| WAV closed to decoded IPC message in parent | 0.120 / 0.128 | 0.107 / 0.271 |
| Parent reader queue to chunk handling | 0.049 / 0.062 | 0.065 / 0.196 |

`generate()` entry includes the C++ text frontend; it is not mislabeled as the first neural kernel. A separate two-generation ONNX Runtime profiling diagnostic recorded model runs of **719.885 and 711.623 ms**, versus native generation wall times of 724.295 and 714.925 ms. It confirms neural execution dominates this fixture. Convolution and sine operators were the largest aggregate operator categories. That diagnostic includes profiler overhead and is not a 20-sample benchmark or directly aligned kernel timestamp on the Python clock. See [operator summary](artifacts/performance/phase3_operator_summary.json).

The approximately 14 ms WAV stage is roughly 1–2% of these first-audio times; pipe delivery and PCM conversion are much smaller. Replacing it with binary IPC/shared memory would add cancellation and ownership complexity for a small maximum gain. **WAV transport, sample format, native callbacks, bounded message queues and ownership cleanup were retained.**

## CPU settings and load

Plain CPU thread sweep, identical warm medium fixture, 20 samples each:

| Threads | First audio p50 / p95 / p99 / max |
|---:|---:|
| 1 | 2022.35 / 2039.54 / 2039.54 / 2145.70 |
| 2 | 1891.03 / 2026.51 / 2026.51 / 2029.56 |
| 4 | 1169.85 / 1232.82 / 1232.82 / 1236.33 |
| 6 | 950.03 / 984.24 / 984.24 / 989.76 |
| 8 | 932.62 / 963.20 / 963.20 / 981.83 |

The same sweep also measured short, long and League fixtures: [thread results](artifacts/performance/phase3_threads.json). Exploratory four-thread runs found dynamic blocks alone gave no meaningful improvement; disabling spin reduced CPU time, and combining both was stable. Eight threads with those options outperformed six in the subsequent controlled idle and active-generation tests.

The options file contains only:

```ini
SessionConfig.session.dynamic_block_base=4
SessionConfig.session.intra_op.allow_spinning=0
SessionConfig.session.inter_op.allow_spinning=0
```

These configure scheduling, not precision or model weights. The installed sherpa runtime supports CPU options files and forwarding `SessionConfig` entries. Older/unrecognized runtimes keep plain CPU; missing files warn once, and a recoverable tuned-engine initialization error retries plain CPU. References: [sherpa 2.0.1 session implementation](https://raw.githubusercontent.com/k2-fsa/sherpa-onnx/v2.0.1/sherpa-onnx/csrc/session.cc), [ONNX Runtime threading](https://onnxruntime.ai/docs/performance/tune-performance/threading.html).

Controlled real-model load, medium fixture, 20 measured samples per row. Every sample began while the named model was actually decoding:

| Active load | TTS setting | First audio p50 / p95 / p99 / max |
|---|---|---:|
| Qwen2.5:32B | Plain 4 | 1463.40 / 1510.75 / 1510.75 / 1515.59 |
| Qwen2.5:32B | Tuned 6 | 1206.71 / 1253.01 / 1253.01 / 1257.62 |
| Qwen2.5:32B | Tuned 8 | 1100.58 / 1203.35 / 1203.35 / 1297.22 |
| Qwen2.5VL:7B vision | Plain 4 | 1239.31 / 1262.63 / 1262.63 / 1265.06 |
| Qwen2.5VL:7B vision | Tuned 6 | 926.08 / 942.30 / 942.30 / 955.28 |
| Qwen2.5VL:7B vision | Tuned 8 | 820.86 / 836.69 / 836.69 / 838.50 |

Raw: [Qwen load](artifacts/performance/phase3_qwen_active.json), [vision load](artifacts/performance/phase3_vision_active.json). The stress harness deliberately bypasses Capri's admission gate using generated imagery, not private screen capture. No active League match or game frame-rate measurement was available.

A separate [dual-request thread sweep](artifacts/performance/phase3_stress_threads.json) issued both models concurrently, but Ollama queued/swapped them on the 12 GiB card. Active decode coverage differed across settings, so it is **not** evidence of simultaneously resident GPU inference or a fair ranking under identical load. No GPU runtime was installed or enabled.

Eight tuned threads use more cores briefly: medium native synthesis averaged approximately **5.02 core equivalents (31.4% of this 16-thread host)** versus 3.97 (24.8%) before. Median native CPU time per medium utterance fell from 4.703 to 3.953 CPU-seconds, and wall time from 1185.70 to 787.50 ms. Long utterance CPU time fell from 11.969 to 10.313 CPU-seconds. These are process CPU-time/wall ratios, not a system-wide utilization trace or proof of unaffected game FPS.

Model-process RSS after the medium fixture was 598.3 MiB before and 598.9 MiB after; after the name fixture 601.4 versus 602.5 MiB. Early sweep RSS values described the Windows venv launcher stub and are excluded; the harness now selects the actual native model process. The configured native thread pool increased; no extra Python application worker, service or timer was added.

Cold worker readiness was 1662.56 versus 1653.77 ms, one startup per cohort. First short generation immediately afterward was 507.38 versus 403.70 ms. These n=1 cold observations are not percentile distributions; startup preloading remains important.

## Text delivery and playback

The existing `StreamingReply` can now submit a complete initial sentence at 20 characters instead of waiting for the shared 32-character minimum. Later sentences and comma/clause thresholds retain their old limits. Added guards avoid new early boundaries inside abbreviations and initials. It does not split words, remove text or alter delivery styles. `TTS_FIRST_SENTENCE_MIN_CHARS=0` restores the previous behavior.

Using synthetic 40 ms/word delivery with **real Kokoro and the production chunk pipeline**, the emotional fixture's feed-start-to-first-audio median changed from **793.94 to 603.77 ms**, p95/p99 from 819.43 to 616.79 ms, max from 821.33 to 617.15 ms, n=20 each. This is a hybrid dispatch experiment, not measured Ollama token timing or human response latency. Saved A/B WAVs and text-order tests cover the complete reply. Kokoro's existing one-sentence native batch setting was retained; the Python API exposes no independently validated lower-latency sub-sentence neural callback setting here.

Playback now queues its first PCM job **before** opening/starting PortAudio, fixing the race on backends that invoke callbacks synchronously during startup. A regression test exercises that backend behavior. On this machine's default MME driver, the old code did not hit that race, and the real silent-driver comparison showed **no meaningful latency improvement**:

| Real silent output measurement | Before p50 / p95 | After p50 / p95 | n |
|---|---:|---:|---:|
| PCM call to first filled callback buffer | 52.87 / 53.37 | 52.72 / 53.32 | 20 each |
| PCM call to first-buffer DAC proxy | 143.05 / 143.52 | 143.05 / 143.59 | 20 each |

The buffer sweep tested host-selected, 5, 10 and 20 ms blocks, 20 trials each. Opening the default device took approximately 12 ms median; all silent cases recorded zero underflows. The existing **10 ms** production block remains: the smaller-buffer evidence used silence, not speech under game load. A priming experiment submitted buffers at 12.06 ms median, but all 20 initial callbacks lacked usable DAC clocks; **no presentation improvement is claimed and priming was not enabled**. [PortAudio documents priming as initial buffer filling](https://portaudio.com/docs/proposals/020-AllowCallbackToPrimeStream.html), which is distinct from audible playback.

Optional metrics distinguish PCM decoding, playback queue entry, stream opening/start, actual output callbacks, underflows, inter-chunk empty frames, non-silent buffer submission and PortAudio's first non-silent DAC estimate. The audio callback only updates small numeric counters/a bounded deque; aggregation, locks in the metrics collector and reporting stay off that callback. First non-silent physical playback and acoustic clicks were **not measured** in these silent runs. See [buffer results](artifacts/performance/phase3_playback.json) and [checked priming experiment](artifacts/performance/phase3_priming_checked.json).

## Speech quality, interruption and compatibility

Saved A/B speech retains the same rate, voice, dictionary and text. Signal checks found no clipping or non-finite samples. Five of six CPU-comparison fixtures produced identical normalized Whisper words. The name fixture differed in ASR's spelling of Jarvan; the earlier-sentence experiment differed in ASR's spelling of Yuumi. Both variants retained the same recognized word counts. The long fixture produced 21 ASR words against 23 reference words in both settings, so ASR is explicitly **not proof of perfect pronunciation or absence of omissions**. Timing/duration checks and automated text/audio ordering tests add evidence, but cannot replace listening for rhythm, identity, clicks or name pronunciation.

No human listening or acoustic validation is claimed. Listen to the saved baseline and optimized WAVs before judging subjective quality: `artifacts/performance/phase3-quality-final/control`, `8`, `dispatch-0` and `dispatch-20`. The CPU comparison changes scheduling only; small stochastic waveform differences existed even between repeated baseline generations. [Signal/ASR comparison](artifacts/performance/phase3_quality.json), [sentence-boundary comparison](artifacts/performance/phase3_dispatch_quality.json).

Real cancellation, 20 samples each, no speaker output:

| Cancellation case | p50 / p95 / p99 / max, ms | Resident workers reused |
|---|---:|---:|
| Final-chunk cancellation to generator released | 1.46 / 3.92 / 3.92 / 5.09 | 20 / 20 |
| Final-chunk cancellation to next worker ready | 3.26 / 9.55 / 9.55 / 10.02 | 20 / 20 |
| Ongoing generation to generator released | 2.72 / 6.07 / 6.07 / 6.86 | 0 / 20, intentional termination |
| Ongoing generation to next worker ready | 1639.22 / 1706.59 / 1706.59 / 1764.08 | Reload required |

The Phase 2 completion-ack drain remains bounded at 50 ms and is still restricted to actual final chunks. Its historical 2.70 ms median ready time is not replaced by a claim that this cohort is faster. Ongoing synthesis still stops immediately instead of waiting for the network or full utterance. Physical sound cessation remains unmeasured. [Cancellation results](artifacts/performance/phase3_cancellation.json).

## Files and configuration

Phase 3 changes, relative to the preserved Phase 2 source:

| File | Purpose |
|---|---|
| `config.py` | Measured eight-thread default, CPU-options path, initial complete-sentence threshold |
| `voice/kokoro_cpu.conf` (new) | Three scheduling settings; no model/math changes |
| `voice/kokoro_worker.py` | Optional CPU settings with plain-CPU fallback; opt-in native stage timestamps and CPU time |
| `voice/kokoro_session.py` | Include options in resident-worker signature; collect cross-process stage timing; retain cancellation protocol |
| `voice/tts.py` | Time existing cleaning and pronunciation operations |
| `voice/playback.py` | Queue-before-start fix; opt-in stream/callback/gap diagnostics |
| `core/latency.py` | Bounded numeric counters and stage relationships |
| `core/streaming.py` | Earlier complete first sentence with abbreviation guards |
| `gui/workers.py` | Pass the threshold to the existing speech pipeline; no League changes |
| `benchmark_tts.py` (new) | CPU sweeps, matched controls, load gating, saved A/B audio, silent driver probes, optional playback/loopback cue |
| `tests/test_tts_phase3.py` (new) | Native timing, disabled-profile overhead, CPU fallbacks, PCM ordering/startup, natural boundaries and benchmark regressions |
| `PHASE3_TTS_PERFORMANCE_REPORT.md` (new) | Measurements, limitations and reproduction instructions |
| `PERFORMANCE_REPORT.md` | Link the current TTS report while retaining the earlier results |

No new dependencies. Other pre-existing Phase 2 working-tree changes were preserved.

Defaults:

```powershell
# Measured defaults; set before launching Capri or a benchmark.
$env:CAPRI_TTS_THREADS = '8'
$env:CAPRI_TTS_CPU_OPTIONS = 'F:\Neko\voice\kokoro_cpu.conf'
```

Optional rollback on a computer that performs better with the original settings:

```powershell
# Original CPU scheduling when needed on another computer.
$env:CAPRI_TTS_THREADS = '4'
$env:CAPRI_TTS_CPU_OPTIONS = 'none'
```

The first-sentence threshold is `config.TTS_FIRST_SENTENCE_MIN_CHARS = 20`; set it to `0` to restore the old threshold. Keep the existing `TTS_AUDIO_BLOCK_MS = 10`. Tracing stays disabled by default; enable `CAPRI_LATENCY_METRICS=1` to report numeric production timings at shutdown.

## Local validation commands

The complete suite passed: **654 passed in 77.14 seconds**, using `.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider`. Tests require no League match, Ollama or audio hardware. They cover existing streaming ordering, cancellation, bounded queues, stale suppression, Phase 2 microphone/playback races, plus the new timing/CPU fallback/startup cases. League Companion activation, docking, SVG rendering/switching, movement locking, recovery and topology tests passed. `git diff --check` also passed; Git only reported existing LF/CRLF conversion notices.

An additional offscreen entrypoint check constructed the real main window, processed GUI events, closed it and returned exit code 0. Startup preloads, League networking and screen capture were disabled for that check; real engine loading was exercised separately by the benchmarks. This does not claim a full interactive game-session or physical-display launch test.

From PowerShell in `F:\Neko`, with the existing virtual environment and installed models:

```powershell
$env:CAPRI_TTS_THREADS = '8'
$env:CAPRI_TTS_CPU_OPTIONS = 'F:\Neko\voice\kokoro_cpu.conf'
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider

# Match the isolated before/after TTS comparison; silent, 20 warm samples each.
.\.venv\Scripts\python.exe -B benchmark_tts.py --samples 20 --threads 8 --plain-control --fixtures short medium long league emotional names --cpu-options voice/kokoro_cpu.conf --save-audio artifacts/performance/phase3-local-audio --dispatch --output artifacts/performance/phase3-local-tts.json

# Original CPU thread sweep, then the tuned sweep.
.\.venv\Scripts\python.exe -B benchmark_tts.py --samples 20 --threads 1 2 4 6 8 --plain-cpu --fixtures short medium long league --output artifacts/performance/phase3-local-plain.json
.\.venv\Scripts\python.exe -B benchmark_tts.py --samples 20 --threads 1 2 4 6 8 --fixtures medium --output artifacts/performance/phase3-local-tuned.json

# Real model load; run these separately, not simultaneously with other tests.
.\.venv\Scripts\python.exe -B benchmark_tts.py --samples 20 --threads 6 8 --plain-control --fixtures medium --stress --stress-models qwen2.5:32b --output artifacts/performance/phase3-local-qwen.json
.\.venv\Scripts\python.exe -B benchmark_tts.py --samples 20 --threads 6 8 --plain-control --fixtures medium --stress --stress-models qwen2.5vl:7b --output artifacts/performance/phase3-local-vision.json

# Same end-to-end replay and cancellation probes used in this report.
.\.venv\Scripts\python.exe -B benchmark_voice.py --samples 20 --tts --replay --profile --output artifacts/performance/phase3-local-replay.json
.\.venv\Scripts\python.exe -B benchmark_voice.py --samples 20 --interrupt --output artifacts/performance/phase3-local-cancel.json

# Existing optional UI scenarios: real engines, mocked League API, no microphone.
.\.venv\Scripts\python.exe -B benchmark_voice.py --samples 20 --replay --league-fixture --companion --profile --output artifacts/performance/phase3-local-companion.json
.\.venv\Scripts\python.exe -B benchmark_voice.py --samples 20 --replay --league-fixture --desktop-ui --profile --output artifacts/performance/phase3-local-desktop.json

# Silent real audio-device probe; add --audio only to explicitly play speech.
.\.venv\Scripts\python.exe -B benchmark_tts.py --samples 20 --fixtures short --silent-playback --output artifacts/performance/phase3-local-driver.json
.\.venv\Scripts\python.exe -B benchmark_tts.py --quality-compare artifacts/performance/phase3-local-audio/control artifacts/performance/phase3-local-audio/8 --output artifacts/performance/phase3-local-quality.json

# Normal app, optional production profiling. Close normally to print metrics.
$env:CAPRI_LATENCY_METRICS = '1'
.\.venv\Scripts\python.exe -B main.py
Remove-Item Env:CAPRI_LATENCY_METRICS
```

Use `--model qwen2.5:32b` with `benchmark_voice.py` to explicitly measure that conversational model; the report's auto replay did not silently replace it. Use `--recording path\question.wav` to compare exactly the same recorded PCM across settings. `--consecutive` exercises retained context. Run benchmarks serially and keep speaker/speed/options consistent.

### Optional Windows output/loopback validation

1. In Audacity choose Windows WASAPI and the playback device's loopback recording input. Disable input monitoring, and start recording before the test. See [Audacity's desktop recording instructions](https://support.audacityteam.org/basics/recording-desktop-audio).
2. Run `.\.venv\Scripts\python.exe -B benchmark_tts.py --samples 1 --fixtures medium --audio --loopback-marker --output artifacts/performance/phase3-loopback.json`. This explicitly plays speech and a short 1 kHz reference cue. Skip the first warm-up cue/reply when measuring.
3. Measure from the measured trial's cue onset to speech onset in the loopback waveform. Subtract that trial's JSON `reference_dac_to_text_request_ms` to estimate text-request-to-recorded-output latency. The cue alignment itself uses a DAC estimate and is not perfect clock calibration.
4. WASAPI loopback observes the digital output mix, **not sound arriving at your ears**. For acoustic validation record the cue and speaker with an external microphone/recorder instead. Check inter-chunk gaps, clicks, names, emotional rhythm and interruption/recovery while listening. Do not enable microphone monitoring near speakers.

The existing `benchmark_voice.py --samples 20 --interrupt --audio` additionally measures driver abort return and recovery. That is not acoustic silence detection. No acoustic loopback, human listening comparison, actual League match/FPS test, or simultaneous resident Qwen32/vision GPU measurement was performed here.

## Next bottleneck

Native neural generation remains about 767 ms median for the medium fixture, versus approximately 14 ms WAV delivery, so changing transport is low priority. Next, measure shorter **natural complete** phrases on real conversations and perform the listening/loopback checks above. Endpointing plus Whisper still contributes roughly 1.16 seconds in replay; representative recorded pauses are required before reducing the 500 ms silence threshold. Background model swaps can still dominate occasional turns, and the existing six-second admission policy should remain in place.
