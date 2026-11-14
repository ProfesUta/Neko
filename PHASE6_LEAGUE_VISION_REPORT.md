# Phase 6 — local League visual awareness and recording validation

Implemented inside Capri's existing application. Live competitive capture and automatic sighting speech remain **disabled**. The new recorded-video workflow works without enabling that gate. No Qwen/Kokoro model, voice asset, SVG, teleport renderer, League docking behavior or inference-priority implementation was replaced.

The supplied real recording has now been rerun with all ten references. See [PHASE6_REAL_FOOTAGE_EVALUATION.md](PHASE6_REAL_FOOTAGE_EVALUATION.md) for the scoped assistant visual audit, Warwick sightings, direct Data Dragon comparison, processing/CPU measurements, new tests and remaining independent human-review requirements. Historical synthetic results below remain separate.

## Architecture and evidence rules

`LeagueClient` publishes a small immutable assigned-position context using its existing LCU/Live Client updates. TOP, JUNGLE, MIDDLE, BOTTOM and UTILITY are supported; UTILITY maps to the bottom lane for calibrated lane-region membership. Assignments survive loading, reconnection and brief API outages. A reliable session ending or new match clears the old assignment. An unavailable role remains unknown unless an explicit fallback is configured. Only CLASSIC on map 11 enables this detector; no champion-based role guesses.

OpenCV Template Matching is the primary minimap detector. The existing replay matcher is reused, with cached scaled references, a shared grayscale search image, BGR verification and a competing-roster similarity margin. A complete known ally/enemy roster and reference for every champion are required. Three nearby, consecutive visible samples confirm a candidate. Disappearance, a time gap, a large jump, a context change or a match change resets confirmation. A past sample never produces an extrapolated fog-of-war position.

Named locations and lane membership come from measured minimap regions. Approximate proximity is normalized minimap distance to a separately matched **allied player champion icon**; without that icon, proximity is unknown. Team orientation is recorded explicitly and does not silently flip captured pixels. Similarity is labelled as template similarity, **not a calibrated probability**.

Optional viewport detection uses tightly cropped **champion-name** nameplates plus nearby enemy-colored health bars. It does not recognize arbitrary 3D models, summoner names or unseen skins. It stays disabled until all roster nameplate references, a viewport ROI and a scoreboard blocker are supplied. HUD/minimap exclusions, identity-margin checks and temporal confirmation reject distractors. A scoreboard blocker suppresses observations while that interface is visible. There is no movement-direction or approaching claim.

The opt-in runtime reuses `FrameHistory`/`LiveScreenStream`, the existing window enumerator, MSS and Win32 client geometry. Capture runs in its owning thread at a target 10 FPS, only for calibrated regions of the foreground League game window. Per-monitor DPI awareness resolves physical client pixels, including negative monitor origins; normalized calibration handles proportional resolution changes. Changed aspect ratios require recalibration. Occlusion, minimization, missing game data, roster mismatch and stale frames suspend observations. The raw queue holds at most two frames / 8 MiB; consumers take the newest fresh frame and count skipped sequences. These CV frames are not saved, uploaded or sent to Ollama in normal operation; Capri's separate existing screen-understanding feature is unchanged.

There is one optional CV processing worker, with one instance of the existing capture-thread component. No additional League polling, API connection, movement timer, GPU scheduler or TTS engine was added. Foreground conversation and its existing quiet period defer CV processing. Capture remains bounded. Queued Qt signals carry state changes; no network, model inference or video decoding runs on the GUI thread.

Current high-similarity candidate proposals enter the existing `LeagueAnnouncement` → one-slot `LeagueVoicePolicy` → existing voice worker/Kokoro route. They use priority 0, per-champion cooldowns, a mute setting, one-second lifetime, and a 350 ms current-frame guard. Old match epochs, disappeared sightings and obsolete positions fail the existing pre-playback guard. User requests retain priority. Existing advice settings still apply. No automatic sighting invokes Qwen.

## Calibration and manual labels

Open the editor inside Capri with **Ctrl+Shift+L**, ask to open League vision calibration, or run:

```powershell
Set-Location F:\Neko
.\.venv\Scripts\python.exe -B evaluate_league_vision.py label --video "F:\Neko\tests\fixtures\league_real_gameplay.mp4" --calibration "F:\Neko\data\league-vision\calibration.json"
```

**Automatic icon setup:** manual portrait cropping and confirmation are optional for using the detector. On an explicit offline analysis, missing references are downloaded from Riot's pinned Data Dragon champion catalog and cached. Existing working recording crops are preserved. The editor also has **Get champion icons**; standalone preparation is available with `python -B evaluate_league_vision.py icons --calibration data/league-vision/calibration.json`. `--replace` is an explicit opt-in to replace existing crops with official baselines; normal detection does not do that. Downloads use the existing static-reference HTTP client, bounded public PNG validation, cancellable/coalesced requests and atomic raw-asset writes. They never happen per frame or through Ollama. A cached reference does not certify detection accuracy: low-confidence matches are still rejected, and real accuracy remains unavailable without labels. No manual review gate was added to detection or automatic notification proposals.

Automatic setup verification: the current private calibration returned **ready, 10 preserved references, no manual cropping required**, without rewriting the file or invalidating its existing recording report. A separate private profile with no templates successfully bootstrapped all **10 official references** from the existing Riot cache and loaded the detector. The original working profile was byte-for-byte preserved. Automated coverage includes cached/offline operation, missing-only versus explicit replacement, malformed/oversized responses, failed requests, filename traversal rejection, request coalescing, cancellation, concurrent calibration edits, automatic video preparation and the reused Qt worker. Complete suite: **801 passed in 111.54 seconds** (`artifacts/phase6/automatic-icons-full-tests.txt`); final targeted verification after help-text/read-consistency cleanup: **87 passed in 3.92 seconds** (`automatic-icons-final-tests.txt`). No new voice, GPU, match-FPS or real-accuracy measurement is claimed for this setup change.

The provided recording is present and readable: 1920×1080, 60 FPS metadata, 15,232 frames, approximately 253.87 seconds. Its first frame was successfully loaded through the real Qt review interface. The user subsequently confirmed the ten-champion roster and their Yuumi Support role; these are saved in the private local calibration, with Support represented as UTILITY. Eight references use visually inspected recorded minimap crops. Warwick and Yuumi currently use explicitly marked official static baseline references, without a verified clean minimap appearance in the sampled calibration frames. `_template_sources` records each reference's provenance. Public square portraits were cached once from [Riot Data Dragon](https://developer.riotgames.com/docs/lol#champion-square-assets); no match-time download was added. **Real detection accuracy remains unmeasured:** exhaustive reviewed sighting labels are still required. No automatic candidate is treated as human confirmation.

1. Load a recorded frame. Use the JSON setup panel to enter the actual ally/enemy roster, assigned role, player champion, team orientation, measured resolution, HUD scale, threshold, radius and cooldown. `examples/league-vision-calibration.json` documents the schema; its empty roster is intentional.
2. Choose **Minimap ROI**, then drag its bounds on the preview. Press **Get champion icons**, or simply **Generate candidates** to fill missing references automatically. Every roster champion, including allies/distractors, gets a cached reference. Optional recording-specific tuning remains available via **Champion minimap icon**, an exact roster champion name and a tight crop; local image paths may also be supplied in JSON. A minimap crop should be 32–1024 reference pixels per side; icon references 6–128 pixels.
3. Optionally drag **Lane region** rectangles inside the minimap, name each region and choose TOP/MIDDLE/BOTTOM. Regions must not overlap if an unambiguous location is expected. Radius is normalized minimap distance, not game units.
4. To test viewport recognition, enable champion-name display in the recording/calibration, provide every roster nameplate reference, draw the viewport, exclude HUD regions, and crop a textured scoreboard blocker. Otherwise leave viewport detection disabled. Enemy health-bar color can be red or yellow.
5. Save calibration, then click **Generate candidates**. It saves pending editor changes and starts one cancellable background file-analysis task. In-app analysis pauses for foreground work and stops if a League session starts. Repeated clicks do not start competing tasks in this editor.
6. Optional validation: in **Confirm / reject candidates**, select a row to load its exact confirmation frame with champion, similarity, box and video time. A confirmation adds only that inspected frame to ground truth. Rejection removes its auto-added label; it does not delete independent manual labels. This step is not required to run detection.
7. Only if measured accuracy is wanted, use **Ground truth / missed sightings** to label source-relative centers, missed positives, truly continuous visible intervals, reviewed coverage and sources. Do not fill fog-of-war gaps. Mark the exhaustive-annotation checkbox only after labelling sightings **and absences**, then save labels JSON. Merely confirming suggested detections cannot establish recall.
8. The optional accuracy report scores spatial associations before champion identity, false positive episodes per minute, frame-level precision/recall, identity, known proximity and appearance-to-confirmation delay. It refuses changed recordings/calibrations. Incomplete/no labels leave accuracy unavailable; processing and candidate generation still work.

All runtime profiles, references, labels and reports default to ignored `data/league-vision/`. The supplied real MP4 is explicitly ignored by Git. No raw video/frame data is committed. Annotated images are saved only with explicit `--debug-overlays` (maximum 500), or by this task's explicit local visual validation.

## Exact recording validation commands

On the first run, `data/league-vision/calibration.json` does not exist until calibration is saved in the editor. `detect` now reports the exact missing path and prints an executable setup command instead of a traceback. Run the `label` command above first; it automatically loads the supplied video frame. Draw the measured minimap bounds, enter the verified roster/icon references and click **Save calibration** (or **Generate candidates**). An illustrative/example profile is not a verified calibration and is not silently substituted. Missing recordings are reported separately by path. Detection failure exits with code 2 and does not open an unsolicited GUI.

The first-run fix adds purpose/path-aware file errors in `core/league_replay.py`, labels calibration inputs in `core/league_visual.py` and `core/league_visual_evaluation.py`, and adds safe CLI error handling/setup commands and editor frame loading in `evaluate_league_vision.py`. Eight regression cases in `tests/test_league_vision_cli.py` cover these paths, quoting, GUI startup order and preservation of unexpected programming errors. Targeted validation: **58 passed**. Complete suite after this fix: **789 passed in 110.41 seconds** (`artifacts/phase6/calibration-fix-tests.txt`). The actual failed command was also rerun: it now identifies the missing calibration and supplies the setup command, without creating a guessed profile or an output folder. No synthesis, live capture or detector algorithm changed in this fix.

After saving the measured calibration:

```powershell
.\.venv\Scripts\python.exe -B evaluate_league_vision.py detect "F:\Neko\tests\fixtures\league_real_gameplay.mp4" --calibration "F:\Neko\data\league-vision\calibration.json" --output "F:\Neko\data\league-vision\real-review-01"
.\.venv\Scripts\python.exe -B evaluate_league_vision.py label --video "F:\Neko\tests\fixtures\league_real_gameplay.mp4" --calibration "F:\Neko\data\league-vision\calibration.json" --detections "F:\Neko\data\league-vision\real-review-01\detections.json" --labels "F:\Neko\data\league-vision\real-labels.json"
.\.venv\Scripts\python.exe -B evaluate_league_vision.py evaluate "F:\Neko\data\league-vision\real-review-01\detections.json" --labels "F:\Neko\data\league-vision\real-labels.json" --output "F:\Neko\data\league-vision\real-accuracy.json"
```

Choose a new output folder for another run. To save debug images, add `--debug-overlays` to `detect`. No sound or speech is played by detection/evaluation. Optional explicit controlled timing sound, using the existing playback path:

```powershell
.\.venv\Scripts\python.exe -B evaluate_league_vision.py test-sound --samples 20
```

This reports available software callback/DAC presentation estimates. It does **not** measure physical acoustic output, champion detection or Kokoro voice latency, and never starts recording.

## Configuration defaults

| Setting | Default / override |
|---|---|
| `LEAGUE_VISION_ENABLED` | False; `CAPRI_LEAGUE_VISION` |
| `LEAGUE_VISION_RIOT_APPROVED` | False; `CAPRI_LEAGUE_VISION_APPROVED` |
| `LEAGUE_VISION_FPS` | 10; `CAPRI_LEAGUE_VISION_FPS` |
| `LEAGUE_VISION_FALLBACK_ROLE` | UNKNOWN; `CAPRI_LEAGUE_FALLBACK_ROLE` (live API context) |
| `LEAGUE_VISION_CALIBRATION` | `DATA_DIR/league-vision/calibration.json`; `CAPRI_LEAGUE_VISION_CALIBRATION` |
| `LEAGUE_VISION_MAX_FRAME_AGE` | 0.35 seconds |
| `LEAGUE_VISION_ALERT_LIFETIME` | 1.0 second |
| Profile fields | 10 FPS, threshold .92, margin .08, radius .12, cooldown 8 s, alerts muted, fallback UNKNOWN |

The profile's assigned/fallback roles describe an **operator-labelled recording**. Live fallback uses the environment/config setting above and remains separately labelled as a user fallback. `CAPRI_LATENCY_METRICS=1` exposes bounded worker timing summaries through the status tool; it does not create periodic GUI updates. Percentiles retain at most 2,048 recent samples per metric; counts and maxima cover the run.

## Measured performance

Windows, Intel Core i7-10700F @ 2.90 GHz, 16 logical processors; installed OpenCV 5.0.0.93 / NumPy 2.4.6. CV benchmarks use one OpenCV CPU thread. They ran separately from pytest. Before/after replay fixtures and settings were identical. Replay timing includes the first invocation; the all-enemy test uses ten warm-up frames, nameplates five. No GPU, real League client, capture or audio was involved in these timings.

| Actual CPU processing on synthetic fixtures | Samples | Before p50 / p95 / p99 ms | After p50 / p95 / p99 / max ms |
|---|---:|---|---|
| Existing single-target replay matcher, visible icon | 250 | 26.5822 / 34.1010 / 36.3160 | 8.2851 / 10.6699 / 11.1383 / 27.4224 |
| Existing matcher, absent icon | 250 | 26.2020 / 33.7434 / 35.4690 | 7.9322 / 10.2195 / 10.7100 / 11.0386 |
| Five enemies + player, 1080p source / 10 references | 250 warm | New capability | 49.9043 / 60.0414 / 64.5299 / 68.4447 |
| Deterministic notification proposals | 250 | New capability | .0089 / .0141 / .0333 / .0505 |
| Five calibrated enemy nameplates + health bars | 100 warm | New capability | 17.8189 / 22.5444 / 25.2720 / 25.6401 |

The existing single-target matcher median fell approximately **68.8%**; all 250 positive and 250 absence checks still passed. The all-enemy fixture returned 1,250/1,250 expected confirmed observations, and the viewport fixture 500/500. Those are constructed-fixture checks, **not real champion accuracy**. The unpaced combined new-feature microbenchmark used 14.96875 CPU seconds over 15.30275 wall seconds (~97.8% of one logical core). It intentionally runs faster than real-time and is not an active-game CPU/FPS measurement. Actual capture, live FPS, GPU utilization and detection-to-audible-TTS latency remain unmeasured.

The existing saved synthetic MJPEG footage was also decoded end-to-end: 20 frames / 4 FPS, two separate visible episodes, no detections during absence. Processing p50/p95/p99/max was 1.9640 / 3.7453 / 3.7453 / 5.7292 ms, decode p50/p95 .1257/.2347 ms. Fixture-labelled results: 7 true-positive frames, 0 false positives, 4 false negatives, precision 100%, recall 63.64%, identity 7/7, known proximity 7/7. The first two visible frames of each episode are deliberately withheld for temporal confirmation: both appearance-to-confirmation delays were **500 ms at this source's 4 FPS**. This explicitly accounts for confirmation delay rather than hiding it in an inflated accuracy score. At 10 FPS, three samples span 200 ms before processing/queue/synthesis time; this is an engineering interval, not a measured audible response.

Raw ignored outputs: `artifacts/phase6/before.json`, `after.json`, `vision-final.json`, `synthetic-accuracy.json`, `synthetic-analysis/`, `review-ui.png`, `real-frame-ui.png`. Synthetic accuracy uses known construction labels, not user-reviewed real footage. The real frame-preview smoke check succeeded; no real champion accuracy is claimed.

Reproduce portable processing benchmarks and tests:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-replay.txt
.\.venv\Scripts\python.exe -B benchmark_league.py --seconds 1 --companion --replay --vision --output artifacts/phase6/local-benchmark.json
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider
```

## Validation and remaining limits

The test suite covers role persistence/reset/fallback, concurrent immutable snapshots, malformed calibration, minimap scaling, known-team separation, obscuration, ambiguity/low similarity, temporal resets, no absence extrapolation, verified-player proximity, scoreboard/HUD rejection, enemy health bars, raw queue bounds, physical negative coordinates, occlusion/focus guards, the live approval gate, recovery after a temporary data outage, new-match invalidation, optional sound caching, manual confirm/reject/missed labels, stale calibration/video rejection and worker teardown. Existing League Companion, speech, cancellation, SVG and teleport tests remain in the complete suite.

Final complete run: **781 passed in 119.17 seconds**, using `.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider`. This includes 36 new Phase 6 tests. The expanded Qt workflow test generates candidates off-thread, confirms/rejects, saves labels, generates an accuracy report and cancels an active decoder. The final test log is `artifacts/phase6/verified-all-tests.txt`. An earlier run exposed an exact floating-point timestamp assertion after adopting decoder presentation timestamps; it now uses a tolerance. A targeted GUI check also caught a misplaced new provenance guard; the guard now applies to confirmation, and the final complete run passed afterward.

The user's fractional minimap selection exposed a further geometry issue: the detector rounded the ROI height independently to 258 pixels while the editor's rounded top/bottom edges yielded a 257-pixel crop. That introduced resampling before reference matching. `core/league_replay.py` now derives reference dimensions from the same rounded edges as capture/editor extraction. The new fractional-edge regression in `tests/test_league_visual.py` verifies exact pixel preservation and successful temporal identification. Targeted validation: **93 passed**. Complete suite after this correction: **790 passed in 179.61 seconds** (`artifacts/phase6/setup-check/full-roster-geometry-tests.txt`). This suite ran alongside offline recording validation; that run's raw processing timings are not a controlled latency benchmark or a before/after accuracy comparison. Earlier synthetic benchmark tables above remain separate measurements.

The recorded Yuumi-attached marker can contain Caitlyn's portrait. The Caitlyn face crop is recorded as Caitlyn evidence, with an explicit note that it does not establish Yuumi's position after attachment changes. Player proximity remains unknown unless the actual player reference can be matched independently. Named lane regions and viewport nameplate references are still unconfigured for this recording; the detector does not invent these or enable live alerts.

The subsequent complete pass over the supplied recording analyzed **2,539 frames at a 10 FPS source sampling cadence** and produced **8 unreviewed candidate episodes**: Galio 3, Karma 4, Ziggs 1. It did not report a Warwick sighting. The latest output is private `data/league-vision/real-review-02/detections.json`; `labels.json` beside it contains the recording paths and coverage with **no labels/reviews and exhaustive annotation disabled**. The earlier `real-review-01` is the pre-correction pass, retained rather than overwritten. Neither result establishes recall or real-footage accuracy. For transparency, observed processing p50/p95/p99/max during the latest pass was 34.0451 / 45.8565 / 51.4200 / 56.6330 ms over 2,539 frames (2,048 retained percentile samples), with 210.88 s wall time; pytest was running concurrently, so these are load-dependent observations, not a controlled performance comparison or audible latency.

Open the latest candidate list for explicit review:

```powershell
.\.venv\Scripts\python.exe -B evaluate_league_vision.py label --detections "data\league-vision\real-review-02\detections.json" --labels "data\league-vision\real-review-02\labels.json"
```

Offscreen visual checks loaded the synthetic overlay and the supplied real recording into the actual dialog, saved local previews, and closed their decoding workers. `git diff --check` passed (only normal LF/CRLF notices). A separate diff check confirmed no modifications to `voice/`, sprite assets, `gui/sprites.py`, `gui/teleport.py`, `core/inference_priority.py` or `core/model_client.py`. No orphan Python process remained after tests.

Manual checks still needed: calibration and exhaustive labels on the supplied real recording; other resolutions/HUD scales/skins/nameplate settings; actual multi-monitor Windows capture; busy/occluded/minimized game windows; live-match CPU/FPS and voice timing after the feature has been approved. Identical champion portraits across opposing teams, clones/decoys, overlays, partial icon occlusion, camera changes and compressed icons can be ambiguous. The detector rejects unsupported or ambiguous evidence rather than claiming a location. It cannot tell whether a player noticed a sighting. MSS is the existing GDI-based stack; exclusive fullscreen/black captures may require borderless mode. No DXGI/WGC dependency or anti-cheat access was added.

Both runtime switches stay off. An environment flag is an operator attestation, **not Riot authorization**. Before competitive deployment, submit the product/change and obtain the required review; local visible pixels alone do not establish permission for an automatic positional-alert feature. Current [Riot general policies](https://developer.riotgames.com/policies/general) require product/change review and prohibit unfair advantages. The [League developer policy](https://developer.riotgames.com/docs/lol#game-integrity) also restricts unfair or hidden-information uses. This implementation does not read process memory, reveal hidden identities/positions, bypass anti-cheat, automate gameplay input, calculate routes or dictate tactical decisions.

## Changed files for Phase 6

| File | Change |
|---|---|
| `core/league_lane.py` | Immutable assigned-role context and lifecycle |
| `core/league_capture.py` | Gated cropped game-client capture and physical coordinate helpers |
| `core/league_visual.py` | Calibration, cached minimap/optional nameplate detection, temporal confirmation, current-only notification proposals |
| `core/league_visual_evaluation.py` | Automatic missing-icon preparation, local video sampling, explicit overlays, JSON labels and optional accuracy/latency scoring |
| `core/league_icons.py`, `core/league_knowledge.py` | Preserve current references; cache/resize missing official icons through the existing static-reference connection |
| `core/league_visual_sound.py` | Optional cached controlled-test tone through existing playback |
| `gui/league_vision.py` | Dormant-by-default CV worker/service, Qt lifecycle, recovery and freshness guard |
| `gui/league_calibration.py` | Recorded-frame crop/calibration, confirm/reject and missed-sighting editor |
| `evaluate_league_vision.py` | Detect/label/evaluate/test-sound CLI |
| `examples/league-vision-calibration.json` | Illustrative measured-calibration schema |
| `core/league.py` | Existing-data role publication and guarded visual notifications; no extra requests |
| `core/league_replay.py` | Reuse/optimize the existing template matcher |
| `core/live_screen.py` | Extend existing bounded history to raw cropped frames |
| `gui/workers.py`, `gui/window.py` | Queued role state, service/cleanup, calibration shortcut, evidence-source label |
| `tools/league.py`, `core/chat.py`, `core/conversation.py` | Scoped calibration/status tools and narrow action routing |
| `config.py`, `.gitignore` | Safe defaults and local/private artifact exclusions |
| `benchmark_league.py` | Extend existing benchmark with all-enemy/nameplate processing |
| `requirements-replay.txt` | Clarify existing optional OpenCV purpose; version unchanged |
| `tests/test_league_visual.py`, `tests/test_league_icons.py`, `tests/conftest.py` | Detection, automatic icon caching/coalescing/cancellation/security, Qt workflow and isolation from real captures/models |
| `LEAGUE_COMPANION_GUIDE.md`, this report | Entry points, evidence and limitations |

No new dependency beyond the existing optional headless OpenCV package was introduced. NumPy, MSS, HTTP clients, PySide6 and playback remain the existing project dependencies. Earlier uncommitted Phase 5 knowledge/replay work was preserved.
