# Spatial upgrade verification

Run automated checks from the project directory:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
```

Tests use synthetic screen pixels, window/accessibility fixtures, mock model
responses and audio playback, and Qt's offscreen backend. No test reads the real
desktop or microphone.

| Scenario                      | Automated coverage                                                                                                   | Live desktop check                                                                                |
| ----------------------------- | -------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| One monitor                   | GUI action, placement, overlays, cleanup                                                                             | Ask where a visible app is                                                                        |
| Two or more monitors          | Per-image IDs/transforms and monitor associations                                                                    | Put Discord on the other monitor and ask for it                                                   |
| Secondary left/above/below    | Negative/vertical geometry and placement                                                                             | Verify arrival on the requested physical display                                                  |
| Different scaling/resolution  | Physical/logical/image round trips and friendly-name/native-ID mapping                                               | Use 100% and 150%/200% displays together                                                          |
| Target on primary/secondary   | Native/UIA target resolution and explicit monitor filtering                                                          | Repeat requests in both directions                                                                |
| Target near an edge           | Candidate scoring and sprite bounds                                                                                  | Move a small target to each corner                                                                |
| Partially/completely covered  | Visible window fragments; covered controls rejected                                                                  | Cover part of the target with another app                                                         |
| Missing/ambiguous target      | No action target; clarification/unverified results                                                                   | Minimize the requested app or open two matching windows                                           |
| Moving/disappearing target    | Refresh before GUI acknowledgement and response; cancellation                                                        | Move/close/minimize the target during inference and speech synthesis                              |
| Rapid requests                | Earlier inference cancelled; latest request owns position                                                            | Send a second location request while the first is pending                                         |
| Normal speech after pointing  | Gesture priority, restoration, and playback acknowledgement                                                          | Check listening/talking expressions after completion                                              |
| Cross-monitor electricity     | Existing global-coordinate VFX plus placement/conversion checks                                                      | Verify simultaneous departure/arrival bursts across actual displays                               |
| Monitor unplugged             | Target revision rejection, gesture clearing, safe recovery                                                           | Unplug/reconnect the display Capri occupies                                                       |
| Input interception            | Native Qt overlay flags and offscreen checks                                                                         | Click through the target outline/VFX; drag and click Capri normally                               |
| Highlight lifetime            | Timer expiry and cancellation cleanup                                                                                | Check optional red outline and TARGET_HIGHLIGHT_ENABLED=False                                     |
| Hand-to-target lightning      | Exact endpoints in both poses; negative coordinates and DPR 1/1.5/2 rendering                                        | Verify fingertip alignment, visible target reticle, click-through and cleanup during speech       |
| Monitor zero/default          | String/numeric zero, null, all-display search and explicit user monitor precedence                                   | Ask for an app on another display without naming its monitor                                      |
| Failed follow-up lookup       | Previous beam/target cleared on not-found, invalid display and ambiguous lookup                                      | Ask for a nonexistent target after a successful pointing request                                  |
| Discord channel/server titles | Refresh by window handle + process ID; changing titles, bounds and occlusion; no switch to another window            | Ask for Discord with a channel/server open, then change channels during the explanation           |
| Transient screen errors       | Capture initialization/grab/model retries; error persists until success                                              | Switch fullscreen modes or lock/unlock, then check VISION recovers                                |
| Fast location request         | Parser, native action, text and speech acknowledgement with both model calls forbidden, even with a busy vision lock | Ask "Where is Discord application on my screen?" and check the `Direct spatial action` timing log |
| Voice request priority        | Background screen inference pauses synchronously at speech onset                                                     | Speak while SCREEN ON is analyzing; the request should take priority                              |
| Warm speech output            | Reused Piper process; cancellation/crash cleanup and restart; real generated WAV validation                          | Wait for Listening after voice startup, then compare first and subsequent short replies           |
| Muted startup preload         | Recognition/VAD and Piper warm on a separate worker; no listening/playback; text remains usable; load sharing/cancel   | Start Capri, keep MIC OFF, check its tooltip, then enable MIC after models are ready               |
| General conversation          | Streamed text without screenshot/tool calls; compact installed model selection                                       | Ask about a non-screen topic and compare first-text/audio timings, including the first cold reply  |
| Streaming speech              | First sentence played before generation finishes; one final transcript; repeat guards; cancellation/error cleanup    | Ask for several sentences, press Stop mid-answer, then ask something else                         |
| League stat questions         | Fixed local GETs, own-player filtering, cache expiry, unavailable-game handling; direct answers skip both models      | In a practice game with VISION ON, ask for health, gold, KDA and CS; compare with the HUD           |
| League background awareness   | Screenshot analysis remains active with game data; low-health warning precedes analysis; one-second resume delay     | Play with VISION ON and ADVICE ON; confirm actual visual observations and useful urgent tips      |
| Natural screen/advice requests | Gameplay watching, current-situation questions and tip requests take fresh screenshots                               | Ask "Watch my gameplay and give me tips" and "What should I do now?"                              |
| Screen capability and errors  | Actual reader state overrides false model denials; OFF, capture failure and successful recovery are distinct         | Ask "Can you see my screen?" with VISION ON/OFF, then simulate an unavailable model                |
| Vision error reporting        | Question failures reach chat/tooltip; speech includes the reason; duplicate error entries suppressed                  | Ask a visual question with the vision model unavailable; check the same reason in chat and tooltip |
| Recovery by failure stage     | Recovered capture errors clear while inference is paused; analysis/capture errors do not clear one another           | Interrupt capture then recover it during conversation; confirm the old error disappears            |
| Incomplete model responses    | One compact retry; cancellation/server failures do not retry; partial tips/history rejected; explicit repeated failure | Check `data/vision.log` if the model repeatedly produces incomplete responses                     |
| Vision context overflow       | Explicit context for analysis/location; exact 7114/4096 rejection expands once; output room reserved; successful size reused | Ask a visual question spanning monitors and check no `exceed_context_size_error` appears       |
| Background sampling cadence   | No extra interval after slow inference; errors retain retry backoff                                                   | Compare `Screen analysis` timing logs against observed screen changes                             |
| Continuous live capture       | Frames continue during stalled inference and conversation pauses; next inference gets newest frames                  | Keep a moving game visible while talking; check VISION LIVE and measured FPS in its tooltip       |
| Temporal interpretation       | Ordered earlier/latest images, timestamp/source labels; all-monitor questions retain their display metadata           | Ask what just changed during a visible transition                                                 |
| Bounded frame memory          | Count/time/byte limits, source changes, capture failure and OFF clear old frames                                       | Watch memory use during a long session and switch VISION off                                      |
| Stream lifetime and recovery  | Capture handles stay on one thread; hotplug/capture failures reopen them; Stop joins workers and clears buffers        | Switch fullscreen modes, unplug a display, toggle VISION, and exit during analysis                |
| Independent League telemetry  | A low-health warning arrives while visual inference is blocked, with conversation priority preserved                 | Check urgent health tips during a slow image-analysis call                                        |

A real temporal-model check used two synthetic HUD frames (95/100 then 8/100
health). The installed vision model identified the decrease in 11.4 seconds.
The independent producer captured 85 additional frames during that call, reporting
about 7.3 FPS with only 22 recent frames retained. This is a synthetic producer
and real Ollama inference check, not a live desktop/game capture benchmark.

A separate offline startup probe loaded the installed Whisper, Silero VAD, and
Piper models in 16.9 seconds with no errors. Microphone streams and playback were
replaced with fail-fast guards; none were called. This verifies model preloading
without recording or playing audio. Loading stays off the GUI thread, and MIC
remains OFF until explicitly enabled.

A local Ollama context test sent a synthetic image with harmless text padding.
The 4,096-token request was rejected, then the automatic 8,192-token retry
succeeded with 7,565 input tokens. Ollama's running-model endpoint confirmed
an 8,192-token context. No actual screen content was used in this test.

The local model manifests include `qwen3:8b`, which the new auto selector chooses
over the installed 14B/32B and vision models. Ollama was initially unavailable;
the later vision verification reached the installed `qwen2.5vl:7b` and correctly
read 8/100 health from a generated HUD image. The first question took 19.7 seconds,
a subsequent background read took 4.1 seconds, and a fresh danger image produced
"Seek immediate cover or defensive actions" in 9.5 seconds. Images were generated
in memory; no real screen content was sent during these model checks.

Live capture sanity checks detected both real monitors but Windows `BitBlt` failed
on both in this execution session, which also had no foreground window available.
This does not establish the cause of any failure in Capri's interactive session.
Actual desktop capture, game API integration, and first-audio latency still require
the checks above. Streaming tests
exercise the installed Ollama SDK with an HTTP stream fixture, including Stop
during a stalled response. They do not measure real model throughput.

Development timing sample: the offscreen GUI test with synthetic native window
metadata reached pointing in 16 ms with no model call. This is not a live Discord
benchmark and excludes recognition. With the bundled real Piper voice, initial
loading took 3585 ms once, then two short sentences synthesized in 130 ms and
62 ms; no audio was played. Actual hardware, desktop APIs and microphone input
still need the live checks above.

Native monitor discovery in the development session detected two displays,
including a secondary display at negative X with a vertical offset. The session's
native window enumeration returned no accessible windows, so real app UI Automation,
live cross-monitor rendering, and physical mixed-DPI interactions still need the
checks above in Capri's interactive Windows session. The left/right pointing
renderings were inspected using the actual SVG renderer.

Accessibility providers differ between apps. Inaccessible game icons or unlabelled
graphics may produce a visual description without a verified target. Expected
behavior is to explain that limitation and remain in place. Optional local
Tesseract OCR can ground readable labels when accessibility is unavailable.

The background watcher checks window identity, geometry, and occlusion. Control
identity/geometry is refreshed before each action and before speech; animations
inside an otherwise stationary app can still change after a snapshot. Verify
fast-changing controls in their actual app. No screenshot-based system guarantees
pixel-perfect tracking of objects moving between observations.
