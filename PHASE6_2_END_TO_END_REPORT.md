# Phase 6.2 — controlled League vision-to-voice benchmark

14 November 2026. The benchmark uses the existing OpenCV detector, temporal confirmation, `VisualAlertDispatcher`, one-slot `LeagueVoicePolicy`, `AdviceWorker`, persistent Kokoro worker and shared-mode WASAPI playback. It does not start League services or enable competitive alerts. Production confirmation remains `strict3`. No voice, model, asset, movement, docking or League API setting was changed.

## Evidence and eligibility

The local recording is 1920 × 1080, 60 FPS, 253.867 seconds. The benchmark releases the configured 10 Hz analysis samples at their original source-time spacing, rather than processing the clip as fast as possible. Short clips are predecoded into bounded RAM; file decoding is measured separately. Mode A's frame acquisition is an in-memory crop read, **not Windows capture**. Source audio is never played. No screenshots, loopback audio or conversations are saved by default.

The existing calibration has no independently confirmed named lane region or independently verified Yuumi position. Therefore the recording has **zero genuinely eligible voice notifications**. Attached Caitlyn/Yuumi artwork is not used to invent player distance. This run cannot establish genuine sighting-to-spoken-warning latency or real spoken-alert precision/recall.

A separate synthetic fixture places the cached Warwick icon into a blank minimap and explicitly selects only that test champion for synthetic eligibility. Its fixed speech is “Benchmark notification. Warwick visible.” This exercises real detection, notification policy, Kokoro and the output device, **not genuine gameplay eligibility or detection accuracy**. Repeating it 20 times supplies latency samples, not 20 independent detection examples. Synthetic eligibility exists only in this explicitly launched benchmark process; the live worker has no route to it.

## Timing methodology

All runtime timestamps use `perf_counter_ns()`. Original video timestamps are retained separately. No video timestamp is directly subtracted from a runtime timestamp.

| Field | Measurement |
|---|---|
| T0 | Actual software release of the sampled, known-onset source frame in Mode A; not physical screen presentation |
| T1 / T2 | Source acquisition start / frame available; Mode B uses the existing physical-DPI MSS capture |
| T3 / T4 | Detector start / successful identity-validated raw match |
| T5 | Temporal confirmation |
| T6 / T7 | Proposal / actual one-slot policy acceptance |
| T8 | Existing speech path dispatches synthesis |
| T9 | First playable synthesized result available to this speech consumer |
| T10 | PortAudio callback processes first non-silent PCM; not sound at the listener |
| T11 | First above-threshold Windows render-mix sample observed by passive WASAPI loopback, using its QPC timestamp |
| `dac_ns` | PortAudio's estimated presentation time, reported separately from loopback |

Auxiliary numeric marks include native PCM readiness, native callback, playback queueing, output opening, cancellation and worker completion where available. A generated native callback is not necessarily a playable result delivered to `AdviceWorker`: that route currently uses full-utterance `speak()`. Existing native streaming and conversational TTS are unchanged.

The source video's audio stays silent. Other applications' audio was paused by the operator before the audio trials. Loopback requires a quiet mix and is accepted only after that notification has both synthesized PCM and a non-silent output callback. This temporal attribution does not identify arbitrary simultaneous sounds; contaminated or unmatched samples are missing/error results. Nothing here measures physical acoustic output.

`loopback_received_ns` records host packet receipt separately from the WASAPI/QPC sample timestamp used for T11. Buffered render-mix samples can be received before their timestamped presentation. Neither timestamp is a calibrated measurement of the speaker or listener.

Percentiles follow the project's floor-order convention; with 20 samples p95 and p99 can coincide. Missing samples and maxima are retained. The JSON includes variance. Trial state, epochs and cooldowns reset between repetitions; strict/adaptive execution order alternates, with one unmeasured warm-up per policy.

## Real-engine audio results

20 warm paired trials, one fixed synthetic sighting and fixed test phrase, real Kokoro and Windows output. All 40 measured trials reached one output callback and one attributable loopback onset.

| Milliseconds, p50 / p95 / p99 / maximum | Strict3 (n=20) | Adaptive (n=20) |
|---|---:|---:|
| Sampled appearance → confirmation | 218.7 / 225.6 / 225.6 / 240.4 | 110.6 / 131.6 / 131.6 / 133.1 |
| Confirmation → digital mix onset | 930.7 / 967.1 / 967.1 / 1039.0 | 934.6 / 989.4 / 989.4 / 992.9 |
| Acquired frame → digital mix onset | 1149.4 / 1191.2 / 1191.2 / 1258.2 | 1052.8 / 1104.8 / 1104.8 / 1111.1 |
| Sampled appearance → digital mix onset | 1149.4 / 1191.2 / 1191.2 / 1258.2 | 1052.8 / 1104.8 / 1104.8 / 1111.1 |
| Confirmation → DAC estimate | 911.3 / 948.3 / 948.3 / 1018.4 | 916.0 / 966.2 / 966.2 / 972.9 |
| TTS dispatch → playable PCM | 836.7 / 871.6 / 871.6 / 945.6 | 836.4 / 890.6 / 890.6 / 891.1 |

Artifacts: `artifacts/phase6_2/paired-audio/trials.csv` and `report.json`. This comparison changes only the already-existing confirmation policy, not production behavior. The roughly 97 ms difference between marginal end-to-end medians is specific to this strong synthetic icon. The median of paired strict-minus-adaptive differences is 100.3 ms, with a fixed-seed 2,000-resample paired bootstrap 95% interval of 80.2–109.4 ms (existing `benchmark_playback.paired_interval`). This characterizes repeatability of this fixture, not general detection accuracy. It does not justify globally changing confirmation on ambiguous real sightings. No before/after production TTS speed improvement is claimed.

Stage breakdown for those same 20 warm samples per policy (milliseconds; p50 / p95 / p99 / maximum):

| Stage | Strict3 | Adaptive |
|---|---:|---:|
| T1 → T2, in-memory frame acquisition | .001 / .001 / .001 / .017 | <.001 / .001 / .001 / .002 |
| T2 → T3, detector dispatch | .001 / .001 / .001 / .002 | .001 / .001 / .001 / .001 |
| T3 → T4, first raw identity match | 16.730 / 21.394 / 21.394 / 28.076 | 16.569 / 19.574 / 19.574 / 20.115 |
| T4 → T5, subsequent confirmation | 201.045 / 207.323 / 207.323 / 224.073 | 93.823 / 115.320 / 115.320 / 116.172 |
| T5 → T6, proposal | .137 / .178 / .178 / .192 | .082 / .243 / .243 / .326 |
| T6 → T7, policy acceptance | .023 / .036 / .036 / .036 | .024 / .042 / .042 / .043 |
| T7 → T8, speech dispatch | .980 / 1.216 / 1.216 / 1.278 | 5.899 / 8.775 / 8.775 / 9.377 |
| T8 → T9, synthesis/result delivery | 836.651 / 871.565 / 871.565 / 945.633 | 836.355 / 890.584 / 890.584 / 891.054 |
| T9 → T10, PCM conversion/queue/device callback | 54.833 / 57.703 / 57.703 / 70.621 | 54.685 / 57.422 / 57.422 / 60.218 |
| T10 → T11, callback to timestamped digital mix | 37.871 / 41.325 / 41.325 / 41.661 | 37.797 / 41.254 / 41.254 / 41.697 |

Marginal percentiles do not add to end-to-end percentiles. T0 equals source acquisition release in this synthetic fixture; the sub-microsecond rows are software bookkeeping, not screen capture performance.

## Paced real-recording trials

20 warm pairs **for each** of four clips, plus an excluded warm-up per policy: 160 measured trials. Both production live flags remained false. Every trial had zero proposals/acceptances/voice starts under genuine eligibility checks. The absent digital latencies are missing observations, not zero milliseconds. Candidate observations below are not independent confirmation of champion identity.

| Known sampled source appearance → confirmation | Strict p50 / p95 / p99 / max ms (n=20) | Adaptive (n=20) |
|---|---:|---:|
| Warwick, video 214.4 s | 218.7 / 225.9 / 225.9 / 238.7 | 215.1 / 232.3 / 232.3 / 236.7 |
| Brief Galio, video 5.2 s | 325.4 / 330.2 / 330.2 / 330.8 | 218.3 / 232.8 / 232.8 / 235.6 |
| Two-enemy clip, Karma onset 8.4 s | 217.9 / 226.8 / 226.8 / 233.7 | 213.4 / 221.6 / 221.6 / 226.8 |
| Darius/Karma overlap clip | No known non-censored Darius onset; no appearance latency reported | Same limitation |

Adaptive confirmation did not accelerate every real sighting. The selected Warwick appearance retained the stricter evidence path. No threshold or reference was changed during this task. Existing assistant-reviewed accuracy from Phase 6.1 is evidence of that earlier evaluation, not newly independent accuracy validation.

Environment: Intel i7-10700F at 2.90 GHz, 16 logical processors, NVIDIA GeForce RTX 3060, Windows build 26200, Python 3.11.6, OpenCV 5.0.0, NumPy 2.4.6. Kokoro speaker 53, speed 1.1, eight configured synthesis threads. Sounddevice resolved HD Audio speakers to WASAPI endpoint index 22; the passive system mix was 48 kHz stereo float32. Device indices can change locally.

During the paced real-clip trials, median process-tree CPU was **21.4% of one core** for strict and **20.3% of one core** for adaptive (about 1.3% of this 16-logical-processor capacity). Sampled peak aggregate process-tree RSS was **632.3 MiB**, including the warm native voice process. There were **zero overdue source-frame drops**. Maximum per-trial detector p95 was 33.2 ms strict / 42.1 ms adaptive; these are maxima of trial percentiles, not pooled global p95. File decode and initial template/model loading are outside these per-frame timings.

The finite 1 Hz NVIDIA sampler collected 510 device-wide samples, peak utilization 25%, peak memory used 1570 MiB. These values include other desktop work and cannot be attributed to Neko. They do not establish live-game FPS impact or model residency. There is no GPU detector. No contention comparison is claimed because Ollama was unavailable. Artifacts: `artifacts/phase6_2/paired-recorded/{trials.csv,report.json,gpu.csv}`.

### New scoped accuracy check

The new paced benchmark predictions were scored once per unique source timestamp with the existing evaluator and existing labels. Replays were not multiplied into the accuracy denominator. These clips cover **less source time** than the Phase 6.1 full audit and reset temporal state at clip boundaries. The figures must not be presented as improvements/regressions against its full-recording results.

| Existing review scope intersected with these clips | Strict TP / FP / FN; precision / recall | Adaptive |
|---|---|---|
| Primary, 54 frames / 5.4 sampled seconds / 84 champion-positive frames | 48 / 0 / 36; 100% / 57.1% | 49 / 0 / 35; 100% / 58.3% |
| Warwick only, 36 frames / 3.6 seconds / 17 positive frames | 15 / 0 / 2; 100% / 88.2% | Same |

Darius remains missed in all 12 labelled positive frames. Karma is detected in 3/10, Ziggs 10/12, Galio 35/50 strict and 36/50 adaptive. Unknown/excluded overlap regions remain excluded. Both small audits have zero observed false candidate episodes (0/min **within their own restricted sampled exposure**). There is no new independent human ground truth and no full-match recall or spoken-warning precision estimate. Genuine speech remained ineligible, so a zero genuine false-alert count is not evidence that warnings are accurate. Artifacts: `artifacts/phase6_2/scoped-accuracy/accuracy-{strict3,adaptive}/`.

## Real-audio lifecycle validation

The corrected warm safety run executed **20 paired trials per case** (40 measured trials each), plus excluded warm-ups. It used actual detector pixels, dispatcher, one-slot policy, `AdviceWorker`, Kokoro and WASAPI. Eligibility and user interruptions below are explicitly controlled fixtures, not live competitive events.

| Controlled case | Observed result across 40 measured trials |
|---|---|
| Enemy disappears before speech | 40 accepted notices cancelled; zero non-silent callbacks or digital onsets |
| New epoch while old notice is queued | 40 old notices cancelled; zero callbacks or digital onsets |
| Capri already speaking a user-priority fixture | 40 fixture callbacks and digital onsets; zero target output or overlap |
| Injected user interruption | 40 target callbacks/digital onsets, followed by 40 software playback aborts; no subsequent target announcement |
| Enemy continuously visible for 12 seconds | Exactly one proposal, acceptance, callback and digital onset per trial; zero duplicates beyond cooldown |

The interruption is injected at least 100 ms after estimated presentation begins; it does not measure microphone/VAD barge-in detection. Interruption marker → playback abort completion: strict **4.717 / 8.814 / 8.814 / 10.504 ms** p50/p95/p99/max (n=20); adaptive **4.180 / 9.406 / 9.406 / 10.333 ms** (n=20). This is software stream-abort completion, not acoustic silence.

All 120 actually output safety utterances (40 busy fixtures, 40 interrupted, 40 continuous) had attributable digital onsets. The quiet-output guard rejected 40 never-played sighting attempts after busy speech; they were dropped and have no output timestamps. The preceding fixture's render tail was not misattributed to a new notice. There were zero source-frame drops in these measured safety trials.

Across 5,000 synthetic analyzed frames per policy in the safety run, pooled detector processing p50/p95/p99/max was **16.69 / 25.12 / 35.34 / 53.72 ms** strict and **16.68 / 25.10 / 35.72 / 51.80 ms** adaptive. These simple synthetic pixels are easier than real minimaps. Median per-trial process-tree CPU was 56.2% / 57.0% of one core including synthesis; sampled peak aggregate RSS was 747.3 MiB. Global NVIDIA sampling recorded 1,249 samples, peak utilization 27%, peak memory 1587 MiB. These are controlled-replay costs, not live FPS measurements. Artifacts: `artifacts/phase6_2/paired-safety-warm/{trials.csv,report.json,gpu.csv}`.

## Captured-window and contention limitations

Mode B creates its own visibly labelled, silent offline replay window. Its adapter reuses `GameWindowCapture.open()` and MSS/Win32 DPI mapping, and requires this process's HWND, PID, nonce title, foreground status, visibility and unobscured regions. A captured frame marker must match before and after ROI acquisition. Arbitrary game windows cannot be substituted. The production game capture/approval gate is untouched.

The available Windows session returned **NULL for `GetForegroundWindow()`**, even after explicitly activating the benchmark's own window. The capture guard therefore rejected the trial. No capture-to-output or compositor appearance measurements are claimed from this session. Run Mode B in the interactive Windows desktop and keep its replay window foreground. T0 remains unavailable in Mode B: a Qt repaint/frame marker is not a synchronized DWM presentation timestamp.

The local Ollama endpoint was unavailable: no TCP listener on 11434, no Ollama process and no `ollama` executable on PATH were found in the final checks. The optional contention harness records this rather than pretending Qwen/vision ran. When available it uses the existing model client and foreground/background admission policy, fixed text and a generated image, with bounded cancellation. It does not send game frames to a vision model or force background inference past the six-second grace period. Admission intervals are recorded; simultaneous model residency is not assumed. Start the existing local Ollama installation before running the documented contention command; this benchmark does not install models or start a persistent server.

## Cancellation recovery finding

The first extended safety attempt exposed an existing limitation: `KokoroSession.request()` shuts down the native worker when synthesis is cancelled before generation completes. A subsequent short-lived visual notification can expire during the cold model load. Repeated expired requests can repeatedly cancel that load. This differs from cancellation of already generated audio, which retains the warm engine.

That diagnostic run was stopped and explicitly marked incomplete in `artifacts/phase6_2/paired-safety/report.json`. Its post-cancellation samples are **not counted as warm latency measurements**. The benchmark now invokes the existing readiness check before each warm trial: an alive worker is reused; a missing worker is reloaded. Readiness/reload wall time and PID changes are reported separately, outside the paced source interval. The 80 reloads before measured safety trials took **1834.4 / 1909.6 / 1943.6 / 1968.8 ms** p50/p95/p99/max. This check loads the engine without generating audio; first-inference effects after reload are not represented as steady-state synthesis timings. This warm-up exclusion must not be interpreted as instantaneous real-world recovery.

No production cancellation policy was changed or hidden by increasing notification lifetimes. The new check fixes benchmark validity, not the underlying application's cold-recovery cost. Future changes should benchmark safe draining/cooperative cancellation of unfinished native requests, stale-file/chunk cleanup and new-request priority before changing that policy. Preserve the already fast cancellation of completed synthesis.

## Changes

| File | Purpose |
|---|---|
| `benchmark_league_vision_e2e.py` | CLI, private calibration workflow, paced paired trials, device/environment metadata, optional scoped accuracy evaluation |
| `benchmarks/league_vision_e2e.py` | Existing detector/notification/worker orchestration, source-clock correlation, lifecycle cases, per-trial CSV/JSON, bounded source storage and resource accounting |
| `benchmarks/league_replay_window.py` | Own-process offline replay window and guarded existing-capture adapter |
| `benchmarks/league_e2e_workload.py` | Optional finite NVIDIA sampling and existing-client model load, shut down with the benchmark |
| `core/latency.py` | Thread-safe copy of numeric trace marks |
| `gui/workers.py` | Optional externally supplied trace on `AdviceWorker`; ordinary callers retain existing tracing behavior |
| `tests/test_league_vision_e2e.py` | Hardware-free safety, correlation, eligibility, real-policy/mocked-audio and cleanup regressions |
| `PHASE6_2_END_TO_END_REPORT.md` | Methods, measured results, limitations and reproducible Windows commands |

No new dependencies or normal-operation polling, capture, recording or inference workers. PNG/SVG rendering and League Companion Mode are unchanged. Tracing is enabled only inside the benchmark process. Private artifacts and calibration remain under already ignored directories; nothing is committed automatically.

## Automated verification

Final command: `.\.venv\Scripts\python.exe -B -m pytest -q` — **889 passed in 137.98 seconds**, no failures. The current suite was executed after the implementation, including 31 new parameterized test cases. Log: `artifacts/phase6_2/final-tests.log`. `git diff --check` passed. No tests or other CV benchmarks were run concurrently with the latency trials.

New coverage includes exact recording/calibration eligibility binding, malformed/unreviewed labels, attached-player uncertainty, synthetic eligibility scope, real template processing, missing source synchronization, loopback attribution, numeric trace concurrency, reuse of the existing speech worker, unique trial counting, cancellation before output, epoch changes, busy-user priority, interruption/abort, continuous-sighting suppression, native readiness/reload accounting, absent audio hardware and refusing foreign/game HWNDs. Audio is mocked in pytest; the separately documented CLI runs exercised the actual synthesizer, PortAudio and loopback. The tests do not require League, Ollama or a microphone.

The captured-window command was also executed against the final implementation and failed safely with the foreground-ownership guard; its incomplete diagnostic is saved under `artifacts/phase6_2/window-final/`. This is an environmental/manual validation requirement, not a passing capture measurement. No genuine eligibility, independent new human accuracy annotations, acoustic measurement or model-contention result was fabricated.

## Windows reproduction

Keep other output quiet; run performance jobs serially, apart from pytest. Each output directory must be fresh. `--audio` explicitly authorizes test speech. `--loopback` passively observes the render mix in RAM; it does not record the microphone, change volume or save captured audio.

```powershell
Set-Location F:\Neko
.\.venv\Scripts\python.exe -B benchmark_league_vision_e2e.py preflight --audio --loopback --output artifacts\phase6_2\preflight-local

# Real pixels, unmodified verified-eligibility checks; correctly silent without a confirmed manifest.
.\.venv\Scripts\python.exe -B benchmark_league_vision_e2e.py run --scenarios warwick,galio,overlap,two-enemies --trials 20 --warmups 1 --audio --loopback --gpu-metrics --output artifacts\phase6_2\recorded-local

# Clearly synthetic eligibility + actual detector/Kokoro/WASAPI, not real warning accuracy.
.\.venv\Scripts\python.exe -B benchmark_league_vision_e2e.py run --scenarios synthetic --trials 20 --warmups 1 --audio --loopback --output artifacts\phase6_2\audio-local

# Requires an interactive visible desktop; Escape closes the offline replay window.
.\.venv\Scripts\python.exe -B benchmark_league_vision_e2e.py run --mode window --scenarios warwick --trials 20 --warmups 1 --audio --loopback --gpu-metrics --output artifacts\phase6_2\window-local

# Explicit model load comparison, after Ollama is available. Run once without this flag as the control.
.\.venv\Scripts\python.exe -B benchmark_league_vision_e2e.py run --scenarios synthetic --trials 20 --warmups 1 --audio --loopback --gpu-metrics --contention --output artifacts\phase6_2\contention-local

# Real-engine safety cases; continuous visibility runs beyond the notification cooldown.
.\.venv\Scripts\python.exe -B benchmark_league_vision_e2e.py run --scenarios disappear,new-match,already-speaking,interrupt,continuous --trials 20 --warmups 1 --audio --loopback --gpu-metrics --output artifacts\phase6_2\safety-local
```

To establish genuine eligibility, launch the existing calibration UI through the benchmark:

```powershell
.\.venv\Scripts\python.exe -B benchmark_league_vision_e2e.py calibrate --manifest data\league-vision\e2e-eligibility.json
```

It works on a separate `e2e-eligibility-calibration.json` copy. Load/inspect source frames, draw the **actual** named assigned-lane region, manually label visible intervals and icon centers, select reviewed label rows, then explicitly export eligibility. Minimap labels require the actual assigned-lane region; viewport labels require the existing calibrated viewport evidence. Do not mark the entire map as nearby or infer Yuumi's position. The whitelist is tied to the recording fingerprint and calibration SHA; changing either invalidates it. No operator manifest was fabricated during this task.

```powershell
.\.venv\Scripts\python.exe -B benchmark_league_vision_e2e.py run --calibration data\league-vision\e2e-eligibility-calibration.json --manifest data\league-vision\e2e-eligibility.json --scenarios verified --verified-index 0 --trials 20 --warmups 1 --audio --loopback --output artifacts\phase6_2\verified-local

# Optional existing scoped labels; score unique sampled source frames once, preserving unknown/excluded areas.
.\.venv\Scripts\python.exe -B benchmark_league_vision_e2e.py run --scenarios warwick,galio,overlap,two-enemies --trials 1 --warmups 0 --labels data\league-vision\real-heldout-final-labels.json data\league-vision\real-warwick-final-labels.json --output artifacts\phase6_2\accuracy-local

.\.venv\Scripts\python.exe -B -m pytest -q
```

Scoped existing labels are assistant-reviewed, not independent human ground truth. Selected candidate confirmations alone cannot measure recall. New exhaustive annotations must include missed positives and negative windows. Repeated replay trials are deduplicated by source timestamp before accuracy scoring.

`verified` selects the requested whitelist interval automatically, including pre-roll, with at most six seconds of bounded source storage. An interval without an explicitly known onset can authorize current visibility but cannot establish appearance latency. `--eligibility synthetic` is an explicit alternative for selected recorded candidates; it must never be reported as genuine eligibility.

## Listening and acoustic validation

During explicit `--audio` trials, listen for complete words, Warwick pronunciation, stable voice identity/rhythm, clicks, ordering and gaps. A disappearance or injected interruption intentionally cancels the utterance; do not score that as a synthesis word omission. Callback/underflow counters and digital onset cannot prove correct pronunciation or absence of clicks. No new human subjective listening score is claimed.

For acoustic output use an independent continuous calibrated microphone recording, ideally with an electrical reference channel and synchronized visual marker/photodiode for true screen appearance. Calibrate channel skew, recorder/ADC delay, acoustic distance and detection thresholds. Do not equate Windows loopback with sound reaching the listener. No background microphone recording was introduced.

## Recommended next change

First investigate safe native cancellation/recovery: the measured 1.83-second reload is larger than the warm synthesis stage and can starve one-second notices. Separately, the 837 ms synthesis dispatch-to-delivered-audio stage dominates the warm deterministic notification path. Investigate adapting the **existing** native streaming path to short visual announcements with the same before-playback guard, absolute freshness deadline and cancellation. Compare identical phrases and verify complete speech and disappearance handling before changing production behavior. Do not replace the engine, relax stale checks, or change strict confirmation merely to lower benchmark numbers. Interactive capture and independently verified eligibility are required before claiming genuine appearance-to-spoken-warning performance.
