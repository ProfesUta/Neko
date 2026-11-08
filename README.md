# Capri

A local Windows desktop companion with a cyberpunk persona, a floating character,
text and voice conversation, persistent facts, screen-grounded answers, and urgent-only screen advice.

## Setup (Windows, Python 3.11)

Install [Python 3.11](https://www.python.org/downloads/) and
[Ollama](https://ollama.com/download/windows). In PowerShell, from the project folder:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
ollama pull qwen2.5:32b
ollama pull qwen3:8b
ollama pull qwen2.5vl:7b
.\.venv\Scripts\python.exe main.py
```

Ollama must be running locally on `127.0.0.1:11434`. If its desktop application
is not already running, start `ollama serve` in another terminal. The default
32B tool model needs substantial available memory; choose another installed,
tool-capable model with `CAPRI_MODEL` if needed. Ordinary conversation automatically
selects a compact, already-installed text model (including `qwen3:8b`), falling
back to `CAPRI_MODEL` if none is available. Set `CAPRI_CONVERSATION_MODEL` to select
one explicitly. Capri does not automatically download models.

For text chat without installing the voice stack, install
`requirements-core.txt` instead. The GUI and screen awareness work without the
optional voice packages. Set `CAPRI_PRELOAD_VOICE=0` to skip voice preloading
entirely, including on machines that have those packages installed.
Install `requirements.txt` when adding voice.

The bundled Piper voice is `assets/voices/en_US-amy-medium.onnx`, together with
its adjacent JSON configuration. Whisper `base` downloads its model on first
startup (or first microphone use if preloading is disabled); Silero's detector
is bundled in its Python package. Initial
model downloads need internet access. Subsequent inference uses local models.

## Controls

- Buttons are hidden by default. **Left-click Capri** to show microphone,
  vision, chat, stop, and advice controls. Left-click her again, use a control, or click
  elsewhere to dismiss them. **Right-click Capri** to reveal **EXIT**.
- **CHAT** opens the text conversation, including voice transcripts and tool results.
  Press Enter to send. Typed replies are silent unless **Speak typed replies** is checked.
  During a location request, sending another message cancels the old request and
  starts the latest one after its worker stops.
- **MIC OFF / MIC ON** starts or stops hands-free voice mode. After each reply,
  Capri listens again. Stop voice mode before sending a typed message; one worker
  owns conversation history at a time. You can compose a draft while she is busy.
  Capri starts with **MIC OFF**. Whisper, the voice activity detector, and Piper
  preload in a separate background worker without recording or playing audio.
  The MIC tooltip shows preparation status. Clicking MIC during loading shares
  the existing load; it does not create a second copy of the models.
- **STOP** interrupts model requests, Piper synthesis, audio playback, and active
  recording. Completed computer actions and saved facts are retained. Stopping
  voice mode also turns the microphone off. This is manual interruption; the
  microphone is not listening for automatic interruption during playback.
- **VISION ON / VISION OFF** enables or disables screen reading. It is on by
  default. The status below Capri always shows **SCREEN ON**, **SCREEN OFF**,
  or **SCREEN ERROR**, even while buttons are hidden.
- **ADVICE ON / ADVICE OFF** controls unsolicited tips independently. With it on,
  Capri speaks only when she judges visible information important to act on now.
  Ordinary observations and general tips wait until you ask. With advice off,
  questions still get fresh screen context. Turning vision off stops both.
- Drag the character to reposition her; dragging does not open the controls.
  She can teleport when idle and stays put during conversations and pointing, or while chat
  or the controls are open. **EXIT** closes her after workers finish cancelling.

Keyboard shortcuts work while Capri or her chat panel has focus:

| Shortcut | Action |
| --- | --- |
| Ctrl+J | Show/hide chat |
| Ctrl+M | Toggle microphone |
| Ctrl+T | Teleport to a random position on her current monitor |
| Ctrl+. | Stop the current conversation |
| Escape in the character window | Stop the current conversation |
| Escape in chat | Hide chat |

## Configuration

Change defaults in `config.py`, or set these environment variables before launch:

```powershell
$env:CAPRI_MODEL = "your-installed-tool-capable-model"
$env:CAPRI_CONVERSATION_MODEL = "auto"  # Or an installed compact model, e.g. qwen3:8b
$env:CAPRI_VISION_MODEL = "your-installed-vision-model"
$env:CAPRI_VISION_CONTEXT_TOKENS = "8192"
$env:CAPRI_VISION_MAX_CONTEXT_TOKENS = "16384"
$env:CAPRI_VISION = "1"  # Set 0 to start with all screen reading off
$env:CAPRI_ADVICE = "1"  # Set 0 for screen questions without unsolicited tips
$env:CAPRI_DIRECT_SCREEN_ANSWERS = "1"  # Set 0 to pass visual context to the conversation engine
$env:CAPRI_WHISPER_MODEL = "base"
$env:CAPRI_WHISPER_LANGUAGE = "en"
$env:CAPRI_PRELOAD_VOICE = "1"  # Warm voice models at startup; microphone stays OFF
$env:CAPRI_DATA_DIR = "F:\CapriData"
.\.venv\Scripts\python.exe main.py
```

There is no automatic `.env` loader. By default, existing facts remain in
`data/memory.db`; the schema is compatible with the previous version. Facts
persist across launches. Conversation text stays in RAM, and the most recent
30 turns are retained for tools; compact conversation uses the latest 6 turns
and up to 12 stored facts. The transcript displays up to 1,000 text
blocks. Remembered facts and recent successful actions refresh on every model
request, including requests after tool execution.

Important limits in `config.py`: 6 tool rounds, 12 tool calls per turn,
12,000 characters per tool result, a 120-second model request timeout, a
45-second recording limit after speech starts, and a 90-second synthesis timeout.
Tool errors are returned to the model so she can recover; unknown tools and
invalid arguments do not run. A Stop between calls prevents later actions.
An already running desktop action may finish before cancellation is observed.

## Teleportation and sprites

Capri snaps directly to each autonomous destination. Her position changes before
the effects are built, with no slide, fade, or hidden arrival frame. Concurrent
departure and arrival bursts use sharp crimson (`#FF0033`) lightning with black
cores, purple glow, branching arcs, and fast red sparks. The arrival is stronger;
the departure leaves a brief fractured contour afterimage.

The renderer samples the transparency of the existing SVG/embedded-PNG assets
once on load, then caches hair/body contours. Lightning follows those contours,
and every active burst keeps clear of Capri's current silhouette. Original images
are unchanged. Effects use reusable, transparent, click-through, non-activating Qt
windows, with one window per display. Bursts expire on a timer; rapid jumps keep
at most six departure/arrival pairs. Dragging never generates bursts. Contextual
actions reserve movement; unrelated autonomous motion pauses until they finish.

Change these constants in `config.py`, then restart Capri:

```python
TELEPORT_ENABLED = True
TELEPORT_VFX_ENABLED = True
TELEPORT_VFX_DURATION_MS = 200
TELEPORT_LIGHTNING_INTENSITY = 1.0
TELEPORT_SPARK_COUNT = 12
```

Duration accepts 16–1000 ms, intensity 0–3, and spark count 0–64. Set
`TELEPORT_VFX_ENABLED = False` for instant movement without electricity, or
`TELEPORT_ENABLED = False` to restore the previous autonomous movement. GUI code
can call `window.teleport_to(x, y, effects=True)` or `window.teleport_to(QPoint(...))`;
`effects=False` is useful for silent placement. Existing visible programmatic
`window.move(...)` calls also teleport. Use these methods on the GUI thread.
Destinations are clamped to their target display's available area; negative
monitor coordinates and Qt's logical-pixel scaling are retained. Effects can
extend beyond the character window and are clipped only at physical display edges.
On supported Windows versions, effect windows are excluded from screen capture;
other backends add active overlays to the existing screen-vision masks.

The new `happy.svg` flashes briefly after a successful typed conversation.
`pointingLeft.svg` is reserved for verified spatial targets; opening chat does not
trigger an ungrounded pointing gesture. Listening, speech detection, thinking,
and talking take priority over temporary expressions. SVG
hot reload refreshes both the image and its cached silhouette.

Verified spatial pointing uses a separate gesture layer that lasts through the
explanation. The same SVG is mirrored in memory for rightward pointing, with a
stable foot anchor. Its cached silhouette also drives the arrival lightning.

Generate a deterministic preview without starting voice, vision, or Ollama:

```powershell
.\.venv\Scripts\python.exe preview_teleport.py
.\.venv\Scripts\python.exe preview_teleport.py --dpr 2 --output-dir artifacts/teleport-2x
```

This writes a GIF and first/mid/final-frame PNGs under the selected output folder.

## Spatial awareness and pointing

With **VISION ON**, try "Where's Discord?", "Show me the download button on
monitor 2", "Where's the Recycle Bin icon?", or "Return to your previous position".
Capri's tools verify the object, choose a position beside it, teleport, and point.
The response waits for the GUI to confirm arrival. Coordinates are checked before
arrival and before starting synthesized speech. Replies that wait for a model
also refresh the target before displaying the answer. If a
target disappears or its monitor disconnects, an obsolete pointing claim is
replaced with an explanation.

`MonitorManager` tracks every connected screen, its native identifier, physical
capture bounds, Qt logical coordinates, usable area, and scale. Monitor numbers
are primary-first, followed by the remaining screens in name/position order;
`get_monitors` returns both these numbers and unique IDs. Qt display-change
signals refresh this inventory and invalidate old targets. Capri is clamped back
onto a connected screen after unplugging a monitor. Negative coordinates and
monitors above/below the primary are supported.

Grounding uses visible Windows window metadata first, then Windows UI Automation
for named controls and desktop icons. Minimized, hidden, and covered targets are
excluded; ambiguous matches ask for clarification. UI Automation runs in a
bounded, cancellable background PowerShell process and reads names, identities,
and bounds. It does not activate applications or click controls.

If native grounding cannot identify the object, the existing vision model can
identify a monitor and semantic label. That label still needs an accessibility
or local OCR match before movement. Optional Tesseract OCR is used if its executable
is on PATH, or if `CAPRI_TESSERACT` names its executable. It is not required for
window/accessibility targeting. A purely visual icon with no accessible identity
or readable label remains unverified; Capri explains the uncertainty instead of
using approximate model coordinates. DOM/browser automation is not installed.

All action tools accept verified target IDs rather than global coordinates:
`get_monitors`, `get_visible_windows`, `locate_on_screen`, `teleport_to_target`,
`point_at_target`, `show_target`, and `return_to_previous`. IDs stay in RAM, have a
bounded lifetime, and are revalidated against current evidence. Candidate placement
considers the actual sprite bounds, fingertip, target overlap, monitor edges,
and Capri's open panels. A fullscreen target may leave no completely empty space;
Capri uses a safe in-bounds position and the target outline helps identify it.

A black/crimson lightning channel runs from the pointing hand to the center of
the verified target box. A small fixed red reticle marks the endpoint while the
bolts flicker. The hand position follows the actual SVG's scale, stable feet and
mirroring. The channel remains visible through the explanation, without covering
Capri's silhouette or intercepting clicks. The optional neon-red box outline
fades after five seconds. Stop, dragging, a replacement or failed lookup, a target
window becoming unavailable, and monitor changes clear the gesture and effects.
Normal expressions resume afterward. The existing teleport bursts are preserved.

Monitor tool arguments use IDs from `get_monitors` or numbers starting at 1.
An omitted value, `0`, `auto`, or `all` searches all displays. Explicit monitor
references in the user's request take priority. A disconnected ID returns the
current inventory instead of raising a tool traceback or guessing a destination.

Window targets are refreshed by their native window handle and process ID. A
Discord channel/server name, unread count, or browser tab title changing does not
invalidate the app. Each refresh still checks current bounds and visibility;
closed, replaced, or fully covered targets return an `unavailable` result and
clear pointing instead of producing a tool traceback.

Clear requests such as "Where is Discord application on my screen?", "Find my
browser", and "Show me the download button on monitor 2" go directly to verified
spatial targeting. App-window lookups use no chat or vision model calls; Capri
points first and gives a short local reply. Named controls can still require UI
Automation/OCR/vision time. Compound questions and explanations keep the normal
AI route. Ambiguous or missing targets are reported instead of guessed.

Background image interpretation is paused as soon as speech starts, before
transcription, and when a text request begins. Live capture keeps running during
conversation. Piper is loaded once into a reusable local process at startup;
voice mode also checks it before listening and reloads it if needed. Stop can terminate an in-progress synthesis,
and the next reply restarts the process if needed. The process keeps the voice
model in memory until Capri exits, without storing conversation audio permanently.

Ordinary conversation, including general League strategy, streams text into chat
and starts speaking each completed sentence while the model generates the rest.
It uses recent conversation and available screen context without waiting for a
new screenshot. The compact conversation model has a short reply budget and stays
loaded for 15 minutes when memory permits. Qwen3's separate thinking mode is
disabled on this route. Execution requests and explicit memory saves retain the
existing tool engine. Stop cancels generation, synthesis, and queued sentences;
the final reply is not spoken a second time.

With VISION ON and League running in a match, Capri can read your own health,
gold, level, champion, items, and score through Riot's read-only
[Live Client Data API](https://developer.riotgames.com/docs/lol#live-client-data-api).
Questions such as "How much gold do I have?" and "What's my health?" can answer
directly without either model. General strategy can use those stats as context.
The connection stays on `127.0.0.1:2999`, uses no account credentials, discards
other players' information, and caches for only one second. Turning VISION OFF
also disables these reads. No live data is fabricated when the game is unavailable.
This does not reveal enemy locations, cooldowns, or whether a fight is safe.

League's own-player data supplements visual analysis of the actual screenshot;
it never replaces watching the game, minimap, and visible UI. A verified low-health
warning can be offered before the visual model finishes. After conversation,
background interpretation resumes after one second. A separate local game-data
watcher checks for urgent own-health warnings once per second even while the
visual model is busy. User speech still takes priority over tip delivery.

Speed settings in `config.py`:

```python
SPATIAL_QUICK_ACTIONS_ENABLED = True  # Skip model planning for clear location requests
TTS_PERSISTENT_ENABLED = True        # Keep Piper loaded between replies
WHISPER_BEAM_SIZE = 1                # Faster transcription; set 5 for wider decoding search
VOICE_SILENCE_MS = 500               # End-of-utterance wait; increase for longer natural pauses
VOICE_PRELOAD_ON_STARTUP = True      # Preload recognition/VAD/Piper; keep MIC OFF
CONVERSATION_MODEL = "auto"          # Or set CAPRI_CONVERSATION_MODEL=qwen3:8b
CONVERSATION_STREAMING_ENABLED = True
CONVERSATION_MAX_TOKENS = 256
CONVERSATION_HISTORY_TURNS = 6
MODEL_KEEP_ALIVE = "15m"
LEAGUE_LIVE_DATA_ENABLED = True      # Also requires VISION ON
LEAGUE_REQUEST_TIMEOUT_SECONDS = 0.4
LEAGUE_CACHE_SECONDS = 1.0
VISION_INTERVAL_SECONDS = 3
VISION_RESUME_DELAY_SECONDS = 1
VISION_BACKGROUND_MAX_TOKENS = 450
VISION_QUESTION_MAX_TOKENS = 850
VISION_CONTEXT_TOKENS = 8192          # Input images/context plus room for the answer
VISION_MAX_CONTEXT_TOKENS = 16384     # Bounded automatic expansion after a context overflow
VISION_LIVE_CAPTURE_ENABLED = True
VISION_CAPTURE_FPS = 8                 # Capture target; not model reasoning FPS
VISION_LIVE_IMAGE_SIZE = (960, 600)     # Lightweight continuous frames
VISION_LIVE_BUFFER_SECONDS = 3
VISION_LIVE_BUFFER_FRAMES = 24
VISION_LIVE_BUFFER_MB = 24
VISION_TEMPORAL_FRAMES = 2
VISION_TEMPORAL_SPACING_SECONDS = 1
VISION_LIVE_FRAME_MAX_AGE_SECONDS = 1
```

The console logs `Direct spatial action: pointing in ... ms (no chat model)` so
you can measure the actual action delay on your desktop. This starts after text
entry/transcription and includes target verification plus GUI acknowledgement.
Speech recognition, audio-device startup, and complex model-based screen analysis
have additional costs; those operations cannot be guaranteed instantaneous.
`Conversation model`, `Conversation first text`, `First speech audio`, and
`Direct League answer` logs distinguish model selection, text generation, audible
response, and direct game-data latency. Speech timing starts after transcription;
it does not include microphone recording or recognition time.
`Screen analysis (question/background)` logs the actual image-analysis latency.

Adjust these defaults in `config.py`, then restart:

```python
AUTONOMOUS_MOVEMENT_ENABLED = True  # Optional idle personality movement
TARGET_HIGHLIGHT_ENABLED = True
TARGET_HIGHLIGHT_DURATION_MS = 5000  # Maximum outline lifetime per gesture
TARGET_LIGHTNING_ENABLED = True     # Hand-to-target channel while pointing
TARGET_LIGHTNING_INTENSITY = 1.0     # Bolt irregularity, branches and sparks (0.1-3)
SPATIAL_POINTING_HOLD_MS = 2500      # Minimum hold for a silent typed reply
SPATIAL_POINTING_MAX_MS = 30000      # Waiting pose timeout; active speech owns its pose
SPATIAL_TARGET_TTL_SECONDS = 180     # Target ID lifetime; every action still revalidates
POINTING_LEFT_HAND_ANCHOR = (0.17, 0.25)
```

Preview both directions using the real SVG and lightning renderer without screen
capture, AI inference, or audio:

```powershell
.\.venv\Scripts\python.exe preview_pointing.py
.\.venv\Scripts\python.exe preview_pointing.py --dpr 2 --output-dir artifacts/spatial-2x
```

This produces `lightning-target.png` and an animated `lightning-target.gif`.

`Standby` is the normal conversation idle state. A `SCREEN ERROR` indicates a
capture or model failure; the VISION button's tooltip and console show its cause.
Temporary capture errors now reopen the capture handles and retry at the usual
interval, including after display-mode changes. The error clears only after a
successful screen analysis. Identical repeated failures are logged once until
recovery; permanent model/dependency failures still require fixing their cause.

The coordinate conversion follows [Qt's per-screen high-DPI geometry](https://doc.qt.io/qt-6.8/highdpi.html)
and [UI Automation's physical bounding rectangles](https://learn.microsoft.com/en-us/dotnet/api/system.windows.automation.automationelement.boundingrectangleproperty).
Physical and logical origins are stored independently; scaling is applied relative
to each monitor, and resized image dimensions are recorded explicitly.

Windows is the native grounding platform. Some games, protected windows, elevated
apps, or controls that expose no accessibility data cannot be precisely grounded.
Other platforms can use OCR when installed; mixed-DPI captures are rejected when
the backend cannot establish a reliable physical mapping. Voice interruption
during playback still requires Stop; automatic microphone barge-in remains planned.
See [SPATIAL_SMOKE_TEST.md](SPATIAL_SMOKE_TEST.md) for automated coverage and live checks.

## Screen awareness

**VISION LIVE** means a continuous capture worker is running independently of
conversation and model inference. It follows the foreground app's monitor,
including while Capri listens, speaks, or thinks. Its tooltip reports measured
capture FPS and the newest frame's age. Turning VISION OFF stops capture and
clears the frame buffer. Display/app changes discard the earlier frame sequence;
capture failures reopen the backend and retry without blocking the GUI.

The vision model compares two recent frames, about one second apart, with explicit
timestamps and source labels. It can interpret visible changes over time rather
than receiving only one isolated image. A slow model does not accumulate a queue:
the next analysis uses the newest available sequence. Results from an obsolete
app/display are discarded, and large intervening scene changes suppress stale tips.
This is continuous **capture**, with slower interpretation of selected frames;
the current model does not understand every frame at the game's refresh rate.

Screen awareness is enabled at startup. Explicit screen questions trigger
fresh screenshots of every connected monitor, supplied together with monitor IDs,
dimensions, and resize transforms. Explicit location requests use the spatial
tools above and only invoke visual analysis when native grounding needs help.
Questions about gameplay or recent changes can also receive an earlier frame from
the active display, clearly distinguished from the current monitor images.
Ordinary knowledge and conversation questions use cached context; they do not
wait for a new screenshot. Requests such as "Watch my gameplay and give me tips",
"What should I do now?", "How am I doing?", and "Any advice?" also trigger a fresh
visual read; general questions such as "How can I improve at League?" stay on the
fast conversation route.
"Can you see my screen?" and "Can you watch my screen live?" are answered from
the application's actual capture and interpretation state. Model self-descriptions
cannot override a running feed, and a running feed is not presented as instant
understanding when analysis is still pending.
Straightforward screen questions are answered directly by the vision model in
Capri's voice, avoiding an extra round through the larger chat model. Actions,
memory requests, and questions needing broader context stay on the normal chat
and tool path. Capri can use visible game HUDs, app dialogs, code,
errors, web pages, and documents. If a visual read fails, Capri reports the actual
capture/model error without waiting for a second model. Background failures also
reach the conversation context, and a successful retry clears the error. VISION
OFF remains distinct from an enabled reader that is waiting or retrying.

Continuous capture targets 8 FPS, independently of model speed. Background analysis
checks the newest buffered frames at a minimum three-second interval between
analysis starts. If analysis takes longer, the next check can start as soon as it
finishes; there is no extra three-second wait. Failures back off before retrying.
Changed frames are analyzed, and an unchanged scene
is refreshed after 15 seconds. On Windows, Capri remembers the last external
app while her own chat has focus, follows that app's monitor, and uses native
physical-pixel window rectangles for masking. Elsewhere, she uses the monitor
containing the character. Descriptions expire 60 seconds after image capture,
not after model processing finishes.

Explicit screen reads are resized to fit 1600 x 1000 for readable interface text;
continuous frames fit 960 x 600 to reduce capture/encoding overhead. Her character,
chat, controls, and advice bubble are masked. Target outlines and electrical effects
are excluded from capture where supported, otherwise masked. Screenshots stay in RAM, are sent
only to local Ollama, and are not saved. The live buffer holds at most three
seconds, 24 frames, and 24 MB of compressed image data, whichever limit is reached
first. It is cleared on VISION OFF and exit. A short history of descriptions helps
interpret changes; historical observations are explicitly distinguished from
the current image. Neither screenshots nor observations become long-term user
memories. Screen text is data, never instructions to execute.

### When she speaks up

The vision model produces structured assessments. An unsolicited tip requires
all of: a concrete action useful right now, high confidence, specific visible
evidence, and a non-sensitive scene. Ordinary settings screens, generic
strategy, encouragement, and uncertain guesses stay silent. She cannot issue
computer tool calls on her own through this advice path.

A tip appears in a brief **CAPRI // HEADS UP** bubble and in chat, and is spoken
when voice output is installed. The bubble does not activate a window or steal
game focus. Tips older than 20 seconds or belonging to a changed active app are
discarded. Repeated/similar warnings are suppressed for 120 seconds, with a
5-second minimum gap between different tips. **STOP** also suppresses new tips
for 15 seconds. Click a bubble to dismiss its text, or use **STOP** to stop speech.

Your questions take priority over unsolicited speech. Speech detection reserves
that priority directly on the voice thread, before any GUI event arrives.
Pending tips are cancelled when you start speaking; normal replies cannot be
cut off by a tip. Advice waits for an 8-second conversation gap, and frames from
before your latest turn cannot produce a late interruption. Failed or cancelled
tips have a 20-second retry backoff. The same spoken warning cannot bypass
suppression just because an app/window ID or generated issue name changes.

The microphone input stream is closed while Capri speaks, followed by a
0.4-second speaker-tail delay. Echo age is measured when speech starts, so slow
transcription cannot let old speaker audio slip through. A shared guard rejects recent output echo and
suppresses repeated long voice answers for 45 seconds. Explicit requests such
as "say that again" remain allowed, as do short answers such as "yes". Whisper
runs without previous-text conditioning, applies speech filtering, and drops
unreliable or repetitive segments. These are feedback safeguards; full acoustic
echo cancellation and voice interruption during playback are planned next.

Screen history contains screen observations only. Recent user/assistant dialogue
is supplied separately for the latest question, so an earlier screen answer is
not recycled as a fresh observation. Background reads do not receive that dialogue.
Background image inference pauses during transcription and streamed replies;
continuous capture keeps running, and an explicitly requested visual answer still
runs. Background inference resumes after the configured conversation delay.

Visual assistance cannot see hidden enemies, unreadable UI, unseen document pages,
or events between frames. League's own-player API adds the limited live stats
described above; other game assistance remains screenshot-based.
Latency includes model loading, image analysis, and speech generation. A local
synthetic HUD check with the installed vision model correctly read 8/100 health:
the first question took 19.7 seconds, a subsequent background read took 4.1 seconds,
and a separate visible-danger test produced a tip in 9.5 seconds. These are test
images, not a live-match benchmark. Busy GPUs and model switching can increase
that delay. Visual advice arrives after analysis, so it cannot reliably guide
split-second reactions. Exclusive-fullscreen or protected content may not
capture; use borderless/windowed mode if necessary. Adjust sampling, image size,
freshness, and repeat limits in `config.py`.

In a separate continuous-feed check, the real model correctly identified a health
drop from 95/100 to 8/100 across two synthetic frames in 11.4 seconds. Capture
continued throughout, producing 85 additional frames at about 7.3 FPS and retaining
only the bounded recent history. This verifies concurrent capture/interpretation;
it does not benchmark actual gameplay capture, which remains dependent on Windows
desktop access and graphics drivers.

The configured 32B tool model took about 91 seconds in an earlier local answer
check. Ordinary conversation now uses the compact streaming route, and direct
screen answers avoid the extra chat call. Tool requests still use `CAPRI_MODEL`.
Those historical timings do not benchmark the new conversation route; measure
the first-text/audio logs with Ollama running on your hardware.

## Voice and troubleshooting

- Vision errors show their actual reason in chat, speech, and the VISION tooltip.
  All vision and visual-location requests explicitly reserve an 8,192-token
  Ollama context. A confirmed context-size rejection retries once with enough
  room for the server-reported input size, answer budget, and a small margin,
  up to 16,384 tokens. Successful expansion is reused for later frames. Images,
  monitor coordinates, and timeline metadata are preserved during this retry.
  Larger context limits consume more model memory; both limits are configurable
  using the environment variables above.
  Capture and model-analysis failures recover independently: a healthy live feed
  clears its old capture error even while model analysis is paused, and does not
  hide an unresolved model error. A truncated/invalid structured model answer is
  retried once with compact context and a bounded larger output budget; partial
  output never becomes a tip.
  Diagnostic failures are saved to `data/vision.log` (or `CAPRI_DATA_DIR/vision.log`),
  with two rotating 512 KB backups. This log records error reasons, not screenshots
  or conversation text. It is available even after the console closes.
- Microphone input uses the system default input device at 16 kHz. Playback uses
  the system default output device. Check Windows microphone permissions and
  device selection if recording fails.
- Whisper loads on a background startup worker, or on demand when preloading is
  disabled, with CUDA when the installed Torch build
  reports CUDA support and CPU/int8 otherwise. The pinned default Windows
  installation uses CPU Torch. GPU setup requires a matching CUDA-enabled
  Torch/torchaudio build and the CUDA libraries required by CTranslate2; follow
  the [faster-whisper installation documentation](https://github.com/SYSTRAN/faster-whisper#installation).
- Stop can cancel network inference and synthesis immediately. First-time model
  initialization and an in-progress Whisper inference segment must finish
  before that worker can exit. The GUI remains responsive while it waits.
- Piper runs in a hidden subprocess. Stop terminates it; temporary WAV files
  are cleaned up. Playback uses SoundDevice and can stop mid-reply.
- If a chat model is missing, the error includes its `ollama pull` command.
  If voice dependencies are missing, the chat panel explains how to install them.
  Errors and visible answers remain in chat even if speech output fails.
  Startup preload failures only update the console and MIC tooltip; text chat
  stays available, and explicitly enabling voice retries preparation.
- Application tools support Notepad, Calculator, Paint, Explorer, and Edge.
  Browser search opens Google in your browser; it does not retrieve search
  results into the model. File tools list, find, and open existing paths.

## Next upgrade

See [UPGRADE_PLAN.md](UPGRADE_PLAN.md) for the ordered roadmap and acceptance
checks. Audio isolation and interruption come first, followed by latency,
incident-based advice, screen profiles, and diagnostics.

## Development and verification

Direct dependencies are pinned for Windows with Python 3.11. Transitive
packages are resolved by pip; these files are not a cross-platform lockfile.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
```

Tests use temporary databases, an offscreen Qt backend, simulated microphone
input and screen frames, and an HTTP mock transport with the real Ollama SDK.
They cover multi-step tools, budgets, invalid calls, live context, concurrent
memory saves, cancellation, speech detection, vision refresh/masking, typed
conversation, fresh screen questions, urgency thresholds, tip suppression,
user/speech priority, self-reply loops, repetition suppression, and clean shutdown. If Piper is installed, the bundled voice is
also synthesized into a WAV and inspected without playing it.

Teleport checks cover immediate positioning, drag bypass, overlapping bursts,
click-through flags, expiry/reuse, rapid jumps, multi-display coordinate clamping,
and first-frame visibility at device-pixel ratios 1, 1.5, and 2.
Spatial checks also cover monitor transforms, occlusion, edge placement, uncertain
targets, stale coordinates, request replacement, mirrored pose anchors, highlight
expiry, and action/response/audio ordering. They use synthetic screens and window trees.

Live Ollama inference, real microphone/speaker behavior, and screen capture on
mixed-DPI/multiple monitors require a manual smoke test on the target desktop.
No test accesses the real microphone or captures the desktop.

## Project map

| Path | Responsibility |
| --- | --- |
| `main.py`, `config.py` | Startup, persona, model settings and limits |
| `core/chat.py` | Shared conversation loop, live context, tools, history |
| `core/model_client.py`, `core/cancellation.py` | Cancellable local model requests |
| `core/conversation.py`, `core/streaming.py` | Compact model selection, streamed text and sentence speech |
| `core/league.py` | Read-only own-player League data, direct stat answers and game context |
| `core/screen_access.py` | Screen capability/status replies and shared visual-context instructions |
| `core/live_screen.py` | Continuous capture, bounded temporal frame history and recovery |
| `core/context.py`, `core/memory_detector.py` | Recent actions, fresh screen context, fact detection |
| `core/desktop.py`, `core/proactivity.py` | Foreground monitor, structured screen assessments, advice policy |
| `core/monitors.py`, `core/spatial_geometry.py` | Monitor inventory, physical/logical transforms, safe placement |
| `core/window_grounding.py`, `core/text_grounding.py` | Visible windows, UI Automation, optional local OCR |
| `core/spatial.py`, `tools/spatial.py` | Verified target registry, cancellation, and spatial AI tools |
| `core/interaction.py` | Conversation priority, playback echo, and repeated-answer guards |
| `gui/window.py`, `gui/chat_panel.py`, `gui/controls.py` | Desktop character, text UI, and click controls |
| `gui/workers.py`, `gui/vision.py`, `gui/advice_bubble.py` | Voice/text/advice threads, screen reading, and tip display |
| `gui/sprites.py`, `gui/styles.py` | Activity expressions, hot reload, appearance |
| `gui/teleport.py`, `gui/silhouette.py` | Procedural lightning overlays and cached sprite outlines |
| `gui/spatial.py` | Movement/gesture coordinator and click-through target highlight |
| `preview_teleport.py` | Render the actual teleport effect to PNG/GIF without running the assistant |
| `voice/voice.py`, `voice/tts.py` | Recording/transcription and synthesis/playback |
| `memory/database.py` | Thread-safe SQLite facts |
| `tools/` | Approved applications, paths, browser, and system status |
| `tests/` | Regression and integration checks |

Sprite SVGs are discovered and hot-reloaded from `assets/sprites`. The current
expressions are idle, listening, speech detected, thinking, talking, happy, and pointing left.
The personality remains in `SYSTEM_PROMPT` in `config.py`.
