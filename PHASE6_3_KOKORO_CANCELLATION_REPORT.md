# Phase 6.3 — resident Kokoro cancellation recovery

16 November 2026. Implemented in Capri's existing synthesis session and worker. Her Kokoro pack, speaker 53, speed 1.1, eight CPU inference threads, pronunciation dictionaries, playback implementation, personality, conversation model, League mode, SVGs and movement are unchanged. Competitive vision alerts remain disabled and strict3 remains the visual default.

## Confirmed cause and chosen policy

The previous session gave unfinished generation zero acknowledgment grace, then killed the process. Cancelling startup also killed the loading model. Phase 6.2 measured 1,834 ms median reload time. Repeated one-second notifications could expire during each subsequent cold load.

The selected policy combines **cooperative cancellation at native callback boundaries** with **bounded asynchronous draining**. Stop still cancels playback through the existing audio token. The obsolete synthesis becomes a separate operation: its caller returns promptly, a single temporary drain thread inherits the held session lock, and it discards old chunks/acknowledgments. New user requests wait for the actual native operation to finish or the fallback restart. A resident but busy worker is not counted as available.

The installed sherpa-onnx **2.0.1** Python docstring explicitly specifies callback return `1` to continue and `0` to stop. This agrees with the [upstream callback contract](https://github.com/k2-fsa/sherpa-onnx/blob/master/sherpa-onnx/csrc/offline-tts.h). The worker advertises that capability only when the installed runtime documents it. If absent, the parent uses bounded draining without assuming native interruption. Native inference cannot be interrupted between callbacks; no claim of arbitrary ONNX-operation cancellation is made.

For supported requests, cancellation touches a per-request marker in a private native output directory. The existing callback checks it and returns `0`, discarding the partial result. Normal callbacks return `1`; original full/streamed sample conversion and voice settings remain intact. Crashed, malformed or unresponsive workers still restart. Legacy peers without protocol negotiation retain the previous safe restart behavior for unfinished generation.

The isolated real-runtime contract probe confirmed this behavior: continuing generated two callbacks / 79,902 frames; returning `0` generated one callback / 38,880 frames. The same engine subsequently generated the full next phrase. This is a measured early stop, not merely an assumption from a function signature. Probe output: `artifacts/phase6_3/native-probe/native-probe.json`.

## Protocol, ownership and priorities

Protocol 2 attaches a random request ID to every synthesis message and completion, and a consecutive chunk index to streamed audio. Missing/unknown IDs, invalid paths or out-of-order chunks fail safely. A small bounded retired-ID history discards late messages from completed/obsolete requests. A terminal message popped just as cancellation arrives is retained for draining rather than lost.

Native output lives in a **session-owned temporary directory** until completion. Valid active chunks are moved into the existing caller-owned speech directory; full WAVs move only after a matching successful completion and cancellation check. Cancelling the caller cannot remove a directory while the native process is still writing. Abandoned files are removed after acknowledgment or process termination. Native-directory interpreter-exit cleanup is ordered behind session shutdown. There is no PCM transport rewrite or extra TTS engine.

Session states distinguish stopped, restarting, loading, ready, synthesizing, cancellation requested, draining and unhealthy. Playback remains independently owned by the existing `PlaybackSession` and GUI speaking signals. A brief completed-generation acknowledgment race retains Phase 2's 50 ms maximum grace. At most one temporary drainer, one existing pipe reader and one resident native process belong to the session. Shutdown interrupts draining instead of waiting out its deadline.

The existing one-slot League notification policy is unchanged. Advice uses native priority 0; user speech defaults to 10. A small bounded waiter set selects the highest priority, then FIFO. Cancelled/expired waiters leave before sending native work. They do not extend obsolete-work deadlines or restart an otherwise healthy engine. Queue capacity is 32; application text/audio queues remain unchanged.

## Measured main comparison

Real CPU Kokoro, actual shared-mode WASAPI output and passive Windows loopback on the available Windows workstation. Fixed next response: **“What's on your mind?”** The obsolete text is a fixed three-sentence fixture. Cancellation occurs 100 ms after native dispatch; the next user synthesis is requested immediately. No League, microphone or Ollama is involved. Model readiness/priming before a trial is outside its timed cancellation interval; **drain waits and recovery reloads after cancellation are included**.

Environment: Intel i7-10700F, 16 logical processors, Windows build 26200, Python 3.11.6 and sherpa-onnx 2.0.1. The original CPU tuning file and eight synthesis threads were retained. The render-mix probe used 48 kHz stereo float32; native Kokoro PCM remained 24 kHz mono. No audio driver, device volume or model configuration was changed for the comparison.

20 measured warm trials per strategy, with one excluded warm-up, alternating execution order on the same machine. The restart control uses the same transport and voice with the previous terminate/reload policy, rather than treating Phase 6.2's different phrase and date as a paired baseline. The original source files were also preserved privately under `artifacts/phase6_3/baseline/` from commit `13becb4e832b18a560bc0843f9bd2b760c3a7038`.

| Immediate follow-up, milliseconds p50 / p95 / p99 / maximum (n=20 each) | Restart control | Drain only | Cooperative + drain |
|---|---:|---:|---:|
| Cancel → old synthesis caller returns | 11.5 / 18.2 / 18.2 / 18.6 | 7.0 / 14.8 / 14.8 / 15.0 | 7.5 / 15.1 / 15.1 / 15.4 |
| Cancel → worker available | 1703.3 / 1730.1 / 1730.1 / 1731.3 | 840.4 / 985.7 / 985.7 / 988.3 | 356.6 / 433.8 / 433.8 / 459.9 |
| Cancel → next playable PCM | 2112.5 / 2141.1 / 2141.1 / 2187.9 | 1207.5 / 1328.2 / 1328.2 / 1340.7 | 723.9 / 842.9 / 842.9 / 884.0 |
| Cancel → next digital mix onset | 2202.4 / 2232.2 / 2232.2 / 2278.3 | 1297.2 / 1418.5 / 1418.5 / 1430.2 | 813.5 / 933.3 / 933.3 / 974.6 |
| Recovery process starts / requests | 20 / 20 | 0 / 20 | 0 / 20 |

Cooperative recovery reduced median cancellation-to-next-PCM by **65.7%**, and median cancellation-to-next-digital-output by **63.1%** against this restart control. It improved next-audio time, not just process reuse. All strategies generated 20 follow-up outputs with no reported synthesis errors in this comparison.

### Uninterrupted control

20 warm trials per strategy using the same short phrase. These are request-to-result/output controls, not cancellation measurements.

| Request → response, milliseconds p50 / p95 / p99 / maximum | Restart setting | Drain setting | Cooperative setting |
|---|---:|---:|---:|
| Playable PCM | 342.0 / 416.0 / 416.0 / 468.5 | 341.1 / 365.4 / 365.4 / 396.0 | 341.2 / 397.5 / 397.5 / 404.3 |
| Digital mix onset | 437.5 / 504.9 / 504.9 / 559.5 | 435.7 / 469.2 / 469.2 / 484.8 | 437.1 / 486.4 / 486.4 / 493.0 |

No material median regression was measured. The small normal-control differences are not presented as a synthesis speed improvement. PCM hashes can differ between native generations; byte equality is not assumed as a speech-quality test. Fixed fixture WAVs were explicitly retained under `artifacts/phase6_3/listening/` for human listening.

A separate frozen-Phase-6.2-source control used 20 warm samples per version in alternating blocks of ten, with no overlapping model processes. Original source PCM readiness p50/p95/p99/max: **547.6 / 689.4 / 689.4 / 812.2 ms**; current source: **526.9 / 592.8 / 592.8 / 620.6 ms**. This later blocked run has different absolute timings and is not pooled with the interleaved policy comparison or claimed as a speedup. It found no material normal-path regression. Results and baseline/current WAVs: `artifacts/phase6_3/normal-source-control/`.

Artifacts: `artifacts/phase6_3/paired-audio/report.json` and `trials.csv`. Times use `perf_counter_ns()`. The report retains sample counts, missing observations, variance, device callback timestamps, PortAudio DAC estimates and loopback receipt separately. Percentiles use the project's floor-order convention, so p95/p99 can coincide for 20 samples. Loopback observes the Windows digital mix, **not sound arriving at the listener**. Other output must be quiet; ambiguous mix samples are rejected rather than assigned zero latency.

## Remaining controlled cases

The initial extended run tested 20 samples per strategy for every case below. All generated valid follow-up PCM and reported zero unexpected synthesis errors. Startup cases deliberately start a fresh load and therefore are not warm-inference controls. The notification case synthesizes three fixed short texts through the existing TTS route; the real one-slot event policy and voice priorities are covered separately by regression tests, without Riot API calls or LLM inference.

| Case, cancel → next PCM in ms, p50 / p95 / p99 / max | Restart control | Cooperative + drain |
|---|---:|---:|
| Before dispatch | 355.1 / 424.4 / 424.4 / 439.0 | 352.8 / 389.8 / 389.8 / 414.4 |
| During native generation, recover then request | 2106.3 / 2146.9 / 2146.9 / 2179.2 | 715.7 / 765.2 / 765.2 / 896.8 |
| After first unfinished native chunk | 2102.3 / 2190.9 / 2190.9 / 2214.9 | 817.5 / 858.3 / 858.3 / 964.5 |
| After completed native chunk | 352.6 / 379.1 / 379.1 / 403.2 | 348.2 / 399.9 / 399.9 / 414.6 |
| Three rapid cancellations | 2203.8 / 2272.6 / 2272.6 / 2274.4 | 690.8 / 814.5 / 814.5 / 814.6 |
| Consecutive notification speech, first response (no cancellation) | 378.8 / 419.4 / 419.4 / 451.7 | 368.2 / 418.9 / 418.9 / 452.3 |
| Early startup cancellation, 50 ms after process creation | 2168.9 / 2844.3 / 2844.3 / 2995.9 | 2070.4 / 2839.4 / 2839.4 / 2990.2 |
| Cancel while old audio plays | 462.7 / 510.2 / 510.2 / 757.1 | 465.2 / 509.6 / 509.6 / 513.5 |
| Later startup cancellation, one second after process creation | 3069.9 / 3947.0 / 3947.0 / 4112.8 | 1785.4 / 2661.2 / 2661.2 / 2712.1 |
| Injected unresponsive worker | 3154.9 / 4031.9 / 4031.9 / 4883.6 | 4098.2 / 4679.4 / 4679.4 / 4805.3 |

For completed generation, cancel → old caller return stayed fast: **.382 / 1.137 / 1.137 / 1.356 ms** p50/p95/p99/max under cooperative policy (n=20), with zero reloads. The restart control was .308 / .857 / .857 / .890 ms. This preserves the Phase 2 completed-acknowledgment path; next-PCM latency is a different measurement.

In the actual old-audio playback case, software abort completion took **5.977 / 8.719 / 8.719 / 8.753 ms** under cooperative policy, versus **5.397 / 8.245 / 8.245 / 8.540 ms** for the restart control (n=20 each). There is no model reload benefit when old synthesis was already complete; playback cancellation still uses the original mechanism. These are software abort times, not acoustic silence.

Across the initial 20-sample cases, cooperative policy required zero recovery starts for healthy synthesis cancellations, including rapid interruptions. The restart control started 20 replacement processes for native cancellation, 20 after partial chunks, and 60 for the three-interruption case. Drain-only mode had one timeout restart during rapid interruptions; cooperative mode had none. In both startup fixtures, cooperative policy used 20 initial loads total versus the control's 40 loads: the original required load remains, while the redundant replacement is avoided. Every late-startup cancellation snapshot recorded the `loading` state.

The unresponsive-worker fixture loads the real model, then deliberately stalls the Python worker before native generation. It is fault injection, not an observed spontaneous ONNX hang. All three policies recovered successfully with a replacement process in every trial. Cooperative mode pays its one-second detection/drain limit before restarting and was slower in this case. This measured penalty is retained; the policy is not claimed to win for every workload. Crash, malformed-response and partial-output recovery are additionally exercised with mock subprocesses in pytest.

Artifacts: `artifacts/phase6_3/paired-cases/` and `paired-fallback/`, each with per-trial CSV and full JSON including all three strategies and percentiles. Later machine activity changed absolute timings; comparisons are within each run, not across dates or setups.

### Digital measurement validity

The original 720 measured trials all produced follow-up PCM, with zero unexpected synthesis failures. **549** had attributable loopback onset. Other sound appeared during later cases: 171 output measurements were rejected/missing, including all original late-startup and injected-stall samples. Those samples are not reported as zero latency or used to claim digital-output improvement. The user subsequently paused the other sound, a fresh quiet check passed, and the affected cases were repeated with the restart control and selected cooperative policy. **All 240 measured recheck trials had attributable output and zero unexpected errors.** The original rejected records are preserved; the independent repeat is not silently substituted into the original run.

| Quiet repeat: cancel → digital mix onset, ms p50 / p95 / p99 / max (n=20 per cell) | Restart | Cooperative |
|---|---:|---:|
| Consecutive notification speech, first response (no cancellation) | 510.7 / 543.0 / 543.0 / 546.3 | 528.0 / 552.2 / 552.2 / 554.2 |
| Rapid interruptions | 2382.2 / 2582.2 / 2582.2 / 2633.0 | 980.8 / 1047.7 / 1047.7 / 1064.6 |
| Early startup cancellation | 2280.6 / 2480.1 / 2480.1 / 2512.1 | 2232.8 / 2398.2 / 2398.2 / 2472.6 |
| Cancel old playback | 525.3 / 558.8 / 558.8 / 562.6 | 518.2 / 548.6 / 548.6 / 549.9 |
| Later startup cancellation | 2208.9 / 2295.1 / 2295.1 / 2302.7 | 1205.9 / 1278.3 / 1278.3 / 1420.0 |
| Injected stalled worker | 2224.3 / 2341.5 / 2341.5 / 2346.5 | 3212.8 / 3370.8 / 3370.8 / 3386.9 |

The stalled-worker repeat quantifies the approximately one-second fallback penalty clearly. Late-startup preservation saves approximately one second in this fixture; cancelling only 50 ms after startup cannot save much loading work. Steady notification synthesis does not benefit from cancellation recovery, and its small timing differences are retained rather than claimed as a gain. Full PCM/dispatch/abort/memory measurements and counts are in `artifacts/phase6_3/quiet-output-recheck/{report.json,trials.csv}`. No invalid/missing original measurement was counted as a successful zero-latency sample.

## CPU and memory tradeoff

On the primary immediate-follow-up fixture, median sampled aggregate process-tree CPU time per complete trial was **3.28 CPU seconds** restart, **5.94** drain-only and **3.91** cooperative. Cooperative mode spends some CPU finishing the current native batch instead of killing it immediately, while avoiding the full abandoned utterance and cold reload. This is a latency/CPU tradeoff, not a claim of universally lower CPU use. Process-tree sampling can miss the final CPU interval of terminated children, so these totals are approximate and include the next synthesis/playback interval.

Native-reported CPU for the entire abandoned operation, including the 100 ms before cancellation, was 4,266 ms median for drain-only versus 2,188 ms cooperative. This is not isolated post-cancellation CPU time; that exact split was not instrumented. Sampled peak aggregate RSS was approximately **644.5 MiB** restart, **644.6 MiB** drain and **642.1 MiB** cooperative. No meaningful memory reduction is claimed. One native model is retained; no GPU runtime or extra model is introduced. These controlled tests do not measure live-game FPS or concurrent Qwen/vision performance.

A separate 20-trial consecutive cooperative-only rapid-interruption run retained one PID throughout, with **zero recovery starts**. Sampled aggregate RSS was 620.23 MiB in the first measured trial and 620.98 MiB in the last, including the growing benchmark report. This is bounded evidence over this run, not a proof of indefinite leak-free operation. Artifact: `artifacts/phase6_3/consecutive-residency/`.

## Speech correctness and tests

The local cached Whisper-base diagnostic recognized the complete fixed phrase “What's on your mind?” in **11/11 retained WAVs**, including all policies, callback/no-callback controls, generation after native early stop, and frozen/current source outputs. No network download or microphone was used. This is supporting evidence, not a definitive human pronunciation/listening score. Output hashes differ across native generations, so preservation is not claimed from text or checksum alone. The WAVs remain available for the listening procedure below.

An explicitly launched, separate fixed-fixture validation also retained two short **Windows render-mix** recordings after active cancellation, one per restart/cooperative strategy. Capture began only after a quiet guard and was bounded in memory; it never used the microphone. Both digital-output WAVs were recognized as the complete expected phrase, including the initial “What's”, with no obsolete sentence recognized. This checks generated **and rendered** words in these samples, while remaining an ASR-assisted test rather than human/acoustic certification. Files and recognition evidence: `artifacts/phase6_3/digital-word-check/`.

Final full suite: **915 passed in 212.60 seconds**, using `.\.venv\Scripts\python.exe -B -m pytest -q`. Log: `artifacts/phase6_3/final-tests.log`. An initial run had one failure because a legacy startup-cancellation test assumed unconditional termination. It now explicitly tests the restart rollback policy, retaining the termination assertion; new tests cover protected startup, hung startup deadlines and explicit shutdown. Test fixtures isolate the cancellation settings and stop asynchronous native work before removing fixture assets.

The added regressions cover late chunks/acknowledgments, IDs and chunk sequence validation, internal-exit marker spoofing, worker crashes, file ownership, callback partial PCM rejection, warm reuse, queued deadlines, rapid cancellations, priority, cancellation after queue-pop, shutdown generations and worker startup. Existing voice, pronunciation, streamed speech, League mode, SVG and movement tests were included in the full run. No tests were run concurrently with the latency benchmarks.

Final cleanup checks found **zero remaining benchmark/native worker processes and zero session-owned native temporary directories**. `git diff --check` passed. The source settings still reported competitive vision disabled and Riot approval false; strict3 was not changed. Evidence: `artifacts/phase6_3/final-state.json`.

The existing paced OpenCV-to-voice integration was additionally rerun for disappearance and a valid controlled synthetic sighting, one trial per strict/adaptive policy. Disappearance produced no speech; the following valid fixture spoke once; every readiness check retained the same native PID. The competitive gate remained unchanged. This is an integration sanity check, not new real-footage accuracy or live-game performance evidence. Artifact: `artifacts/phase6_3/vision-integration/`.

## Configuration and rollback

| Setting | Default | Meaning |
|---|---|---|
| `CAPRI_KOKORO_CANCEL_POLICY` | `cooperative` | `restart` restores terminate/reload; `drain` waits without callback stop |
| `CAPRI_KOKORO_ABANDON_TIMEOUT` | `1.0` seconds | Maximum obsolete synthesis drain time, then restart |
| `CAPRI_KOKORO_STARTUP_DRAIN` | `10` seconds | Maximum background completion time for an already-started model load |
| Existing `TTS_CANCEL_DRAIN_SECONDS` | `.05` seconds | Short completed-generation acknowledgment grace, unchanged |

Deadlines are bounded and validated before launching work. The original request timeout can shorten them. A true hang necessarily pays detection time before restart; zero restarts is not the objective. The maximum drain is configurable because a long indivisible native inference may be better restarted than awaited. Asset, voice settings or lexicon changes still trigger an explicit reload.

## Files changed

| File | Change |
|---|---|
| `voice/kokoro_session.py` | Request isolation, lifecycle status, lock handoff/draining, priority admission, scoped output ownership, startup preservation and restart fallback |
| `voice/kokoro_worker.py` | Backward-compatible protocol 2, capability declaration, callback marker cancellation and discarded partial results |
| `gui/workers.py` | Lower-priority native admission for existing advice speech; user speech route unchanged |
| `config.py` | Three environment-overridable cancellation/recovery settings |
| `benchmark_kokoro_cancellation.py` | Reproducible fixed-fixture policy comparison with actual synthesis/playback and optional loopback/retained WAVs |
| `tests/test_kokoro_recovery.py` | Hardware-free request/late-message/file-ownership/priority/shutdown/failure regressions |
| `tests/test_tts.py` | Explicit restart-policy regression with guaranteed subprocess cleanup |
| `tests/conftest.py` | Isolated recovery settings and asynchronous native-session teardown |
| `PHASE6_3_KOKORO_CANCELLATION_REPORT.md` | Evidence, methods, limits and reproduction commands |

No new dependencies, polling service, GPU runtime or model installation. Private benchmarks and retained fixed audio remain under the ignored `artifacts/` directory. Original model assets and personal pronunciation files are untouched.

## Windows reproduction and listening

```powershell
Set-Location F:\Neko

# Primary paired real-audio comparison; keep all other output quiet.
.\.venv\Scripts\python.exe -B benchmark_kokoro_cancellation.py --samples 20 --cases immediate,normal --audio --loopback --save-audio artifacts\phase6_3\listening-local --output artifacts\phase6_3\audio-local

# Remaining real-engine cases; each policy runs 20 measured trials.
.\.venv\Scripts\python.exe -B benchmark_kokoro_cancellation.py --samples 20 --cases before,native,chunk,complete,rapid,notifications,startup,playback --audio --loopback --output artifacts\phase6_3\cases-local

# Late startup cancellation and deliberately stalled worker fixture.
.\.venv\Scripts\python.exe -B benchmark_kokoro_cancellation.py --samples 20 --cases startup-late,fault --audio --loopback --output artifacts\phase6_3\fallback-local

# Direct native callback contract check and fixed listening WAVs; no playback.
.\.venv\Scripts\python.exe -B benchmark_kokoro_cancellation.py --native-probe --save-audio artifacts\phase6_3\probe-listening-local --output artifacts\phase6_3\probe-local

# Quiet repeat of the affected output cases, baseline versus selected policy.
.\.venv\Scripts\python.exe -B benchmark_kokoro_cancellation.py --samples 20 --policies restart,cooperative --cases notifications,rapid,startup,playback,startup-late,fault --audio --loopback --output artifacts\phase6_3\quiet-recheck-local

# Consecutive reuse without another policy replacing the engine between trials.
.\.venv\Scripts\python.exe -B benchmark_kokoro_cancellation.py --samples 20 --policies cooperative --cases rapid --output artifacts\phase6_3\residency-local

# No output-device use; real synthesis/recovery still measured. Playback-only case is omitted.
.\.venv\Scripts\python.exe -B benchmark_kokoro_cancellation.py --samples 20 --cases native,chunk,complete,immediate,rapid,startup,normal --output artifacts\phase6_3\pcm-local

.\.venv\Scripts\python.exe -B -m pytest -q

# Behavior rollback, before launching Neko; voice/model settings remain unchanged.
$env:CAPRI_KOKORO_CANCEL_POLICY = 'restart'
.\.venv\Scripts\python.exe -B main.py
```

Each output directory must be fresh. Inspect per-trial CSV/JSON and missing/error counts. Fixed generated speech is saved only with `--save-audio`; the benchmark CLI's onset probe never saves loopback audio. The separate explicit `artifacts\phase6_3\verify_digital_words.py` validation saved only the two controlled test outputs described above. Normal Neko operation gains no recording mechanism.

Open the retained `normal-*.wav`, `immediate-*.wav` and `digital-word-check\*.wav` files to compare full initial/final words, contractions, voice identity, rhythm and any clicks or truncation. The private frozen-source helper can be rerun with `.\.venv\Scripts\python.exe -B artifacts\phase6_3\verify_normal.py` while its preserved baseline files are available. Intentionally cancelled old speech is expected to stop; it must not reappear in a later response. Automated text or non-silent PCM alone cannot certify audible word correctness. Microphone/acoustic stop latency requires a separate calibrated recording; none is claimed here.
