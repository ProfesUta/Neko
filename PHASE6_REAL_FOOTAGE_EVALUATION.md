# Phase 6: recorded-footage evaluation — 14 November 2026

The subsequent [Phase 6.1 report](PHASE6_1_DETECTION_OPTIMIZATION_REPORT.md) documents notification lifecycle fixes, confirmation-policy comparisons and paired processing measurements. The original baseline and evidence below are preserved.

The supplied recording was decoded again with all ten references, then rerun after a small OpenCV matching change. No YOLO, Ollama, capture worker, speech engine, sprite, docking or teleport change was needed. Competitive capture and alerts remain disabled: both `LEAGUE_VISION_ENABLED` and `LEAGUE_VISION_RIOT_APPROVED` evaluated to `False`. The local profile remains muted.

**Evidence and annotation limits.** This is real recorded gameplay, but the accuracy labels are an **assistant visual audit**, not independently verified human ground truth. The existing human-label file contained no sightings. Unlabelled, enlarged nearest-neighbor minimap sheets were inspected before consulting the detector predictions. No model predictions were copied into ground truth. The JSON records the reviewer and limitations. Results apply only to the explicit windows below, not the whole recording, another HUD configuration, another match, or live play. Human confirmation remains necessary.

The video is 1920×1080, 60 FPS, 253.867 seconds. All 2,539 samples at 10 FPS were processed, using the measured 262×257 minimap. The primary audit covers 93 frames / 9.3 sampled seconds at `[0.5,2.5]`, `[3,5.9]`, `[8,10.9]`, `[18.8,19.9]`. It contains 100 champion-frame positives. Ambiguous overlapping bot portraits are explicitly excluded during 10.0–10.9 and 18.8–19.9 seconds; predictions in these rectangles are neither true nor false positives.

A separate **Warwick-only** audit covers 81 frames / 8.1 sampled seconds at `[0.5,2.5]`, `[180,181.4]`, `[213,217.4]`. It includes 27 visible Warwick-positive frames, with appearances at video 180.5 and 214.4 seconds, confirmed against the preceding absent samples. Other champions are not scored in this audit. The repeated early negative window means these two audit durations must not be added as independent exposure.

Tuning used separate 6.0–7.9, 13.0–14.9 and 21.0–22.9 second windows, including missed positives and explicit ambiguous-region exclusions. The held-out windows were not used to select the smoothing/cutoff. References and tuning still come from the same recording: this is not a cross-recording generalization test.

**Changes and measured accuracy.** Native 14–16 pixel icons differ after compression, subpixel movement, borders and overlap. The original .92 cutoff with unsmoothed references missed every labelled positive in the audit, although it emitted eight candidates elsewhere in the clip. Add optional `minimap_blur_sigma`, default **0.0**. A calibrated .8-pixel Gaussian filter is applied once to each frame, shared by every champion; reference variants are filtered/cached once at initialization. Existing color verification, roster ambiguity margin, three scales (.9/1/1.1) and strict three-consecutive-sample confirmation remain intact. Missing samples still immediately remove current observations.

For **this local recording profile only**, `.8` smoothing and `.85` similarity cutoff recovered positives without audit false positives. The global .92 default is unchanged. The ambiguity margin remains .08. A similarity value is not a probability.

| Reviewed scope | Before TP / FP / FN | After TP / FP / FN | After precision | Before → after recall | False candidate episodes/minute |
|---|---:|---:|---:|---:|---:|
| Primary audit, all five enemies | 0 / 0 / 100 | 62 / 0 / 38 | 100% | 0% → 62.0% | 0 in this small audit |
| Separate Warwick-only audit | 0 / 0 / 27 | 19 / 0 / 8 | 100% | 0% → 70.4% | 0 in this small audit |

Baseline precision is undefined because it predicted no positives in these windows. Zero observed false positives over such short exposure does **not** establish a low production false-alert rate. These are candidate-sighting rates, not spoken-alert rates. The profile has no verified Yuumi position or named lane regions, so no speech proposals were eligible; live speech and audio latency were not exercised.

| Champion, primary audit | TP / labelled positive frames | Recall | Remaining problem |
|---|---:|---:|---|
| Darius | 0 / 12 | 0% | Partial overlap with Vladimir and a border-contaminated reference |
| Galio | 36 / 50 | 72.0% | Compression and overlapping Taliyah icon interrupt strict confirmation |
| Ziggs | 23 / 28 | 82.1% | Very small face, camera lines and confirmation delay |
| Karma | 3 / 10 | 30.0% | Short visibility and overlap; cannot treat ambiguous bot portraits as confirmed |
| Warwick | No positives in this primary window | Not measurable here | Separate positive audit above |

The final complete pass produced **109 candidate episodes**: Galio 56, Karma 36, Ziggs 12, Warwick 3, Darius 2. Repeated fragments are not unique sightings or announcements. Warwick candidates began at video 180.5, 188.1 and 214.4 seconds; the middle episode is outside the held-out audit. A separate candidate-conditioned spot review inspected all 109 first-confirmation context crops: 108 show the proposed champion, zero clear wrong identities were found, and one Ziggs crop at 214.3 seconds remains unconfirmed because of a yellow ping overlay. Those reviews do not verify every subsequent sample or missed sightings. They are saved separately with `annotation_complete=false`; exhaustive whole-clip precision/recall remains unknown.

Rejected experiments: stronger 1.2-pixel smoothing with a .85 cutoff produced seven false-positive frames in the tuning audit. Three-of-four confirmation gave no tuning improvement, so it was reverted. A new Warwick crop at 188.8 seconds scored 18/27 held-out positive frames versus 19/27 using the existing official reference, so it was not adopted. No prediction is carried through an absent frame to improve recall artificially.

**Data Dragon comparison.** All ten official portraits were tested in separate profiles without overwriting the eight existing recorded-icon crops. The apples-to-apples smoothed comparison changes only the reference set. Raw official portraits are useful baselines, not universally reliable drop-in minimap templates.

| Reference set | Smoothing / cutoff | Primary audit TP / FP / FN | Warwick audit TP / FP / FN |
|---|---|---:|---:|
| Eight recorded + two official, original | Off / .92 | 0 / 0 / 100 | 0 / 0 / 27 |
| All ten official, original | Off / .92 | 0 / 0 / 100 | 0 / 0 / 27 |
| Eight recorded + two official, final profile | .8 / .85 | 62 / 0 / 38 | 19 / 0 / 8 |
| All ten official, exploratory comparison | .8 / .85 | 67 / 0 / 33 | 19 / 0 / 8 |

The all-official smoothed profile reaches 67% primary recall (Galio 38/50, Ziggs 26/28, Karma 3/10, Darius 0/12), with 100% precision in this small audit. Processing p50/p95/p99/max was 21.8136 / 22.6683 / 23.7965 / 31.8254 ms, 2,539 samples. This is encouraging, but the five-frame difference on one recording is insufficient to establish general superiority or justify silently overwriting working crops. The alternative is retained as `evaluation-official-smoothed-calibration.json` for independent human/new-clip validation. Warwick's existing official reference is sufficient for the two tested appearances after calibrated preprocessing. The final mixed profile preserves the eight recorded references plus official Warwick and Yuumi. The Yuumi-attached marker contains Caitlyn's face; matching it does not independently establish Yuumi's position.

**Processing measurements.** Windows build 26200, 16 logical processors, Intel64 Family 6 Model 165; installed OpenCV 5.0.0 / NumPy 2.4.6. One OpenCV CPU thread. Runs were serial, without pytest or another detector benchmark running concurrently. These are actual CPU executions on recorded pixels, not synthetic accuracy results. Existing bounded metrics retain the most recent 2,048 samples for percentiles; counts/maxima cover all 2,539. Percentiles use the project's floor-order statistic, which is coarse for the tiny onset sample counts.

| Measurement | Samples | Before p50 / p95 / p99 / max ms | After p50 / p95 / p99 / max ms |
|---|---:|---|---|
| Full video: region extraction + matching + temporal processing | 2,539 each | 21.7110 / 22.5169 / 28.5174 / 35.0109 | 22.2065 / 23.0423 / 26.0217 / 38.6627 |
| Identical lossless cached minimaps, excluding decode | 2,539 each | 17.4439 / 18.1822 / 19.0407 / 29.7453 | 21.6442 / 22.3523 / 24.6409 / 37.4217 |
| Final video decode, separate from detector | 2,539 | — | 27.6364 / 42.8359 / 46.3465 / 60.0696 |
| Final notification-proposal construction, not TTS | 2,539 | — | .0064 / .0089 / .0130 / .0352 |
| Known appearance → confirmed sighting, primary audit | 3 detected onsets | No detection | 200 / 200 / 200 / 300 |
| Known appearance → confirmed sighting, Warwick audit | 2 detected onsets | No detection | 200 / 200 / 200 / 200 |

This is an **accuracy improvement with processing cost**, not a latency optimization claim. Full-video detector median rose about 2.3%; the cached comparison rose about 24.1%. These are separate single-run observations, not confidence-bounded causal estimates. Detection processing remained well below the 150 ms/frame goal. The 200–300 ms appearance delays are sampled video-time delays at 100 ms resolution, exclude processing/scheduling/audio, and only cover known onsets that were detected. Individual primary-audit delays were 200, 200 and 300 ms; Warwick delays were 200 and 200 ms. The small-sample percentile estimator should not hide the 300 ms maximum or be treated as a stable population estimate. Missed or left-censored episodes cannot produce valid onset latency.

Full final decode/analysis consumed 135.00 CPU seconds over 135.18 wall seconds (~99.87% of **one** logical core) while running unpaced, faster than video playback. The pure cached final-settings run used 55.31 CPU / 55.50 wall seconds; cached baseline 45.11 / 45.18. This is not 100% whole-machine usage or a measured active-game FPS/CPU cost. Actual League/vision/Qwen contention and GPU usage were not measured. Final no-cache sampled peak process RSS was ~89.3 MiB. The baseline deliberately saved a ~513 MB lossless pixel cache and thus had much higher mapped RSS; that difference is not a claimed memory optimization.

**Implementation files changed in this evaluation pass.** Existing uncommitted Phase 5/6 files were preserved.

| File | Purpose |
|---|---|
| `core/league_replay.py` | Validated optional cached reference/frame smoothing |
| `core/league_visual.py` | Share prepared pixels across all champion comparisons |
| `core/league_visual_evaluation.py` | Explicit bounded lossless cache, identical-pixel reruns, disjoint reviewed windows, ignored regions, champion scope, per-champion scoring, onset provenance/grouping, CPU/timing output |
| `evaluate_league_vision.py` | Opt-in `--cache-minimap` and explicit equivalent-geometry annotation reuse |
| `benchmark_league.py` | Reuse the existing benchmark entry point for cached real-footage comparisons |
| `gui/league_calibration.py` | Do not infer onset from a confirmation frame; retain disjoint coverage when saving |
| `examples/league-vision-calibration.json` | Document disabled-by-default smoothing |
| `tests/test_league_visual.py`, `tests/test_league_vision_cli.py` | Cache parity/cancellation/validation, privacy defaults, geometry/cadence, label scope/latency, smoothing/input preservation and CLI regressions |
| `PHASE6_LEAGUE_VISION_REPORT.md`, this report | Link the real-footage evidence and remaining limits |

The local ignored `data/league-vision/calibration.json` now uses the evaluated settings. `evaluation-baseline-calibration.json` preserves the old profile. No model, runtime dependency, inference scheduler, voice file, pronunciation dictionary, SVG, teleport renderer or League Companion movement path was modified.

**Verification.** Final complete suite: **817 passed in 112.18 seconds**, using `python -B -m pytest -q -p no:cacheprovider`. The final focused detector/evaluation/CLI suite passed 86 tests. Logs: `artifacts/phase6/real-evaluation-verified-all-tests.log` and `real-evaluation-targeted-tests.log`. A preceding complete run passed 816 tests before adding the final CLI regression. One initial new smoothing test used the original .90 threshold against its synthetic boundary pixels; it was corrected to test the actual calibrated .85 setting, and all subsequent focused/full runs passed. `git diff --check` passed, with only Git's normal LF/CRLF notices. Existing voice, League Companion, SVG and teleport regression tests remained in the full suite.

**Local validation and review.** These commands use existing private results. The JSON files include provenance, scope and exact counters. Do not declare the entire recording exhaustively labelled when reviewing only selected sightings.

```powershell
# View final candidates and primary audit labels; inspect missed sightings as well.
.\.venv\Scripts\python.exe -B evaluate_league_vision.py label --detections "data\league-vision\real-evaluation-final\detections.json" --labels "data\league-vision\real-heldout-final-labels.json"

# Review Warwick separately, without treating other champions as labelled negatives.
.\.venv\Scripts\python.exe -B evaluate_league_vision.py label --detections "data\league-vision\real-evaluation-final\detections.json" --labels "data\league-vision\real-warwick-final-labels.json"

.\.venv\Scripts\python.exe -B evaluate_league_vision.py evaluate "data\league-vision\real-evaluation-final\detections.json" --labels "data\league-vision\real-heldout-final-labels.json" --output "artifacts\phase6\primary-local-score.json"
.\.venv\Scripts\python.exe -B evaluate_league_vision.py evaluate "data\league-vision\real-evaluation-final\detections.json" --labels "data\league-vision\real-warwick-final-labels.json" --output "artifacts\phase6\warwick-local-score.json"

# Rerun actual matching against identical lossless recording pixels. Use a fresh output folder each run.
.\.venv\Scripts\python.exe -B benchmark_league.py --real-vision-report "data\league-vision\real-evaluation-baseline\detections.json" --real-vision-calibration "data\league-vision\evaluation-final-calibration.json" --real-vision-output "data\league-vision\local-repeat-01" --output "artifacts\phase6\local-repeat-01.json"

# Fully decode the video again. --cache-minimap is optional and explicitly saves cropped private pixels (max 1 GiB).
.\.venv\Scripts\python.exe -B evaluate_league_vision.py detect "tests\fixtures\league_real_gameplay.mp4" --calibration "data\league-vision\calibration.json" --output "data\league-vision\local-video-repeat-01"
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider
```

Saved evidence is local/ignored: `data/league-vision/real-evaluation-{baseline,final,smoothed,cached-baseline,official,official-smoothed}/`, `real-{heldout,warwick,tuning}-*-score.json`, `real-candidate-spot-review.json`, frozen evaluation profiles, audit labels and `artifacts/phase6/real-audit/` contact sheets. Normal operation creates no frame cache and uploads nothing. No continuous live alert capability was enabled.

Next work is independent human review and longer labelled clips covering clear/occluded Warwick, Darius and Karma, additional HUD scales and camera overlays. Keep tuning and evaluation footage separate. Improve clean icon exemplars or calibrated crop/scale handling before considering a heavier detector. Strict template matching remains unreliable for heavily overlapping/hidden portraits; no current position, proximity or approach direction should be inferred from a previous sighting.
