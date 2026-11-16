# Phase 6.4 — real-footage review and location eligibility

16 November 2026. This implements stricter evidence checks and a reproducible independent-review workflow on the existing OpenCV pipeline. It does **not** establish independently validated full-match accuracy. Competitive alerts remain disabled; strict3 remains the default. No voice/model assets, Kokoro/session/playback code, League APIs, SVGs, docking or movement code changed.

## Evidence available

The actual local player-view recording is 1920×1080, 60 FPS, 253.867 seconds. Both full reruns analyze 2,539 samples at 10 Hz, with the unchanged cached ten-champion roster: enemies Darius, Warwick, Galio, Ziggs, Karma; allies Vladimir, Ekko, Taliyah, Caitlyn, Yuumi. The player is explicitly UTILITY/Yuumi; no lane is inferred from champion identity.

The three existing audit files identify their reviewer as `assistant_visual_audit`, with `independent_human_review: false`. There is no operator-confirmed eligibility manifest, independently verified Yuumi point, or verified named lane region in the current calibration. These are missing evidence, not proof that every detected enemy is irrelevant.

| Independent real-footage result | Result |
|---|---|
| Independently human-reviewed sightings available at completion | 0 |
| Independently verified eligible / ineligible sightings | 0 / 0; remaining relevance unknown |
| Independent precision, recall, incorrect identities and missed sightings | Unavailable pending review |
| Correct / missed / false real notifications | Unavailable without eligibility ground truth |
| Genuine accepted notifications / PCM / loopback samples | 0 / 0 / 0 |
| True Windows capture-to-digital-output latency | Not measured |

Do not interpret zero emitted warnings as 100% precision, zero missed warnings, or successful audible warning validation. No synthetic eligibility is counted as real evidence.

## Measurements performed

Both full runs used the same actual recording, calibration, cached references and strict3 settings. No tests, model inference or other benchmark was deliberately run concurrently with either full-video measurement. These are two full-file processing runs, not 20 paired device-latency trials. Metrics retain the most recent 2,048 processing samples; total count and maximum include the whole run.

| Full-file measurement | Before | After |
|---|---:|---:|
| Analyzed frames / candidate fragments | 2,539 / 109 | 2,539 / 109 |
| Detector processing p50 / p95 / p99, ms | 18.92 / 25.23 / 30.62 | 18.86 / 19.86 / 22.59 |
| Detector maximum, ms | 365.50 | 33.18 |
| Dispatcher update p50 / p95, ms | 0.0156 / 0.0237 | 0.0180 / 0.0265 |
| Full-run CPU / wall seconds | 134.52 / 139.96 | 128.31 / 128.53 |
| CPU as percent of one core, including file decode | 96.11% | 99.83% |
| Sampled process RSS, MiB | 88.73 | 89.38 |

Champion identity, source, position, box, similarity and episode origin were compared on **all 2,539 frames: zero changed frames**. This task adds evidence validation, not a faster matcher. Different initialization/cache/CPU conditions can account for timing variation; the lower p95 is not presented as a proven optimization effect. Decoding and JSON output are included in full-run CPU, and the file is processed faster than normal playback. These figures do not establish live capture overhead or active-game FPS impact.

The new report records **2,670 confirmed champion-frame observations**, all with unknown player-position/relevance evidence under the current profile. This is not 2,670 independent sightings. Eligible observation frames, proposals, acceptances, playback starts and dropped proposals are all **zero**. There was nothing genuinely eligible to synthesize, so Kokoro and loopback were deliberately not forced through a fake lane qualification.

The previous audit files were scored against both full runs. Results are unchanged:

| Existing assistant-audit scope | TP / FP / FN champion-frames | Precision / recall | Episode misses | Incorrect identity frames |
|---|---:|---:|---:|---:|
| Primary: 93 sampled frames, 9.3 s exposure | 62 / 0 / 38 | 100% / 62.0% | 1 of 6 | 0 |
| Warwick-only: 81 frames, 8.1 s exposure | 19 / 0 / 8 | 100% / 70.4% | 0 of 2 | 0 |
| Separate tuning scope | 58 / 0 / 27 | 100% / 68.2% | 1 of 5 | 0 |

These are **limited assistant-reviewed diagnostic windows, not independently human-validated results**. Primary and Warwick scopes share a negative interval and must not be pooled as independent exposure. Unknown overlap regions remain excluded. The primary misses include Darius (0/12 positive frames), Karma (3/10), Galio (36/50) and Ziggs (23/28). Zero false episodes per minute was observed only within each small audit; a full-match false-alert rate remains unknown.

Saving raw observations now makes the video-time breakdown measurable. In the three explicitly onset-marked primary episodes, appearance→raw p50/p95/max was 0/0/100 ms, raw→confirmation 200/200/200 ms, and appearance→confirmation 200/200/300 ms. The two Warwick onsets had 0 ms sampled raw delay and 200 ms confirmation delay. A zero sampled raw delay means the detector matched the first **sampled** annotated appearance; it is not instantaneous screen recognition. These small-sample percentiles use the existing lower-order statistic; consult sample count and maximum, not just p95.

The existing paced replay benchmark was also run without audio or synthetic eligibility:

```powershell
.\.venv\Scripts\python.exe -B benchmark_league_vision_e2e.py run --mode replay --scenarios warwick,galio,overlap,two-enemies --trials 2 --warmups 0 --output artifacts\phase6_4\paced-real
```

This is 16 runs: four real clips, two strict/adaptive pairs each, alternating order. It processed 444 frames at source cadence with **zero dropped or stale frames**, zero proposals/output starts, and zero capture-queue depth. Pooled processing p50/p95/p99/max: **21.03 / 49.97 / 55.86 / 59.62 ms**. Measured process-tree CPU was 11.36 seconds over 43.45 paced seconds (**26.15% of one core**), with sampled RSS at 79.50 MiB. Clips were predecoded into bounded RAM; decoding is not called Windows capture. This was detection-only, with neither concurrent Qwen/background vision nor game-FPS measurement. The paced tail is higher than the continuously running full-file tail; no causal speedup is claimed.

For the Warwick test clip, strict3 source-sample-release→raw ranged 18.42–39.68 ms and release→confirmation 205.62–216.10 ms (two repeats of the same recorded example). Adaptive release→confirmation ranged 217.79–222.86 ms. The small trial count and source-onset provenance preclude a general policy conclusion. Strict3 remains production default.

Confirmation→acceptance, acceptance→PCM, PCM→Windows-loopback and true capture→digital-output all have **n=0** for genuine notifications. No digital or physical audible-latency result is invented or copied from Phase 6.3. The output device was discoverable, but no audio stream or loopback recording was started for these ineligible trials.

Private outputs: `artifacts/phase6_4/{baseline,validated}/detections.json` and `frames.jsonl`; scoped `*-diagnostic.json`; `heldout-review-status.json`; `paced-real/report.json` and `trials.csv`. The independent held-out status currently has `complete: false`, `accuracy: null` and zero independently reviewed sightings.

Environment: Windows build 26200, Python 3.11.6, OpenCV 5.0.0, NumPy 2.4.6, 16 logical processors. TTS configuration was only inspected: existing speaker 53, speed 1.1, eight CPU threads and shared-mode WASAPI mapping retained. GPU utilization and physical speaker output were not measured in this phase.

## Changes and evidence rules

* `core/league_visual.py`: region naming and eligibility require explicit reviewed geometry. Eligibility recomputes containment and rejects ambiguous overlapping regions and unsupported/unknown lanes rather than trusting `in_assigned_lane` alone. The result distinguishes a verified assigned-lane region, another verified lane, unknown player position, and insufficient evidence. A verified assigned-lane observation may qualify without claiming measured distance to Yuumi. Proximity additionally requires a verified independent player position and a finite distance inside the calibrated radius.
* The same file adds immutable `ReviewedPlayerPosition` inputs for explicitly reviewed **single recorded frames**. They must match the current frame time and session epoch, have valid normalized coordinates, and explicitly exclude attached-host inference. There is no interpolation, last-seen extension, or enemy path prediction. The normal live worker never supplies these offline points.
* `core/league_visual_review.py` and `evaluate_league_vision.py`: new `prepare-review`, `review`, and `score-review` operations. A plan freezes video size/mtime, calibration SHA-256 and each cached reference SHA-256 before annotation. Disjoint tuning/held-out windows exclude prior audit intervals. Changing inputs invalidates the plan. Candidate confirmations cannot be imported as independent labels.
* `gui/league_calibration.py`: the existing dialog/decoder gains blind frame stepping, enlarged cached minimap viewing, stable sighting episode IDs, explicit absence review, uncertainty exclusions, autosaved partial progress, and a separate explicit operator finalization. Predictions and reference editing are hidden/locked during blind review. Reviewing every planned sample is required, even when no enemy is present. Normal candidate review remains available separately.
* The calibration dialog also records explicitly confirmed lane regions and exact-frame independent player points. Nothing is automatically marked human-verified. GUI state changes continue through the existing queued worker signals; there is no additional processing thread or polling timer.
* `core/league_visual_evaluation.py`: saves raw observations alongside confirmed observations so first-detection delay can be scored; records location-evidence counts; reports missed episodes and wrong-identity frames; excludes wholly unknown frames from false-positive exposure time. Unknown frames cannot silently become true negatives.
* `benchmark_league_vision_e2e.py` and `benchmarks/league_vision_e2e.py`: the existing replay whitelist now uses the same verified-geometry/proximity checks and can supply exact-frame player evidence. Export requires explicit sighting/location review. Reports add appearance→raw, raw→confirmation, confirmation→acceptance, acceptance→PCM and PCM→loopback pairs. Missing output remains missing. Synthetic controls remain explicitly isolated in the offline benchmark process.
* `tests/test_league_visual_review.py`: new regression coverage for evidence boundaries, blind splits, negatives/exclusions, input changes, identity errors, player attachment/staleness, actual Qt frame loading, and missing audio timings. Existing fixtures in `tests/test_league_visual.py`, `tests/test_league_visual_delivery.py` and `tests/test_league_vision_e2e.py` now explicitly declare their synthetic reviewed-region evidence.

No dependencies or environment configuration options were added. Existing runtime defaults remain `LEAGUE_VISION_ENABLED=False`, `LEAGUE_VISION_RIOT_APPROVED=False`, strict3 confirmation, 10 Hz, and a 0.35-second maximum frame age. Phase 6.3's cooperative cancellation/warm worker is untouched.

## Reproduction and human verification

The prepared private plan is `data/league-vision/phase6_4-blind-review/plan.json`: tuning windows 113.0–115.9, 65.0–67.9, 121.0–123.9 seconds; held-out windows 153.0–155.9, 133.0–135.9, 89.0–91.9 seconds. Each contains 30 sampled frames, for 180 planned frames total. These are selected without detector proposals. Their presence does not mean they have been reviewed. Do not edit this plan, its profile or references while labeling.

From PowerShell in `F:\Neko`:

```powershell
.\.venv\Scripts\python.exe -B evaluate_league_vision.py review --plan data\league-vision\phase6_4-blind-review\plan.json --window window-4
```

Repeat for window-5 and window-6 to complete held-out coverage; window-1 through window-3 are the separate tuning split. Click each visible enemy icon, enter its exact champion and a stable episode ID, and add its label. Reuse that ID only while the same sighting is genuinely continuous. Label every enemy on a frame before marking it reviewed. When none is visible, explicitly mark the frame reviewed without inventing a positive. Exclude an uncertain/obscured minimap frame instead of assuming absence. Verify onset against preceding absence or leave onset unknown. Finalize only after independent inspection; the tool records an operator attestation, not an externally authenticated reviewer identity.

The sampling grid cannot establish that no transient icon appeared between 10 Hz samples. Onset estimates carry that sampling uncertainty. Brief appearances between samples remain outside this measured scope.

To generate a new plan later, use a **new** output directory:

```powershell
.\.venv\Scripts\python.exe -B evaluate_league_vision.py prepare-review --video tests\fixtures\league_real_gameplay.mp4 --calibration data\league-vision\calibration.json --output data\league-vision\phase6_4-new-review --windows-per-split 3 --seconds 3 --exclude-labels data\league-vision\real-tuning-final-labels.json --exclude-labels data\league-vision\real-heldout-final-labels.json --exclude-labels data\league-vision\real-warwick-final-labels.json
.\.venv\Scripts\python.exe -B evaluate_league_vision.py detect tests\fixtures\league_real_gameplay.mp4 --calibration data\league-vision\calibration.json --output artifacts\phase6_4\new-detection
.\.venv\Scripts\python.exe -B evaluate_league_vision.py score-review artifacts\phase6_4\new-detection\detections.json --plan data\league-vision\phase6_4-new-review\plan.json --split heldout --output artifacts\phase6_4\new-heldout-score.json
```

Keep tuning results separate. Do not retune against held-out labels and then describe them as untouched validation.

## Calibrating genuine replay eligibility and speech

```powershell
.\.venv\Scripts\python.exe -B benchmark_league_vision_e2e.py calibrate
```

This opens a separate copied benchmark calibration, preserving the frozen detection profile. Load the relevant recorded frames. Select **Lane region**, draw a tight area that is visibly and independently identified as the actual bottom lane, name it, choose BOTTOM, and explicitly tick the region-verification checkbox before drawing. Do not include river/jungle/other lanes just to make a candidate qualify. Several small rectangles are safer than a broad diagonal lane approximation; overlapping regions are deliberately ambiguous.

Add/load actual sighting labels, including enough continuously verified frames to cover speech startup, then select the relevant rows. Check the explicit sighting/location confirmation and export the whitelist. For player-distance eligibility, click an independently visible player point, explicitly confirm it and record each required frame separately. **A Caitlyn face while Yuumi is attached is insufficient.** Calibrated assigned-lane evidence avoids needing such a distance claim.

After genuine eligible labels exist, with other output audio paused:

```powershell
.\.venv\Scripts\python.exe -B benchmark_league_vision_e2e.py preflight --audio --loopback
.\.venv\Scripts\python.exe -B benchmark_league_vision_e2e.py run --mode replay --scenarios verified --trials 20 --warmups 1 --audio --loopback --calibration data\league-vision\e2e-eligibility-calibration.json --manifest data\league-vision\e2e-eligibility.json --output artifacts\phase6_4\verified-audio
```

`--verified-index` selects another exported sighting. The benchmark alternates strict/adaptive order and reuses Capri's actual AdviceWorker/Kokoro/WASAPI path. Continuous current visibility, freshness, cooldowns, one pending slot and user priority still apply. A one-frame whitelist that disappears during synthesis is correctly cancelled, not extended to force speech. Repeated replays are latency samples, not new independent examples.

Mode replay measures software sample release/file-replay acquisition, **not actual Windows capture**. For the existing dedicated captured-window test, use `--mode window` and another fresh output directory; its own-window/foreground/occlusion guards remain mandatory. Neither mode enables the competitive approval gate. Known source-video times are mapped to actual runtime release times; they are not directly subtracted from monotonic audio timestamps. PCM readiness, PortAudio callback processing, estimated DAC presentation and passive Windows mix onset remain separate. None is physical speaker measurement.

Local report and CSV files contain the observed/missing stage pairs and suppressed/dropped outcomes. Listen to approved replay test speech for complete champion names, natural chunk transitions, interruption and absence of stale audio; automated text/queue tests alone cannot certify listening quality. No new audible-quality claim is made without these trials.

## Test results

```powershell
.\.venv\Scripts\python.exe -B -m pytest -q --tb=short
```

**937 passed in 135.41 seconds** on the final implementation, including existing voice/cancellation, pronunciation, one-slot notification lifecycle, stale/duplicate suppression, user priority, League Companion Mode and SVG/movement regressions. An earlier complete run also passed 937 tests. Targeted Qt tests exercised actual recorded-frame decoding and preserved frozen coverage; the tests require neither League nor Ollama nor audio hardware. They do not certify real spoken-warning quality or replace human labels.

## Remaining limitations

Independent review and location calibration remain the highest-priority next step. The existing small assistant audits cannot establish full-recording recall or a competitive false-warning rate. Darius and overlapping Karma remain known weak cases; templates and thresholds have deliberately not been tuned on the new held-out windows. No hidden positions, pathing or intent are inferred. No new GPU model, continuous gameplay recording or extra frame worker was introduced. Private recording, calibration, labels and diagnostic artifacts remain local/ignored; nothing was committed or uploaded.
