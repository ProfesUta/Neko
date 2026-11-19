# Phase 7 — live League observation

19 November 2026. Implemented and exercised against the foreground, borderless Practice Tool on this Windows workstation. This is **silent local observation**, not approval to deploy competitive warnings. Qwen, Kokoro, pronunciation, native audio streaming/cancellation, League sprites, docking and teleportation are unchanged. No dependencies were added.

## Architecture and operating modes

The existing `LeagueWorker`/`LeagueClient` remains the only League API producer. Its session/state signals start and stop the existing `LeagueVisualWorker`. That worker owns one existing `LiveScreenStream` capture thread, a two-frame/8 MiB bounded history, the existing cached OpenCV matcher, strict3 confirmation and `VisualAlertDispatcher`. There is no extra vision model, inference loop, TTS worker or movement timer.

| Requested mode | Effective behavior |
|---|---|
| OFF | No League-specific capture. Default unless explicitly enabled. |
| LIVE_OBSERVATION | Foreground game-window capture and local recognition; aggregate diagnostics only. No enemy-warning speech or tactical overlay. |
| CONTROLLED_TEST | Explicitly selected local video, reviewed eligibility manifest, unchecked-by-default sound opt-in and a maximum 30-second deadline. Refused during an active League session. |
| APPROVED_ALERTS | Falls back to silent observation: this build has no verified Riot deployment authorization. An environment variable, checkbox or locally created approval receipt is not accepted as evidence. |

An environment request for CONTROLLED_TEST becomes silent observation; it cannot bypass the on-demand opt-in. This implementation supports the **recorded replay** alternative for controlled speech. Even Practice Tool remains observation-only for live capture; automatic private-session classification/speech is not implemented. A new session, match epoch, deadline expiry, Stop or application shutdown cancels a recorded test and invalidates its announcements.

The visual signal now reaches the existing main-window notification handler, one-slot `LeagueVoicePolicy`, Advice/voice workers and warm Kokoro session. Current visibility, cancellation, freshness, mute, priority and cooldown checks remain in force. Normal screen-model proactive tips are also suppressed during active observation, so that they do not provide a separate automatic-warning route. User-requested conversations and existing factual League API announcements remain available.

Read-only APIs and visible pixels do not constitute deployment approval. Current requirements must be checked with Riot before enabling an approved product: [League documentation](https://developer.riotgames.com/docs/lol), [general developer policies](https://developer.riotgames.com/policies/general). No gameplay input, process-memory reading, anti-cheat bypass, hidden-state inference or continuous audio recording was introduced.

## Reliable capture and current match context

* Discover the foreground `League of Legends.exe` window, not the client launcher or an arbitrary similarly titled window. Validate HWND/PID before and after acquisition.
* Resolve physical client coordinates on the capture thread using per-monitor DPI awareness. Crop normalized minimap coordinates within that client, including negative desktop origins; no primary-monitor origin assumption.
* Reject minimized, nonforeground, resized/mismatched, stale or obscured frames. A layered window is ignored only when its invisibility is demonstrable (DWM cloaking or known global alpha zero). Unknown per-pixel overlay transparency still blocks capture.
* Refresh derived references from the **current API roster**, not the old recording. Reuse cached official icons; downloads/preparation occur on the existing worker at setup. Base recording calibration is preserved. Roster, epoch and calibration changes rebuild safely; missing state pauses recognition.
* Skip blank Practice Tool champion rows. Incomplete/mirror rosters that cannot support reliable team recognition become capture-only instead of guessed enemies.
* Retain the actual assigned role from existing APIs through transitions. Unsupported modes remain disabled. Practice Tool map 11 is supported for observation, without inferring a lane from champion selection.

The measured game was 1920×1080. Current roster: allied Yuumi, Hecarim, Volibear, Varus, Kassadin; enemy Sion, Yorick, Maokai, Cassiopeia, Ziggs. The API reported player Yuumi with position NONE. Capri correctly retained **unknown role/lane/player position**, rather than inheriting Support or the previous recording's roster. No attached Yuumi position was inferred from another champion's icon.

The base normalized minimap ROI mapped to approximately `(1645,806,262,257)` physical pixels. Explicit snapshots confirmed that this crop contains the actual minimap. Geometry validity is distinct from verified HUD/identity/location calibration. Recording-specific lane regions, nameplates and occlusion crops are not copied into live evidence. Only an explicitly scoped `calibration_scope: "live_game_window"` profile can retain reviewed live regions. Viewport identity needs separately calibrated visible nameplates; this live run used the minimap, not general 3D champion/skin recognition.

## Actual workstation measurements

Windows build 26200; Python 3.11.6; Intel Core i7-10700F (8 physical/16 logical cores); 31.87 GiB RAM; PySide6 6.12.0; OpenCV 5.0.0; MSS 10.2.0; psutil 7.2.2. Existing sherpa-onnx 2.0.1 and sounddevice 0.5.6 were preserved, but no audio was generated in these observation trials.

Two 60-second foreground Practice Tool runs used the current roster, same geometry, official references and strict3. No test suite or deliberate inference workload ran concurrently. Neko's main process was closed; the finite benchmark used the existing League services plus capture/detection, not the full conversation application. Gameplay frames and external CPU scheduling were not identical, so these are sequential field measurements, not paired identical-frame experiments.

The first run exposed a measurable wait in the existing detector's 20 ms polling loop. The change wakes that same worker from `FrameHistory` on frame publication, without creating another thread or timer.

| Milliseconds: p50 / p95 / p99 / maximum | Polling (`live-practice-03`, n=552) | Frame notification (`live-practice-04`, n=553) |
|---|---:|---:|
| Crop acquisition → frame ready | 4.484 / 10.905 / 11.619 / 12.285 | 8.029 / 11.187 / 11.892 / 12.938 |
| Frame ready → detector start | 13.349 / 28.611 / 29.376 / 30.598 | 0.069 / 0.121 / 0.171 / 0.294 |
| Detector processing | 24.891 / 33.388 / 36.018 / 46.893 | 23.486 / 32.518 / 36.267 / 44.841 |
| Crop acquisition → detection complete | 48.066 / 62.509 / 67.120 / 70.942 | 31.213 / 40.795 / 46.339 / 56.001 |

Acquisition timing starts after the initial window/visibility validation, immediately before pixel reads, and includes post-capture validation. It excludes initial discovery/preflight. These metrics are not appearance-to-confirmation or appearance-to-audio measurements. The median handoff reduction is 13.28 ms; the marginal acquisition-to-detection median decreased 16.85 ms (35.1%). The smaller change in detector CPU time is not attributed to the signaling change.

| Other measurement | Polling | Frame notification |
|---|---:|---:|
| Frames analyzed | 552 | 553 |
| Steady detection rate | About 9–10 FPS | About 9–10 FPS |
| Skipped / stale processed frames | 0 / 0 | 0 / 0 |
| Sampled process CPU, percent of one core | 29.12% | 26.30% |
| Sampled process RSS | 90.59 MiB | 90.68 MiB |
| Confirmed candidate episodes | 30 | 33 |
| Confirmed champion-frame observations | 1,083 | 1,178 |
| Proposed / accepted / started warnings | 0 / 0 / 0 | 0 / 0 / 0 |

CPU is this benchmark process's accumulated CPU/wall ratio, not total system CPU, game CPU, or incremental full-Neko overhead. RSS is a sampled process value, not a measured peak. These metrics exclude an actively running conversation/TTS model workload. Incremental GPU cost and paired in-game rendering FPS were **not measured**; this run cannot establish League FPS impact or contention with Qwen/background vision. Offline Phase 6.4's 18.86/19.86 ms detector baseline used different footage and conditions and is not a live guarantee.

A final 25-second silent run (`artifacts/phase7/live-practice-05-gpu/report.json`) processed 227 frames, with zero skipped/stale processed frames and zero proposals. Handoff p50/p95 was 0.070/0.121 ms; acquisition→detection p50/p95/p99/max was 29.611/38.941/43.020/44.727 ms. Sampled process CPU was 27.30% of one core and RSS 89.70 MiB. This confirms the handoff behavior after the final capture-context fix; it is not another accuracy trial.

Alongside that run, a finite read-only `nvidia-smi` process collected 26 device-wide samples at approximately 1 Hz into `artifacts/phase7/live-practice-gpu-samples.csv`. GPU: NVIDIA GeForce RTX 3060, 12,288 MiB, driver 621.01. GPU busy was 29–56% (median 36%); device memory use was 2,577–2,583 MiB (median 2,582 MiB). These totals include League/display/other device users; they are **not GPU utilization attributable to Capri**. There was no paired no-observer game run or synchronized render-FPS measurement. The detector itself remains CPU OpenCV; no GPU model was added. The finite monitoring process exited after sampling and is not part of normal Neko operation.

Initial diagnostics also caught and fixed a tuple/list roster-adapter error. A subsequent zero-frame run correctly stopped at Discord's overlapping overlay; after the operator disabled it, capture succeeded. These blocked runs are not counted as successful live performance samples.

Local evidence, ignored by Git:

* `artifacts/phase7/live-practice-03/report.json`
* `artifacts/phase7/live-practice-04/report.json`
* `artifacts/phase7/live-practice-04/explicit-minimap.png`
* `artifacts/phase7/live-practice-04/explicit-minimap-observations.json` — exact associated sequence/source/observations.

The explicit benchmark `--snapshot` option retains one crop for inspection. Normal operation neither writes screenshots nor retains an extra diagnostic image. Neither benchmark artifacts nor calibration/video data were committed or uploaded.

## Recognition accuracy: what is and is not verified

There are **zero independently reviewed live detection episodes**. Episode/champion-frame counts above are matcher outputs, not ground truth, precision or recall. No full-run false/ambiguous detection count or missed-enemy rate can be established from these samples.

An assistant spot inspection of the final correlated snapshot found one clear false candidate: **Cassiopeia at normalized `(0.046,0.327)` falls on an allied turret icon**, not a champion portrait. Two other reported portrait locations (Yorick and Maokai) were present in the crop, but this is not independent identity validation. This small, detector-selected inspection must not be converted into a full-run accuracy estimate. Official portraits and current-roster competition are insufficient to guarantee accuracy against static map structures. No ad hoc live threshold or unvalidated detector change was introduced to conceal this finding.

The observed role/location evidence was insufficient for proximity or lane relevance. Zero speech proposals therefore demonstrate silent-mode enforcement, not perfect warning accuracy. Genuine live notification accuracy and digital/physical audio onset have **n=0** in Phase 7. Historical Phase 6.2–6.3 audio results are not new live results.

Recommended next step: label representative independent live minimap frames, including static turrets, empty regions, overlaps and current-roster champions. Evaluate a structural/background rejection change on held-out labels before considering eligible live speech. Keep strict3 and the approval gate unchanged.

## Windows setup and manual use

From a normal interactive PowerShell in `F:\Neko`:

```powershell
$env:CAPRI_LEAGUE_VISION_MODE = "LIVE_OBSERVATION"
.\.venv\Scripts\python.exe -B main.py
```

No administrator launch is required by the application. The coding tool's isolated desktop could not see the game; its finite benchmark required an approved unsandboxed invocation. This was a tool-environment restriction, not a new runtime requirement for Neko.

1. Leave League in borderless mode. Left-click Capri to reveal controls, then choose **LEAGUE OBSERVATION** (initially LEAGUE VISION), or use **Ctrl+Shift+V** while Neko has application focus. These are application shortcuts, not global game hooks.
2. Choose **Start observation**. The panel shows selected game window/client bounds, actual role/mode/roster, capture/detection FPS, counts, resource samples, calibration and why speech is disabled. It shows no tactical enemy coordinates.
3. Return League to the foreground. Opening a foreground diagnostics window pauses pixel reads; its last aggregate metrics remain visible. Overlays covering the crop also pause capture. Unknown overlay transparency is deliberately not bypassed. Resume by restoring an unobstructed foreground game.
4. **Stop** disables capture immediately. Session end/shutdown also stops it; a new active game can resume the requested observation mode. Loading/reconnect waits for fresh supported state. OFF can also be set with `$env:CAPRI_LEAGUE_VISION_MODE = "OFF"` on the next launch.
5. For different HUD/minimap settings, open the existing calibration editor (panel button or Ctrl+Shift+L), use a local frame from that configuration and adjust the minimap ROI/reference size. Save locally; capture checks geometry and rebuilds on changes. Do not reuse recorded lane-region evidence for a new live match or invent attached Yuumi coordinates.

To repeat the finite silent capture measurement, close Neko first so that the benchmark is the sole League producer:

```powershell
.\.venv\Scripts\python.exe -B benchmark_league_live.py --seconds 60 --output artifacts\phase7\my-live-run-01
```

Use a fresh output directory. Add `--snapshot` only when explicitly choosing to save a local minimap crop. Keep the game foreground and unobstructed throughout; zero captured frames is a blocked/invalid capture run, not a performance sample.

## Controlled recorded speech test

Finish the active League session first. Open Ctrl+Shift+V, select a local recording, its exact calibration and a reviewed whitelist. Sound opt-in defaults to off. The test has a 1–30 second limit and routes accepted announcements through the existing voice system; ADVICE and League voice settings still apply. It never grants permission for live matchmaking speech.

To create your own human-reviewed whitelist with the existing tool:

```powershell
.\.venv\Scripts\python.exe -B benchmark_league_vision_e2e.py calibrate --video tests\fixtures\league_real_gameplay.mp4 --calibration data\league-vision\calibration.json --manifest data\league-vision\e2e-eligibility.json
```

Mark actual named lane regions, label verified visible intervals, select the relevant label rows and export eligibility. For attached Yuumi, use verified lane regions or independently visible player evidence; never derive her independent position from Caitlyn's portrait. The workflow writes `e2e-eligibility-calibration.json` alongside the manifest. Select those exported files in the panel and explicitly enable the spoken test.

Existing assistant-reviewed evidence can be used only with the separate explicit **assistant-reviewed** checkbox, and remains non-independent diagnostic evidence:

* calibration: `data\league-vision\phase6_4-assistant-review\audio-calibration.json`
* whitelist: `data\league-vision\phase6_4-assistant-review\audio-eligibility.json`
* recording: `tests\fixtures\league_real_gameplay.mp4`

The UI test is not a calibrated loopback benchmark. For device measurements use the existing Phase 6.2 tool, outside an active match, with other audio paused and matching reviewed files:

```powershell
.\.venv\Scripts\python.exe -B benchmark_league_vision_e2e.py run --mode replay --video tests\fixtures\league_real_gameplay.mp4 --calibration data\league-vision\phase6_4-assistant-review\audio-calibration.json --manifest data\league-vision\phase6_4-assistant-review\audio-eligibility.json --assistant-reviewed --scenarios verified --audio --loopback --trials 20 --output artifacts\phase7\my-reviewed-audio-01
```

That command measures recorded evidence through real Kokoro/output, not live game accuracy. Software DAC estimates, digital loopback onset and physical audible output remain distinct. No Phase 7 real-device speech result is claimed; the current Practice Tool was kept silent throughout.

## Changed files and validation

| File | Change |
|---|---|
| `config.py` | Explicit opt-in operating mode; legacy approval flags cannot authorize speech. |
| `core/league_visual_modes.py` (new) | Mode permissions and cached actual-roster profile preparation. |
| `core/league.py` | Omit blank Practice Tool champion rows from the public roster. |
| `core/league_lane.py`, `core/league_visual.py` | Supported Practice Tool observation; existing role uncertainty and matcher retained. |
| `core/league_capture.py` | HWND/PID and visibility checks, diagnostics, finite local recorded capture source. |
| `core/live_screen.py` | Condition notification from the existing bounded frame history. |
| `gui/league_vision.py` | Existing worker/service lifecycle, current-roster refresh, silent/recorded routing, bounded metrics and epoch-safe cleanup. |
| `gui/league_vision_panel.py` (new) | On-demand aggregate diagnostics, Start/Stop and explicit recorded-test controls; no new timer. |
| `gui/controls.py`, `gui/window.py` | Controls/shortcut, queued notification integration, shared status refresh and screen-tip suppression during observation. |
| `benchmark_league_live.py` (new) | Finite actual capture benchmark with optional, correlated local snapshot. |
| `tests/conftest.py` | Isolate external mode configuration from tests. |
| `tests/test_league_live.py` (new) | Current roster, permissions, actual replay/CV lifecycle, mode/session changes, UI, overlay/PID checks and restarted frame sequences. |
| `tests/test_league_visual.py` | Adapt capture fakes to explicit source identity and new permission semantics. |
| `tests/test_live_screen.py` | Frame arrival/context/staleness and shutdown wake regressions. |
| `PHASE7_LIVE_LEAGUE_REPORT.md` (new) | This report. |

Validation command:

```powershell
.\.venv\Scripts\python.exe -B -m pytest -q
```

Final full-suite result: **967 passed in 198.44 seconds**. The preceding full run passed 965 tests; two final regressions were added for blank Practice Tool roster rows and capture-context changes that restart the frame sequence. All 22 focused live-integration tests passed separately as well. `git diff --check` passed (Git's LF/CRLF notices are not test failures).

Tests use Qt offscreen and mocked APIs; they do not require League, microphone, Ollama or real audio hardware. Existing cancellation, pronunciation, League Companion Mode, sprites and movement coverage remains part of the full suite. Live measurements above were separate from the test suite; no synthetic frames were substituted in the successful live capture runs.
