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
ollama pull qwen2.5vl:7b
.\.venv\Scripts\python.exe main.py
```

Ollama must be running locally on `127.0.0.1:11434`. If its desktop application
is not already running, start `ollama serve` in another terminal. The default
32B chat model needs substantial available memory; choose another installed,
tool-capable model with `CAPRI_MODEL` if needed. Capri does not automatically
pull chat or vision models.

For text chat without installing the voice stack, install
`requirements-core.txt` instead. The GUI and screen awareness work without
loading Whisper, Torch, or Piper. Install `requirements.txt` when adding voice.

The bundled Piper voice is `assets/voices/en_US-amy-medium.onnx`, together with
its adjacent JSON configuration. Whisper `base` downloads its model on first
microphone use; Silero's detector is bundled in its Python package. Initial
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
$env:CAPRI_VISION_MODEL = "your-installed-vision-model"
$env:CAPRI_VISION = "1"  # Set 0 to start with all screen reading off
$env:CAPRI_ADVICE = "1"  # Set 0 for screen questions without unsolicited tips
$env:CAPRI_DIRECT_SCREEN_ANSWERS = "1"  # Set 0 to always use the main chat model
$env:CAPRI_WHISPER_MODEL = "base"
$env:CAPRI_WHISPER_LANGUAGE = "en"
$env:CAPRI_DATA_DIR = "F:\CapriData"
.\.venv\Scripts\python.exe main.py
```

There is no automatic `.env` loader. By default, existing facts remain in
`data/memory.db`; the schema is compatible with the previous version. Facts
persist across launches. Conversation text stays in RAM, and the most recent
30 turns are supplied to the model. The transcript displays up to 1,000 text
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
The response waits for the GUI to confirm arrival. Coordinates are refreshed
again before displaying the answer and before starting synthesized speech. If a
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

Screen awareness is enabled at startup. Ordinary typed or spoken questions trigger
fresh screenshots of every connected monitor, supplied together with monitor IDs,
dimensions, and resize transforms. Explicit location requests use the spatial
tools above and only invoke visual analysis when native grounding needs help.
Straightforward screen questions are answered directly by the vision model in
Capri's voice, avoiding an extra round through the larger chat model. Actions,
memory requests, and questions needing broader context stay on the normal chat
and tool path. Capri can use visible game HUDs, app dialogs, code,
errors, web pages, and documents. If reading fails, that failure is supplied to
the conversation model; she must not claim to see a fresh screen.

Background sampling checks the active app's monitor every 5 seconds after the
previous analysis completes. Changed frames are analyzed, and an unchanged scene
is refreshed after 15 seconds. On Windows, Capri remembers the last external
app while her own chat has focus, follows that app's monitor, and uses native
physical-pixel window rectangles for masking. Elsewhere, she uses the monitor
containing the character. Descriptions expire 60 seconds after image capture,
not after model processing finishes.

Images are resized to fit 1600 x 1000 for readable interface text. Her character,
chat, controls, and advice bubble are masked. Target outlines and electrical effects
are excluded from capture where supported, otherwise masked. Screenshots stay in RAM, are sent
only to local Ollama, and are not saved. A short history of descriptions helps
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
Background vision pauses during transcription and user-question inference; the
focused read for that question still runs. It resumes while she listens or speaks.

This is screenshot-based assistance. She has no game-state API and cannot see
hidden enemies, unreadable UI, unseen document pages, or events between frames.
Latency includes model loading, image analysis, and speech generation; a synthetic
local check took about 6-8 seconds for warm image analysis and about 19 seconds
for the first image after switching models. Busy GPUs and slower hardware can
increase that delay. Advice arrives after analysis, so it cannot reliably guide
split-second reactions. Exclusive-fullscreen or protected content may not
capture; use borderless/windowed mode if necessary. Adjust sampling, image size,
freshness, and repeat limits in `config.py`.

The configured 32B chat model took about 91 seconds in a separate local answer
check. Direct screen answers avoid that extra call. Complex questions and tool
requests still use `CAPRI_MODEL`, so their speed depends on your chosen chat model.

## Voice and troubleshooting

- Microphone input uses the system default input device at 16 kHz. Playback uses
  the system default output device. Check Windows microphone permissions and
  device selection if recording fails.
- Whisper loads on the voice worker, with CUDA when the installed Torch build
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
