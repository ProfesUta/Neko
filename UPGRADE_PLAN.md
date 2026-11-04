# Capri: next upgrade plan

The next release should make conversation reliable during gameplay before adding
more autonomous behavior. The current fix adds echo/reply guards, cleaner screen
question context, and conversation priority. It still closes microphone input
while Capri speaks; continuous listening with acoustic echo cancellation is
planned work below.

The spatial foundation is now implemented: monitor inventory and transforms,
all-monitor question images, verified window/accessibility targets, contextual
teleportation, mirrored pointing, and a temporary target outline. Live desktop
acceptance checks are in [SPATIAL_SMOKE_TEST.md](SPATIAL_SMOKE_TEST.md).

## 1. Audio control and interruption (highest priority)

- Add an optional push-to-talk hotkey and input/output device selection, with
  preferences saved across restarts. Keep hands-free mode available.
- Introduce one audio controller that owns microphone capture, playback, speech
  detection, and cancellation. Keep the new shared conversation guard as the
  authority for user priority.
- Evaluate a Windows capture path with a speaker-output reference and acoustic
  echo cancellation. Separate user speech from Capri and game audio before
  transcription. Transcript similarity remains a backup safeguard.
- Enable voice interruption during playback only after the echo-cancelled path
  passes replay and real-device checks. Push-to-talk should interrupt immediately.

Acceptance: a 10-minute speaker/game/conversation test creates no turns from
Capri's own speech. The user can interrupt through push-to-talk, and an explicit
"say that again" still works. Test headphones, speakers, quiet speech, game
voices, and rapid microphone toggles. Measure interruption delay; target under
300 ms for the hotkey path.

## 2. Faster, predictable responses

- Add per-turn timings for capture, image analysis, transcription, tools, synthesis,
  and playback. Record warm and cold baselines on this machine first.
- Benchmark the installed models for simple conversation, screen questions, and
  complex tool requests. Route simple work to the fastest model that meets the
  accuracy checks; preserve a larger-model option for difficult requests.
- Keep the Piper voice loaded instead of starting a model process for every reply.
  Add sentence-based speech generation with one cancellation owner.
- Schedule user requests ahead of background vision, avoid duplicate image
  analysis, and measure GPU/model swapping before choosing warm-model settings.

Acceptance: publish median and 95th-percentile timings for the same test prompts;
reduce the common screen-question delay from the measured baseline. Stopping or
switching tasks must never replay old speech or duplicate a tool action.

## 3. Advice tied to incidents

- Track a visible issue through "new", "already reported", and "resolved" states.
  Rearm a warning only when evidence shows the issue cleared and returned.
- Add advice categories and per-game/app preferences, including a timed quiet mode.
- Use current evidence, confidence, conversation activity, and delivery history
  together. Keep autonomous advice unable to execute computer tools.

Acceptance: unchanged low health or a persistent error produces one warning
through a long session. A genuinely new verified danger can produce another.
Changing window IDs, paraphrasing, stale results, and failed speech cannot bypass
suppression. User speech and replies retain priority.

## 4. Screen focus and readable details

- Build on the implemented monitor references and window targeting with saved
  screen profiles and optional regions for HUDs, terminal errors, and small text.
  Show which app Capri is reading.
- Adapt capture frequency and image size to app activity and available performance.
- Build a fixture set of games, menus, browser pages, code, and errors, including
  app switching and mixed-DPI monitor layouts.

Acceptance: answers identify the selected/current screen and acknowledge
unreadable details. An old frame from another app cannot answer a new question.
No screenshots are saved unless an explicit diagnostic session enables it.

## 5. Diagnostics and release checks

- Add an opt-in local diagnostics panel showing stage timing and decisions such as
  "probable echo ignored", "duplicate reply suppressed", and "advice deferred".
  Keep raw audio, screenshots, and transcripts off by default.
- Extend deterministic replay tests to simultaneous audio, screen changes,
  cancellation, model errors, repeated answers, and failed playback.
- Automate the Windows/Python 3.11 regression suite and retain a real-device
  smoke-test checklist for every release.

Acceptance: a repetition report can be reproduced from non-sensitive event
metadata or an explicitly provided fixture. Existing memory data survives
upgrades, and cancellation always restores usable controls.

## Delivery order

Ship push-to-talk, device controls, and diagnostics first. Then validate audio
echo cancellation and voice interruption. Follow with response scheduling and
persistent Piper, incident-based advice, and screen profiles. Each stage must
pass the previous stage's tests before adding the next feature.
