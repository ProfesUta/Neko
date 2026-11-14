# League reference, match journal and recorded-game review

Capri now has an on-request reference path and a separate recorded-video review path. Her existing League worker, 250 ms event poller, champion-select WebSocket, conversation model, Kokoro voice, interruption handling, League SVGs, docking and movement lock remain in use.

## What she can answer

- “What does Yuumi Q do?” — a patch-labelled Data Dragon ability description.
- “What are Ahri's weaknesses?” — source-provided general tips, not a current fight assessment.
- “What role does Lee Sin play?” — static **classes** are distinguished from an API-assigned lane position. The existing champion-select role answer still supplies the assigned position.
- “What does the enemy jungler's champion do?” — uses the existing player list's exposed enemy `JUNGLE` assignment. If no unique assignment exists, Capri asks for the champion name rather than inferring a role from a champion or summoner spell.
- “What items have I bought?” — current own-player inventory; sold-item history is explicitly unavailable.
- “What's my current KDA?” — cached own-player kills/deaths/assists.
- “Which objectives were recorded?” — the available event history, with game timestamps.
- “What happened in the last teamfight?” — recorded kills with timestamps and an explicit warning that the API does not identify teamfight boundaries.
- “Review my last match” — a short bounded event recap. Objective records are retained separately so ordinary kill-history eviction does not immediately remove them.
- “Give me detailed postgame coaching” — **only on this explicit request**, the existing streaming conversation route receives a small review context. Observations, interpretations and uncertainty must remain distinct. A bounded event log cannot prove a positioning mistake.

Ability interactions are not declared verified merely because two descriptions mention similar effects. Unresolved Data Dragon scaling placeholders remain labelled. General jungle pathing discussion describes concepts; it does not calculate an enemy route or dictate a live decision. Item names with different mode variants require inventory ID/map evidence or clarification.

## Architecture and data

`LeagueClient` still owns all live League networking. Own inventory IDs, map number, and a copy-safe **champion/side/assigned-position-only** roster are extracted from the three existing slower stats requests. No new live endpoint, polling loop or connection system was added. New deterministic answers precede screen/model processing. Existing older API-then-current-frame answers remain compatible.

`LeagueKnowledge` uses a separate bounded, reusable connection only for official static HTTPS downloads. It coalesces cache misses, bounds responses/timeouts, and writes cache files atomically. Warm lookups use in-memory data. No static polling, model download or raw dataset injection occurs. Normal conversation keeps its previous instruction prefix; extra post-game instructions appear only with a relevant review. New tool schemas are omitted from unrelated tool turns.

The static cache lives under `data/league-knowledge/`. A version is pinned until explicitly refreshed; the answer always identifies its Data Dragon patch. This is **not** proof that the user's region/client runs that patch. Set an explicit patch if necessary. Sources: [Riot Data Dragon and local API documentation](https://developer.riotgames.com/docs/lol).

The match journal is bounded and memory-only: 64 current user reports, 128 objective records, the existing bounded kill/event history, and one most recent completed/unavailable-session archive. It does not write a match database automatically. Current notes reset with the existing match epoch and cannot migrate across a racing reset. An unavailable session without a `GameEnd` event never supplies a fabricated result or a claimed final score.

## Settings

| Setting | Environment override | Default |
|---|---|---|
| `LEAGUE_KNOWLEDGE_ENABLED` | `CAPRI_LEAGUE_KNOWLEDGE` | `1` |
| `LEAGUE_STATIC_DOWNLOADS_ENABLED` | `CAPRI_LEAGUE_STATIC_DOWNLOADS` | `1` |
| `LEAGUE_KNOWLEDGE_PATCH` | `CAPRI_LEAGUE_PATCH` | Empty: reuse pinned cache; first download selects a published version |
| `LEAGUE_SIGHTING_NOTES_ENABLED` | `CAPRI_LEAGUE_SIGHTING_NOTES_APPROVED` | `0`, pending product review |
| `LEAGUE_REPLAY_ENABLED` | `CAPRI_LEAGUE_REPLAY` | `0`, explicit offline opt-in |

Existing proactive voice/advice controls are unchanged. The new reference/journal features introduce no automatic speech queue. An offline job posts one completion/error notice to chat, without speaking, focusing or activating the game window. Microphone state remains separate from output speech.

Prewarm static data before playing:

```powershell
.\.venv\Scripts\python.exe -B -m core.league_knowledge --champions LeeSin Yuumi Ahri
# Explicit patch update, outside a game:
.\.venv\Scripts\python.exe -B -m core.league_knowledge --refresh --champions LeeSin Yuumi Ahri
# Optional pin; omit --refresh when using it:
$env:CAPRI_LEAGUE_PATCH = '16.22.1'
```

The local verification downloaded real official patch **16.22.1** data for these champions and items. Cold uncached questions may wait for bounded static requests; prewarming and disabling static downloads during play makes this path strictly offline. Download failure produces an honest unavailable-reference answer and a retry cooldown rather than repeated requests or invented facts.

## User-reported sightings and Riot review

The sighting parser supports statements such as “I saw Lee Sin top” and “Note that I spotted Lee Sin near mid lane.” With the feature enabled, it stores a known champion, reported region, latest fresh API game clock and monotonic reception time. A later “Where did I last report Lee Sin?” repeats the report and age, explicitly denying that it is a verified current position. It does not predict a route, calculate cooldowns, or issue positional alerts.

This implementation does **not** establish Riot product approval. Riot requires product registration/review and prohibits unfair advantage, hidden-information use and dictated decisions. The sighting feature therefore remains disabled until the product's proposed use is reviewed. Setting the environment variable is an operator configuration/attestation, not an approval issued by Riot. Sources: [general policies](https://developer.riotgames.com/policies/general), [League-specific policies](https://developer.riotgames.com/docs/lol#game-integrity).

There is no continuous automated live minimap tracking. The recorded-video analyzer is explicitly post-match. Before distribution/deployment, register/update the application with Riot and present the required disclaimer visibly to players; existing attribution is in `THIRD_PARTY_NOTICES.md`. No product submission or external account action was performed by this implementation.

## Recorded-video setup

Install the optional replay dependency; normal Capri does not need it:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-replay.txt
$env:CAPRI_LEAGUE_REPLAY = '1'
```

This adds **opencv-python-headless 5.0.0.93**. The installed package requires NumPy >=2 on Python 3.11; the project's existing NumPy 2.4.6 was retained. The headless build avoids a second Qt GUI stack. Neither OpenCV nor a replay worker is loaded into the active voice path. It is imported only for an explicit offline job/extraction operation.

Use a completed local MP4/AVI/MKV recording. `.rofl` files must first be rendered to video using the game/replay tools. Use the original player's visibility/fog of war; spectator omniscient footage is not evidence of what the player could see. The perspective fields are an operator assertion, not an automatic proof of fog settings.

1. Extract a calibration frame:

   ```powershell
   .\.venv\Scripts\python.exe -B -m core.league_replay frame 'F:\Recordings\match.mp4' --at 120 --output 'F:\Recordings\calibration.png'
   ```

2. Copy `examples/league-replay-calibration.json` into a private working directory. **Its example roster and geometry are illustrative, not measured for your video.** Replace them with the complete known roster and measured frame dimensions. Include a unique known enemy `JUNGLE` assignment; an arbitrary champion class does not establish that role.

3. Measure minimap pixel rectangle `(x,y,width,height)` in a frame `(W,H)`. Store `[x/W,y/H,width/W,height/H]` as `minimap`. Crop one tight, textured, 6–128 pixel **visible minimap icon** per roster champion into PNGs. Use the same recording/HUD, including its icon border. All roster templates are required as identity distractors. Data Dragon splash/portrait artwork alone is not assumed identical to a minimap icon.

4. Optionally add verified named region rectangles relative to the minimap:

   ```json
   {"label":"your measured region name","bounds":[0.10,0.15,0.20,0.25]}
   ```

   Put these in `regions`. Without a unique calibrated region, reports use minimap fractions rather than inventing a lane label. Resized video with the same aspect ratio is normalized to the calibrated minimap; a changed aspect ratio is rejected. A changed HUD/minimap scale, rotation, heavy compression or icon style may need recalibration.

5. Analyze **after the match**, into a fresh output directory:

   ```powershell
   .\.venv\Scripts\python.exe -B -m core.league_replay analyze 'F:\Recordings\match.mp4' --calibration 'F:\Recordings\calibration.json' --output 'F:\Recordings\review-01' --post-match
   ```

In Capri, the existing tool route also accepts an explicit request to analyze the supplied video and calibration paths. `analyze_league_recording()` queues one job; `get_league_recording_status()` returns its status/report path. Qt handles cross-thread submission/completion. The existing activity state refuses jobs during League sessions and admission rechecks/cancels if a session starts. Existing foreground inference admission pauses offline work for voice/chat and its grace period. Shutdown is asynchronous and waits for worker cleanup; no new polling timer/process is created.

The command-line path uses the explicit `--post-match` assertion and never connects to League; do not use it as a live feed or run it during competitive play. URLs, devices and UNC streams are rejected. Only an existing local file is accepted.

## Detection, review and coaching

The analyzer samples the recording at 2 Hz, uses single-thread CPU OpenCV correlation at three small icon scales, rejects weak/ambiguous matches, and requires two nearby same-region visible samples. It deduplicates continuous appearances and records only actual sampled detections. Missing icons produce no observations, paths or last-seen extrapolation. Limits bound frame size, duration, templates and candidate count. Cancellation releases the decoder and leaves no success report.

`report.json` contains **candidates** with video timestamps, normalized icon positions, template similarities, ambiguity margins, uncertainty and annotated evidence PNGs. Similarity is not a calibrated probability. It is not proof of a true champion's identity: decoys, disguises, overlaps and HUD changes remain possible. Evidence timestamps and the first accepted visible sample are stored separately.

Review every candidate thumbnail and corresponding video segment. Only then confirm chosen IDs:

```powershell
.\.venv\Scripts\python.exe -B -m core.league_replay review 'F:\Recordings\review-01\report.json' --confirm sighting-0001 sighting-0003
# Only if YOU recall missing one of those already-confirmed observations:
.\.venv\Scripts\python.exe -B -m core.league_replay review 'F:\Recordings\review-01\report.json' --confirm sighting-0001 sighting-0003 --missed sighting-0003
```

This generates the confirmed chronological timeline. `load_league_recording_report()` loads that reviewed report into Capri. Ask for the timeline, missed sightings or detailed post-game coaching. Model inference is reserved for the explicit detailed reasoning request; factual summaries use cached records. Capri cannot infer your gaze/attention from minimap pixels. User-marked misses are labelled as such. Video timestamps are **not** API game-clock timestamps; replay speed/pauses/recording offset require separate alignment before correlating them.

No genuine recorded gameplay was supplied for this implementation. Real minimap identity precision/recall, false positives, skins, compressed footage, spectator visibility and human coaching quality still need validation on your recordings. The synthetic detection tests do not establish real-game accuracy.

## Measurements and validation commands

The benchmark extends the existing `benchmark_league.py`:

```powershell
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider
.\.venv\Scripts\python.exe -B benchmark_league.py --seconds 1 --companion --replay --output artifacts/league-companion/local.json
# Optional existing read-only live API probe; no audio or game input:
.\.venv\Scripts\python.exe -B benchmark_league.py --live --seconds 5 --output artifacts/league-companion/live.json
```

Real local Python/OpenCV processing, with **synthetic API/reference/image fixtures** and no active League match, LLM or audio:

| New operation | p50 ms | p95 ms | p99 ms | Max ms | n |
|---|---:|---:|---:|---:|---:|
| Cached ability reference | 0.2960 | 0.3380 | 0.4313 | 0.7035 | 2,000 |
| Cached own inventory | 0.0199 | 0.0221 | 0.0345 | 0.0761 | 2,000 |
| Cached current KDA | 0.0268 | 0.0423 | 0.0515 | 0.0990 | 2,000 |
| Unrelated-conversation gate | 0.0279 | 0.0307 | 0.0475 | 0.1073 | 2,000 |
| Offline visible icon frame, synthetic 1080p/10 templates | 20.4485 | 22.2188 | 26.0506 | 31.4034 | 250 |
| Offline absent icon frame | 19.9757 | 21.3981 | 29.7036 | 32.6567 | 250 |

The synthetic detector accepted 250/250 planted icons and rejected 250/250 absent-icon frames. These are not real minimap accuracy statistics and exclude video decoding. The measured work uses CPU only; the offline worker is disabled/absent during normal play. No actual game FPS, GPU contention or new microphone-to-speaker latency claim is made.

A real **locally cached Data Dragon** item-question check exposed regex-cache thrashing: the first implementation held 512 patterns, fewer than the current item/champion catalog. A bounded 2,048-entry cache reduced this same 30-sample warm query from **57.3627 / 61.1702 / 65.5901 / 66.9297 ms** to **1.0666 / 1.1702 / 1.2213 / 1.2479 ms** (p50/p95/p99/max). This compares the initial new lookup implementation with the corrected implementation, not an older shipped capability.

Existing synthetic sanity checks remained fast: LCU-message snapshot p50 0.0232→0.0203 ms, old deterministic answer 0.0055→0.0047 ms, 300-event mocked HTTP poll 1.1267→0.8587 ms. These separate before/after runs reflect timing variation and are not claimed as causal optimizations. The 250/500 ms polling experiments remain available; polling configuration was not changed.

Raw benchmark records: ignored `artifacts/league-companion/before.json`, `final.json`, and `item-cache-comparison.json`. No match identities, credentials or private recordings are saved by these benchmarks.

## Files

| File | Purpose |
|---|---|
| `core/league_knowledge.py` | Official static cache, bounded retrieval, factual answers and prewarm CLI |
| `core/league_journal.py` | Reported notes, objective retention, one match archive and bounded review evidence |
| `core/league_replay.py` | Explicit recorded-file calibration/detection/review/timeline CLI |
| `core/league.py` | Reuse live snapshots/roster, match resets, deterministic routes and review context |
| `core/conversation.py` | Explicit recorded-file tools and conditional review grounding |
| `core/chat.py`, `tools/league.py`, `tools/registry.py` | Scoped tools through the existing tool engine |
| `gui/league_replay.py` | One queued low-priority cancellable offline job |
| `gui/workers.py`, `gui/window.py` | Direct answers, existing streamed coaching, service wiring and cleanup |
| `config.py`, `.gitignore` | Optional settings and private/generated cache/report exclusions |
| `requirements-replay.txt` | Optional headless OpenCV dependency |
| `examples/league-replay-calibration.json` | Illustrative schema, not a ready calibration |
| `tests/test_league_knowledge.py`, `tests/test_league_replay.py`, `tests/conftest.py` | Mocked/API/cache/geometry/video/concurrency/priority/shutdown coverage and isolation |
| `benchmark_league.py` | Extend existing measurements with explicit new routes and offline detector |
| `LEAGUE_COMPANION_GUIDE.md` | Usage, measurements, limitations and policy boundary |
| `THIRD_PARTY_NOTICES.md` | Expanded static-data attribution and optional OpenCV notice |

## Final checks

Complete suite: **745 passed in 110.85 seconds** with the command above. The new knowledge/replay tests passed 31 cases; a targeted check including the two integration regressions passed 33. The initial complete run had 743 passes and two failures: unresolved personal-champion questions changed the existing uncertainty reply, and a GUI lambda retained a closed window. The verified API/screen fallback was restored and the callback became a bound Qt receiver; both original regression tests and the final complete suite passed afterward.

An offscreen real-window construction/show/close check also passed with preloads, live networking, capture and audio disabled. No replay worker was started by ordinary startup. A saved synthetic MJPEG recording was actually decoded/analyzed into two candidate thumbnails and zero automatic confirmations under `artifacts/league-companion/synthetic-review/`; the annotated target box was visually checked. This remains synthetic evidence, not a real-match accuracy claim.

`git diff --check` passed (Git printed only normal LF/CRLF conversion notices). Voice modules, voice assets, champion pronunciation, League SVGs, teleport renderer and inference-priority implementation were not changed. No actual live-match FPS, microphone/audio-output latency or real gameplay/replay recording measurement was available for this task; the existing hardware performance reports are not relabelled as new measurements.

Phase 6 now adds the local crop/review/label editor, expanded cached template matching and an approval-gated runtime. See [PHASE6_LEAGUE_VISION_REPORT.md](PHASE6_LEAGUE_VISION_REPORT.md) for calibration, validation commands, measured synthetic results and remaining real-footage work. Open the editor with Ctrl+Shift+L. Competitive live capture/alerts stay disabled by default; no approval or real-match accuracy is claimed.
