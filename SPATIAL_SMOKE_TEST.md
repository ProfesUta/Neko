# Spatial upgrade verification

Run automated checks from the project directory:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
```

Tests use synthetic screen pixels, window/accessibility fixtures, mock model
responses and audio playback, and Qt's offscreen backend. No test reads the real
desktop or microphone.

| Scenario | Automated coverage | Live desktop check |
| --- | --- | --- |
| One monitor | GUI action, placement, overlays, cleanup | Ask where a visible app is |
| Two or more monitors | Per-image IDs/transforms and monitor associations | Put Discord on the other monitor and ask for it |
| Secondary left/above/below | Negative/vertical geometry and placement | Verify arrival on the requested physical display |
| Different scaling/resolution | Physical/logical/image round trips and friendly-name/native-ID mapping | Use 100% and 150%/200% displays together |
| Target on primary/secondary | Native/UIA target resolution and explicit monitor filtering | Repeat requests in both directions |
| Target near an edge | Candidate scoring and sprite bounds | Move a small target to each corner |
| Partially/completely covered | Visible window fragments; covered controls rejected | Cover part of the target with another app |
| Missing/ambiguous target | No action target; clarification/unverified results | Minimize the requested app or open two matching windows |
| Moving/disappearing target | Refresh before GUI acknowledgement and response; cancellation | Move/close/minimize the target during inference and speech synthesis |
| Rapid requests | Earlier inference cancelled; latest request owns position | Send a second location request while the first is pending |
| Normal speech after pointing | Gesture priority, restoration, and playback acknowledgement | Check listening/talking expressions after completion |
| Cross-monitor electricity | Existing global-coordinate VFX plus placement/conversion checks | Verify simultaneous departure/arrival bursts across actual displays |
| Monitor unplugged | Target revision rejection, gesture clearing, safe recovery | Unplug/reconnect the display Capri occupies |
| Input interception | Native Qt overlay flags and offscreen checks | Click through the target outline/VFX; drag and click Capri normally |
| Highlight lifetime | Timer expiry and cancellation cleanup | Check optional red outline and TARGET_HIGHLIGHT_ENABLED=False |
| Hand-to-target lightning | Exact endpoints in both poses; negative coordinates and DPR 1/1.5/2 rendering | Verify fingertip alignment, visible target reticle, click-through and cleanup during speech |
| Monitor zero/default | String/numeric zero, null, all-display search and explicit user monitor precedence | Ask for an app on another display without naming its monitor |
| Failed follow-up lookup | Previous beam/target cleared on not-found, invalid display and ambiguous lookup | Ask for a nonexistent target after a successful pointing request |
| Discord channel/server titles | Refresh by window handle + process ID; changing titles, bounds and occlusion; no switch to another window | Ask for Discord with a channel/server open, then change channels during the explanation |
| Transient screen errors | Capture initialization/grab/model retries; error persists until success | Switch fullscreen modes or lock/unlock, then check VISION recovers |

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
