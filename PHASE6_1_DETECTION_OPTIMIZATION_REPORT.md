# Phase 6.1 — detection and notification correctness

14 November 2026. Implemented in the existing detector, League workers and speech route. No new dependency, capture thread, model, speech engine or GPU detector. **Competitive live capture/alerts remain disabled.** The original working calibration and reference images were preserved.

## Results and evidence limits

The existing 253.867-second recording supplies 2,539 lossless minimap samples at 10 FPS. Accuracy uses the same scoped assistant-reviewed labels as Phase 6: 93 primary frames / 100 champion-positive frames, and a separate 81-frame Warwick-only review / 27 positive frames. These are **not independently validated human labels**. The audits share an early negative window; do not add their exposure as independent evidence. Existing unknown regions, ambiguous bot overlaps, champion-limited coverage and left-censored onsets remain excluded/qualified.

Tuning windows remain separate from the primary/Warwick evaluation windows. Faster-policy thresholds were fixed before the comparison; no broad threshold sweep on held-out labels was used. Reference ablations are exploratory and were not used to replace working references. There are no new human annotations or full-match ground truth. Candidate review cannot establish full-recording recall.

| Confirmation policy on identical optimized raw evidence | Primary TP / FP / FN | Precision / recall | Warwick TP / FP / FN | Precision / recall | Full-clip candidate fragments |
|---|---:|---|---:|---|---:|
| Strict three consecutive samples, retained default | 62 / 0 / 38 | 100% / 62.0% | 19 / 0 / 8 | 100% / 70.4% | 109 |
| Two validated consecutive samples | 41 / 0 / 59 | 100% / 41.0% | 20 / 0 / 7 | 100% / 74.1% | 176 |
| Adaptive: strong two-sample evidence, otherwise three | 64 / 0 / 36 | 100% / 64.0% | 20 / 0 / 7 | 100% / 74.1% | 109 |

The pinned original Phase 6 detector also scores 62/0/38 and 19/0/8. Default strict-mode identity/position outputs were identical to the original pipeline on **all 2,539 frames** in the paired run. Adaptive mode is available for explicit offline/controlled use; it is **not the new global default**. The two-validated policy loses too much weak-evidence recall and is not recommended.

Tuning results, out of 85 labelled positive frames: strict 58 TP / 0 FP / 27 FN; adaptive the same; two-validated 29 / 0 / 56. These tuning figures are not added to held-out accuracy. All three policies had zero false candidate episodes in the reviewed primary and Warwick windows, hence **0 observed false candidate episodes/minute within each small audit**. This is not a production/full-recording false-alert rate. Strict and adaptive still miss the Darius episode: five of six labelled primary episodes are detected, versus two of two Warwick episodes; frame misses are counted above, including deliberately withheld confirmation frames.

## Confirmation latency

| Video-time measurement | Strict p50 / p95 / max ms | Two-validated | Adaptive | Known detected onsets |
|---|---|---|---|---:|
| Primary appearance → confirmed sighting | 200 / 200 / 300 | 100 / 100 / 200 | 200 / 200 / 200 | 3 |
| Warwick appearance → confirmed sighting | 200 / 200 / 200 | 100 / 100 / 100 | 100 / 100 / 200 | 2 |
| Primary first successful raw detection → confirmation | 200 / 200 / 200 | 100 / 100 / 100 | 100 / 100 / 200 | 3 |

First successful raw matches occurred at the labelled appearance sample for two primary onsets and 100 ms later for one; both Warwick onsets matched at their first labelled sample. Raw evidence here means a successful identity-validated template match before temporal confirmation, not merely a possible visual hint. Sampling resolution is 100 ms. The existing metrics use floor-order percentiles, which are especially coarse with two or three onsets; maxima and counts are shown rather than treating these as stable population p95 estimates. Undetected or left-censored episodes do not produce latency measurements.

Strict mode remains three consecutive current observations. Adaptive early confirmation requires **both** samples to have similarity ≥ .94, separation from the strongest other roster template ≥ .20, spatial displacement ≤ .025 normalized ROI units, and no detected overlap ambiguity. Otherwise it retains three-sample confirmation. The comparison-only two-validated policy uses .90 / .15 with the same displacement limit. These are similarity/separation thresholds, **not calibrated probabilities**. Missing, stale, conflicting, regressing-time, new-epoch and implausibly jumping evidence resets the track; no position is extrapolated through an absent sample. Sources lacking explicit identity-separation evidence stay on the strict fallback.

## Processing and CPU

The old pipeline was loaded read-only from pinned local commit `b9ed7e7b6ac9d9db61e7f7f4e183b0bd5547529a`, including its original matcher. Old/new pipelines processed the same recorded pixels in alternating order, with 20 warm-up frames each and one OpenCV CPU thread. Pytest and other detector benchmarks were not running during this paired measurement. It excludes video decoding, Windows capture, model inference, TTS and audio output.

Environment: Windows build 26200, 16 logical processors, OpenCV 5.0.0 and NumPy 2.4.6. No GPU provider or hardware setting was changed.

| Paired actual processing | Before ms | After ms | Reduction |
|---|---:|---:|---:|
| p50 | 30.5769 | 25.6104 | 16.2% |
| p95 | 38.4678 | 32.7317 | 14.9% |
| p99 | 44.3125 | 37.6590 | 15.0% |
| Maximum | 73.4374 | 56.4013 | 23.2% |
| Samples | 2,539 | 2,539 | — |

The prior report's 22.2/23.0 ms p50/p95 was **not reproduced in this environment**; the current paired baseline is slower. The optimization improves the current comparison but does not demonstrate a p95 ≤23 ms. Sequential exploratory runs were noisier, so the interleaved comparison above is the performance result. Percentiles retain the last 2,048 samples; counts/maxima cover the run.

Aggregate active processing-thread CPU time fell from **74.828 to 62.313 seconds**, about 16.7%. Dividing this work by source-video duration estimates **29.48% → 24.55% of one logical core at this recording's 10 FPS**. This is a work-rate estimate, not measured live-game CPU/FPS impact. Windows thread CPU accounting is coarse per call; aggregate totals are used. The combined old+new benchmark consumed 138.52 process CPU seconds over 150.26 wall seconds (~92.19% of one core), running unpaced. No actual capture throughput, League FPS, GPU contention or live audio measurement is claimed.

Two measured costs motivated the implementation:

1. The original roster check repeatedly normalized the same small patch in nine OpenCV calls. Cached per-channel, zero-mean normalized reference vectors now perform the same full-roster NCC comparison in one small matrix product. Regression tests compare every roster score/scale against OpenCV; actual paired outputs show no identity changes.
2. Yuumi's independent position cannot be recovered from Caitlyn's attached marker. Searching for a Yuumi portrait every frame was both unhelpful and potentially misleading. The pipeline skips that lookup and explicitly leaves her distance/proximity unknown. Other invalid/out-of-range/ambiguous own-icon results also cannot create proximity claims. The known lane may still qualify a separately calibrated lane observation; it is never labelled measured player distance.

The grayscale image and filtered references remain shared/cached. No additional model, polling loop or capture worker was added. Strongly conflicting champion identities occupying the same icon are rejected; lesser detected enemy overlaps are marked ambiguous and cannot take the adaptive fast path. All ten known roster identities, including allied distractors, remain in ambiguity comparisons.

## Notification lifecycle and duplicates

Confirmed problems were proposal-time champion cooldown consumption, queue-take-time speech cooldown consumption, repeated announcements after a continuous sighting outlived its cooldown, and rolling `first_seen_at` values losing episode identity. A dropped visual message could also enter chat/context before any audio started.

The route now follows `CONFIRMED → PROPOSED → ACCEPTED → STARTED/DROPPED` using the existing announcement and one-slot policy. Champion cooldown is consumed only at actual playback-start callback. The GUI defers its global speech cooldown until that callback too. Proposals known to expire before an existing speech cooldown ends are rejected with the next useful retry time. Rejected/expired/cancelled pre-playback work spends no champion cooldown.

Stable raw episode origins and a spoken-episode latch suppress repeats. A 500 ms **deduplication-only** debounce handles brief confirmation dropouts; while evidence is missing, current observations remain empty and old notifications are cancelled. The debounce never manufactures a visible position, proximity or new alert. Unstarted/dropped proposals do not acquire this latch. Minimap/viewport evidence for the same champion is coalesced without treating their coordinate systems as interchangeable.

Visibility loss, stale source, epoch change, mute, capture pause and shutdown set the existing speech cancellation path's per-notification token. Freshness is checked again before playback. User requests retain priority. State is bounded (one pending/inflight speech item, at most 64 offered visual records, bounded metrics). Late callbacks from cancelled notices cannot cancel newer GUI work. Visual text enters chat/context only after playback starts; cancelled synthesis no longer publishes a ghost spoken message. Qwen is never called for these notices.

**Scheduling simulations, not audible speech:** the harness uses actual processed observations, the old/new dispatchers and voice policies, an explicit 400 ms simulated synthesis delay, and no TTS/audio calls. It runs actual profile eligibility separately from an artificial scenario that qualifies all detections as being in a controlled test region. That qualification is not real lane/proximity evidence and never changes the application profile or approval gate.

| Simulation | Before | After |
|---|---|---|
| Actual recording calibration: eligible proposals / starts | 0 / 0 | 0 / 0 |
| Controlled-region real-recording scheduling: proposals / accepted / starts | 61 / 61 / 25 | 84 / 57 / 35 |
| Controlled region, injected user-busy intervals: proposals / accepted / starts | 61 / 61 / 24 | 87 / 60 / 34 |
| Synthetic 20-second continuous visible sighting: proposals / starts | 3 / 3 | 1 / 1 |
| Duplicate starts in that known continuous fixture | 2 | 0 |
| Same fixture, interrupted before first simulated start: first successful start | 8.4 s | 1.9 s |

The synthetic uninterrupted fixture previously started at .4, 8.4 and 16.4 seconds; now only .4 seconds. In the interrupted fixture, the old route wasted the initial cooldown despite not starting, then spoke at 8.4 and 16.4; the corrected route recovered at 1.9 seconds and did not repeat. These are deterministic scheduling results with a supplied fake TTS delay, **not real engine/playback latency improvements**.

The full artificial recording scenario produces **more proposals**, not fewer: allowing fresh retries after discarded work recovers starts that the old incorrect cooldown suppressed. Therefore proposal-volume reduction is demonstrated for continuous sightings, not for the whole forced-eligibility clip. Real intrusiveness cannot be inferred from making every map sighting artificially relevant.

Within the declared primary/Warwick scopes, simulations recorded **0 false starts and 0 duplicate starts**, or 0 observed simulated false starts/minute in those small scopes. The idle controlled primary case had 1 → 2 true starts; the user-busy case 0 → 1. Warwick had zero simulated starts in both routes under this full-clip coalescing/global-cooldown scenario; this is not evidence of successful Warwick speech delivery. Starts outside reviewed coverage remain unverified. Actual eligibility is zero because this calibration has no independently verified Yuumi position or named lane regions; zero actual starts is correct behavior, not an alert-accuracy achievement.

## Reference and false-positive investigations

Separate working/official/tighter-inner variants for Darius, Karma and Warwick were tested without overwriting working assets. Warwick also used the previously recorded 188.8-second crop. These are **target-only ablations with all ten roster identities still competing**, not full-pipeline speed benchmarks. Only champions actually covered by each source audit are scored. A regression prevents the Warwick-only audit from being silently expanded into Karma/Darius negatives.

| Champion | Working reference | Official alternative | Inner crop / recorded alternative |
|---|---|---|---|
| Darius, tuning / primary TP | 0/20, 0/12 | 0/20, 0/12 | Inner: 0/20, 1/12 |
| Karma, primary TP | 3/10 | 3/10 | Inner: 3/10 |
| Warwick, separate reviewed-window TP | 19/27 | 19/27 | Inner: 18/27; recorded: 18/27 |

These scoped ablations had zero scored false-positive frames. Karma has no usable positive examples in the existing tuning windows (ambiguous bot regions are excluded), so they cannot establish a better Karma reference. Darius's one additional held-out inner-crop frame has no tuning gain and is insufficient for adoption. Masked matching was not added: the evidence does not yet justify its extra complexity/false-positive risk. Working references remain unchanged. Pings and severe overlap still require unknown/excluded handling; the earlier unconfirmed Ziggs/ping crop is not relabelled as a negative merely to manufacture a false-positive score. There is no demonstrated need for YOLO or Qwen vision inference.

## Timing instrumentation and unavailable measurements

Opt-in `CAPRI_LATENCY_METRICS=1` extends existing numeric tracing. Runtime capture records capture-start → frame-ready, ready → detection-start and capture-frame → detection-complete. Successful raw detection, temporal confirmation, proposal, policy acceptance, TTS dispatch, first audio-ready and estimated DAC-presentation marks are connected through existing speech tracing. No transcript, raw audio, identities beyond existing structured observations, credentials or gameplay frames are logged by this tracing. No high-frequency Qt update was added.

Offline raw/confirmation times use video timestamps; runtime intervals use high-resolution monotonic/performance clocks. Windows capture latency, real policy-to-TTS delay, first PCM readiness and DAC estimate **were not measured with live hardware in this task**. The paths are instrumented/tested, not filled with invented numbers. PCM readiness, scheduled callback/device onset and actual listener-heard acoustic output remain separate. The existing audio/loopback benchmarks remain the way to validate hardware output after an approved controlled test; no background recording was introduced.

## Files and configuration

| File | Change |
|---|---|
| `core/league_visual.py` | Validated observations, optional confirmation policies, stable episode identity, unknown Yuumi proximity, overlap rejection, bounded delivery lifecycle/dedup/cancellation |
| `core/league_replay.py` | Cached full-roster normalized color comparisons |
| `core/league_voice.py` | Accepted/started/dropped hooks, deferred actual-start cooldown, cancelled-item freshness, known-deadline rejection; one pending/inflight item |
| `core/league.py` | Forward visual delivery status through the existing League service |
| `gui/league_vision.py` | Serialize dispatcher lifecycle updates/cancellation, capture timing; existing workers only |
| `gui/workers.py`, `gui/window.py` | Actual playback-start acknowledgement, cancellation propagation, late-callback protection, defer visual chat publication until speech starts |
| `core/league_capture.py`, `core/latency.py` | Optional numeric capture and detection-to-speech stage marks |
| `core/league_visual_evaluation.py` | Same-raw policy replay, scoped raw/confirmation scoring, no-audio scheduling simulation |
| `benchmark_league.py` | Pinned old/new paired timing, separate reference ablations, actual/controlled notification simulations |
| `examples/league-vision-calibration.json` | Explicit `confirmation_policy: "strict3"` default |
| `tests/test_league_visual_delivery.py`, `tests/test_league_visual.py`, `tests/test_league_replay.py` | Delivery/queue/race/freshness/epoch/attachment/geometry/overlap/timing/scope/NCC regression coverage |
| This report | Results, tradeoffs, reproduction and limitations |

No changes to `voice/`, Kokoro assets, pronunciation, primary models, SVG assets/renderers, teleport renderer, inference scheduler, `config.py` or League Companion mode logic. Changes in `gui/window.py` are confined to notification handling. Existing role/lane persistence and monitoring workers were reused. No new package requirement.

Original `data/league-vision/calibration.json` and `evaluation-final-calibration.json` remain unchanged. Optional controlled/offline adaptive profile: `data/league-vision/phase6_1-adaptive-calibration.json`. Global/default confirmation remains strict3. The only new profile key is `confirmation_policy` (`strict3`, `two_validated`, `adaptive`). Existing smoothing, similarity, roster ambiguity, freshness, voice cooldown and approval settings remain in use.

## Verification

The complete current suite ran successfully: **858 passed in 139.31 seconds** (`artifacts/phase6_1/verified-all-tests.log`), including existing voice, TTS cancellation, League Companion, SVG, docking and movement regressions. A final isolated delivery/lifecycle run passed **33 tests in 1.49 seconds** (`delivery-final-tests.log`). New coverage includes strict/two/adaptive ordering and identity separation, raw disappearance, stable origins, overlaps, attached-host markers, invalid player coordinates, queue priorities, wasted cooldown recovery, stale/epoch/mute/shutdown cancellation, continuous/cross-source deduplication, late GUI callbacks, no ghost chat message, numeric timing accuracy, cached NCC equivalence, shared raw evidence and champion-limited reference-review scope. Tests use fake events/clocks/Qt offscreen fixtures and require no League, Ollama, microphone or speakers.

`git diff --check` passed (only normal LF/CRLF notices). A separate diff check confirmed no changes to voice modules/assets, SVG/teleport renderers, model client, inference scheduler or `config.py`. Byte comparison confirmed the working and frozen original calibration files remain identical; their working reference paths are unchanged. Both live-vision configuration flags evaluated to `False`. No timing benchmark or test process remained running after verification.

## Reproduce on Windows

Run timing benchmarks separately from tests/other benchmark jobs. Use fresh output folders for recording/policy/reference runs. Git is needed only for the optional pinned baseline comparisons; they load trusted local code into private benchmark modules, never launch another assistant, capture worker or audio system.

```powershell
$baseline = "b9ed7e7b6ac9d9db61e7f7f4e183b0bd5547529a"

# Paired full detector processing on identical real cached pixels.
.\.venv\Scripts\python.exe -B benchmark_league.py --real-vision-report "data\league-vision\real-evaluation-baseline\detections.json" --real-vision-calibration "data\league-vision\evaluation-final-calibration.json" --real-vision-output "data\league-vision\local-paired-01" --paired-vision-baseline $baseline --output "artifacts\phase6_1\local-paired-01.json"

# One actual CV pass; replay exactly the same raw observations through A/B/C.
.\.venv\Scripts\python.exe -B benchmark_league.py --real-vision-report "data\league-vision\real-evaluation-baseline\detections.json" --real-vision-calibration "data\league-vision\evaluation-final-calibration.json" --real-vision-output "data\league-vision\local-policies-01" --confirmation-policies --vision-labels "data\league-vision\real-tuning-final-labels.json" --vision-labels "data\league-vision\real-heldout-final-labels.json" --vision-labels "data\league-vision\real-warwick-final-labels.json" --output "artifacts\phase6_1\local-policies-01.json"

# No-audio actual/controlled scheduling and a synthetic continuous-sighting fixture.
.\.venv\Scripts\python.exe -B benchmark_league.py --real-vision-report "data\league-vision\local-policies-01\strict3\detections.json" --real-vision-calibration "data\league-vision\evaluation-final-calibration.json" --real-vision-output "data\league-vision\local-notifications-01" --simulate-notifications --notification-baseline-revision $baseline --vision-labels "data\league-vision\real-heldout-final-labels.json" --vision-labels "data\league-vision\real-warwick-final-labels.json" --output "artifacts\phase6_1\local-notifications-01.json"
# Add --simulated-user-busy for the documented interruption scenarios.

# Separate local reference variants; working references are not overwritten.
.\.venv\Scripts\python.exe -B benchmark_league.py --real-vision-report "data\league-vision\real-evaluation-baseline\detections.json" --real-vision-calibration "data\league-vision\evaluation-final-calibration.json" --real-vision-output "data\league-vision\local-variants-01" --reference-variants "data\league-vision\phase6_1-reference-variants.json" --vision-labels "data\league-vision\real-tuning-final-labels.json" --vision-labels "data\league-vision\real-heldout-final-labels.json" --vision-labels "data\league-vision\real-warwick-final-labels.json" --output "artifacts\phase6_1\local-variants-01.json"

.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider
```

All real frames, reference alternatives, scope-preserving labels and benchmark JSON remain local/ignored under `data/league-vision/` and `artifacts/phase6_1/`. Key evidence: `paired-processing.json`, `optimized-policies.json`, `reference-variants.json`, `simulation-final.json`, `simulation-busy-final.json`. Live competitive alerts remain disabled pending Riot approval; the controlled-eligibility simulation is not a deployment bypass.

Recommended next step: independently labelled, longer footage with separate tuning/test clips containing clear and overlapping Darius/Karma, pings, allied distractors and verified player-position cases. The current limited audits cannot justify a general false-alert rate, better Darius/Karma recognition, a global faster confirmation default or approved live proximity alerts. Actual controlled capture-to-audio measurements come after that validation and the required approval.
