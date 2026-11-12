# Capri Phase 4: response playback and recognition audit

Measured November 12, 2026, on the same Windows workstation: Python 3.11.6, 16 logical Intel CPU processors, RTX 3060 12 GiB. Installed engines: faster-whisper 1.2.1, CTranslate2 4.8.5, torch 2.8.0, sounddevice 0.5.6, sherpa-onnx 2.0.1 / native ONNX Runtime 1.28.2. Kokoro remains CPU-based, speaker 53, base speed 1.1, eight tuned synthesis threads. Whisper remains `base`, CPU int8, greedy/text-only decoding. No voice assets, pronunciation dictionary, personality, SVGs, conversation model, League code, or dependencies were replaced.

## Shipped change and controlled result

The production optimization is **Windows shared-mode WASAPI output on a uniquely matched version of the currently selected output device**, with the original PortAudio driver as fallback. It preserves one output stream per reply, reused across its chunks. It does not keep a stream running while Capri is idle, change the global audio device, change volume, record audio, or switch to an unrelated speaker.

Recognition thread experiments were **not** adopted as a new default. `CAPRI_WHISPER_THREADS=0` retains the original automatic choice. Both VAD filters and the 500 ms capture silence threshold remain unchanged. Phase 2's six-second inference grace, cancellation, microphone synchronization and Phase 3 TTS settings remain intact.

The final conversational comparison alternates AB/BA order, with identical saved input PCM, model/prompt/seed/empty fixture memory, and the same recognition settings. Each side has an unmeasured complete warm-up turn, then **20 measured warm replies**. The only intentional behavioral difference is the output driver. The question is “Say hello briefly.” Recognition transcripts matched in all 20 pairs. The existing `auto` configuration selected `qwen3:8b`; primary `qwen2.5:32b` configuration/selection behavior was preserved. This is not a 32B voice benchmark.

Times are milliseconds. At n=20, the project's order-statistic percentile convention makes p95 and p99 equal; maximum is separate.

| Measurement | Baseline p50 / p95 / p99 / max | Optimized p50 / p95 / p99 / max | n per side |
|---|---:|---:|---:|
| Speech-end estimate to captured loopback frame's QPC onset | 1969.38 / 2007.94 / 2007.94 / 2010.33 | 1845.50 / 1868.58 / 1868.58 / 1898.78 | 20 |
| Speech-end estimate to first non-silent loopback packet received | 1964.91 / 2000.70 / 2000.70 / 2009.83 | 1838.38 / 1862.06 / 1862.06 / 1888.22 | 20 |
| Speech-end estimate to PortAudio DAC estimate | 1968.21 / 2007.67 / 2007.67 / 2009.53 | 1826.95 / 1848.60 / 1848.60 / 1879.62 | 20 |
| Decoded host PCM ready to DAC estimate | 209.72 / 215.84 / 215.84 / 224.14 | 71.78 / 82.06 / 82.06 / 82.28 | 20 |
| Physical sound arriving at the listener | Unavailable | Unavailable | 0 |

The difference of loopback-onset medians is **123.88 ms, 6.29%**. The paired median reduction is **123.43 ms**, with a fixed-seed, 2,000-resample paired-bootstrap 95% interval of **112.62–132.63 ms**. These describe this small controlled fixture cohort, not all future conversations or hardware. No underflows or empty initial output callbacks were reported in either arm.

Source: [final paired replay](artifacts/performance/phase4_final_paired.json). This short greeting is different from Phase 3's gravity fixture. **Do not compare 1845 ms with Phase 3's 2745 ms and call that an improvement:** the fixtures and endpoints differ. The valid headline comparison is the identical-input driver A/B above.

## Measurement definitions

The existing tracing/cancellation pipeline is reused. Profiling remains disabled by default; use `CAPRI_LATENCY_METRICS=1`. Numeric timings use `perf_counter_ns()` and are bounded by the existing metrics collector. Normal telemetry contains no raw audio, transcripts or game data.

| Stage | Meaning |
|---|---|
| Speech end | Annotated/PCM-threshold endpoint for replay; Silero's unpadded sample-index estimate in live input. Unknown live input-device/acoustic latency remains. |
| VAD end | Silero reports end of utterance after its configured silence/hysteresis. |
| Capture stopped / complete | Input stream has closed; concatenation of retained capture/pre-roll blocks has completed. Replay marks do not measure real input-device teardown. |
| STT frontend / decode | Eager faster-whisper filtering/features, then lazy encoder/decoder segment consumption. Whole recognition timing remains available. |
| A: native PCM | Native Kokoro has converted its first samples to PCM16, before WAV delivery. Parent WAV availability and decoded host PCM readiness remain separate marks. |
| B: playback queue | First decoded PCM job is queued, before opening/starting the device. |
| C: non-silent callback | PortAudio callback carrying the first sample above the existing amplitude threshold begins. Not playback presentation. |
| D: DAC estimate | PortAudio's reported presentation time, adjusted for the first non-silent sample's offset within that callback. Not calibrated speaker output. |
| E: loopback | Explicit WASAPI capture observes non-silent digital mix samples. Both the sample's WASAPI QPC timestamp and packet-receipt time are reported. |
| F: acoustic output | Requires separate calibrated recording; not measured here. |

The new loopback helper is imported only by explicitly invoked benchmarks. It opens a **shared, passive render-endpoint capture**, never a microphone, monitor, exclusive output or application recording service. It inspects one packet in RAM and discards PCM immediately; it saves no capture. An initial quiet guard rejects a continuously noisy mix. All other audio must stay quiet: a sound starting after arming could still be mistaken for Capri. It does not isolate Capri from game/music audio.

The tested mix format is 48 kHz, stereo float32. QPC positions returned by WASAPI are converted from 100 ns units and adjusted by the detected frame's sample offset. The loopback threshold is 0.001 full scale, approximately the existing PCM16 threshold of 32. WASAPI can deliver a packet containing frames whose QPC presentation time lies **after packet receipt**. Receipt and sample timestamps therefore remain separate; negative signed offsets must not be called zero-latency delivery.

The fixed-PCM test found loopback QPC onset approximately **1.52 ms after the MME DAC estimate**, versus **21.35 ms after the WASAPI DAC estimate**. This driver-dependent discrepancy is why the DAC value alone is not a physical playback measurement. The capture run reported zero invalid timestamps and one initial discontinuity flag, not proof of acoustic glitch-free output. Shared loopback may keep the audio engine awake; both comparison arms used the same capture conditions.

References: [Microsoft loopback recording](https://learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording), [IAudioCaptureClient packet/QPC semantics](https://learn.microsoft.com/en-us/windows/win32/api/audioclient/nf-audioclient-iaudiocaptureclient-getbuffer).

## Remaining pipeline delay

Final greeting cohort, p50 / p95 / p99 / maximum milliseconds, n=20 each:

| Stage interval | Original driver | Shared WASAPI |
|---|---:|---:|
| Speech end to VAD | 562.57 / 564.28 / 564.28 / 564.45 | 563.83 / 564.31 / 564.31 / 564.41 |
| VAD to transcription complete | 430.43 / 468.34 / 468.34 / 471.59 | 434.02 / 452.38 / 452.38 / 464.69 |
| Recognition frontend | 5.74 / 7.86 / 7.86 / 12.18 | 7.81 / 15.80 / 15.80 / 26.25 |
| Recognition lazy decode | 422.85 / 460.34 / 460.34 / 466.42 | 424.39 / 442.92 / 442.92 / 444.32 |
| Transcription complete to request dispatch | 2.30 / 2.76 / 2.76 / 4.02 | 2.31 / 2.71 / 2.71 / 2.73 |
| Dispatch to first useful LLM text | 133.72 / 279.91 / 279.91 / 288.80 | 125.74 / 278.53 / 278.53 / 306.84 |
| First useful text to first TTS submission | 248.73 / 304.77 / 304.77 / 305.32 | 248.88 / 306.93 / 306.93 / 307.13 |
| TTS submission to first delivered audio | 358.32 / 372.46 / 372.46 / 376.35 | 357.72 / 365.23 / 365.23 / 377.46 |
| Speech end to first native PCM | 1743.03 / 1796.11 / 1796.11 / 1797.63 | 1733.37 / 1768.56 / 1768.56 / 1771.73 |
| Delivered audio to decoded host PCM | 0.39 / 0.92 / 0.92 / 1.55 | 0.41 / 1.60 / 1.60 / 1.68 |
| Host PCM to playback queue | 0.13 / 0.35 / 0.35 / 0.57 | 0.13 / 0.43 / 0.43 / 0.59 |
| Playback queue to first callback | 49.97 / 55.98 / 55.98 / 63.56 | 9.72 / 20.23 / 20.23 / 21.77 |
| Playback queue to first non-silent callback | 109.56 / 115.02 / 115.02 / 118.18 | 53.94 / 63.75 / 63.75 / 64.14 |

Stage intervals overlap; percentiles must not be added. Real microphone stream-close/assembly and human acoustic timings are instrumented/externally measurable but were not populated by these prerecorded replays.

The approximately 2745 ms historical Phase 3 audio-readiness result consisted of endpointing (~588 ms), recognition (~571 ms), first text (~239 ms), natural text accumulation (~210 ms), and TTS (~1080 ms), with overlapping setup/queue work. It was not a single unexplained GUI wait. In this short greeting, endpoint detection is the largest stage; a longer first sentence still makes Kokoro dominant. The output optimization acts **after** synthesis and does not claim faster LLM intelligence or TTS generation.

## Recognition and endpoint experiments

`benchmark_recognition.py` saves a repeatable corpus and supports a separate human-recording manifest. This run used generated speech, explicit inserted pauses, reduced amplitude and seeded noise—not human microphone recordings. Each configuration has 20 repetitions per fixed fixture, not 20 independent speakers or utterances.

Silero retained threshold 0.5, 512 samples at 16 kHz, 200 ms speech padding and ten pre-roll blocks. Sample-index endpoint delay is separate from accelerated offline computation time.

| Silence setting | Short-question endpoint delay, ms | Quiet-question delay | Noisy-question delay | Premature endings on nominal 350 ms pause | On nominal 550 ms pause |
|---:|---:|---:|---:|---:|---:|
| 300 ms | 365.56 | 312.94 | 408.94 | 20 / 20 | 20 / 20 |
| 400 ms | 461.56 | 408.94 | 504.94 | 20 / 20 | 20 / 20 |
| 500 ms | 557.56 | 504.94 | 600.94 | 0 / 20 | 20 / 20 |

Pause fixtures insert 350/550 ms between two trimmed spoken parts, each retaining about 50 ms of boundary padding. Their actual acoustic gaps are not exactly the labels and are not natural human hesitations. All thresholds produced zero false completed utterances on the silence/noise-only controls. The existing 500 ms setting can still end a longer hesitation early; this limitation was not hidden or “fixed” by making every request slower. The shorter settings do not justify changing the default without a representative human corpus. Full p50/p95/p99/max and sample counts: [corpus results](artifacts/performance/phase4_stt_corpus.json).

Four- and eight-thread recognition produced the same reference word-error counts in all tested fixtures: zero edits on the short, quiet, noisy and paused questions; one edit against 18 reference words on the long question. Silence and noise-only controls produced no hallucinated text in these samples with either filter setting. These observations are not a guarantee against hallucinations on other inputs.

The extra internal Whisper VAD is inexpensive on speech: about 5.5 ms frontend time for the short fixture and 16.4 ms for the long fixture at four threads. Disabling it reduced those frontend times to about 2.3/4.2 ms, but made silence/noise-only recognition take **534/536 ms instead of approximately 3.85 ms**. Filtering and the existing no-speech/log-probability/compression safeguards remain enabled. Source behavior: [faster-whisper](https://github.com/SYSTRAN/faster-whisper).

### Why eight recognition threads were rejected as the default

The isolated corpus favored eight threads: short recognition 555.99→408.25 ms and long 608.89→466.00 ms median. However, CPU time rose from 2.188→3.250 CPU-seconds on the short fixture. In an actual paired conversational gravity replay, four→eight threads saved only **439.87→406.61 ms** recognition median while process CPU time rose **1.703→3.141 CPU-seconds**. First-audio readiness barely changed, **2596.83→2593.48 ms**. The combined driver/thread experiment improved loopback onset, but most useful gain came from the output path. Spending nearly twice the recognition CPU time for that small conversational gain conflicts with the CPU-contention requirement.

An isolated OpenMP passive-wait experiment also measured 4/6/8 threads: short medians 553.38/481.11/408.15 ms with CPU time 1.719/1.703/2.266 CPU-seconds. Six passive threads merit further testing, but no global OpenMP wait policy was changed in Capri: it can affect other native libraries and was not validated in the same complete application/human workload. `--passive-wait` is an explicit benchmark-only experiment. References: [CTranslate2 parallelism](https://opennmt.net/CTranslate2/parallel), [OpenMP wait policy](https://openmp.llvm.org/design/Runtimes.html).

Raw: [thread/filter sweep](artifacts/performance/phase4_stt_threads.json), [full corpus](artifacts/performance/phase4_stt_corpus.json), [passive experiment](artifacts/performance/phase4_stt_passive.json), [rejected eight-thread conversational candidate](artifacts/performance/phase4_paired_replay.json). Audio preparation/model reuse were audited; repeated model loading and large unnecessary runtime conversions were not found. Capture copies/pre-roll handling were retained rather than trading reliability for microseconds.

## League mode validation and limits

The local live-game API was unavailable (bounded connection probe timed out). No active match, live champion-select connection, game FPS or real League+vision ON/OFF comparison is claimed. No extra telemetry requests were added to production instrumentation.

The existing offscreen GUI harness was exercised with real VAD/Whisper/Kokoro, real output/loopback, a known champion-select API snapshot and the deterministic role question. Each run excludes one complete warm-up and measures 20 replies. The GUI run disables real screen capture and League networking; it measures the speech/UI integration rather than the cost of an active game's telemetry.

| Fixture scenario | Speech end to loopback QPC p50 / p95 / p99 / max, ms | n |
|---|---:|---:|
| Normal desktop GUI, mock League role answer | 1522.63 / 1550.84 / 1550.84 / 1551.66 | 20 |
| Docked Companion GUI, champion-select fixture | 1512.40 / 1565.74 / 1565.74 / 1582.95 | 20 |

No LLM answer calls or PortAudio underflows were recorded in either case. These are separate cohorts; the approximately 10 ms median difference does **not** establish that Companion mode is faster or has zero overhead. [Desktop results](artifacts/performance/phase4_desktop_league_fixture.json), [Companion results](artifacts/performance/phase4_companion_league_fixture.json).

Automatic session transitions, startup/reconnection, match exit, SVG switching, movement lock and topology behavior remain covered by mocked/offscreen regression tests. Actual match / vision-enabled / vision-disabled comparisons require the live application with the same questions and comparable game conditions; the commands below enable production tracing without adding a polling system. The existing six-second background inference policy was not changed or disabled to improve these measurements.

## Output startup, reuse and interruption

The fixed-PCM device test used exactly the same saved Kokoro samples, alternating driver order. Twenty warm samples per case:

| Device experiment | Digital loopback p50 / p95 / p99 / max, ms | Variance, ms squared |
|---|---:|---:|
| Original MME, one stream per reply | 211.14 / 221.43 / 221.43 / 231.82 | 43.22 |
| Shared WASAPI, one stream per reply | 94.55 / 95.98 / 95.98 / 104.19 | 22.38 |
| Reused MME session, benchmark-only prototype | 171.33 / 184.74 / 184.74 / 185.95 | 61.80 |

Paired fixed-PCM median reduction: 118.84 ms, bootstrap 95% interval 115.01–120.58 ms. Zero PortAudio underflows were reported in all three cases. The reused prototype's gap counters include deliberate inter-trial idle time and are **not** within-reply speech gaps. Keeping a stream active saved less than changing the driver and would add device-switch/idle/shutdown lifecycle work. No persistent output service was added. [Device experiment](artifacts/performance/phase4_playback.json).

WASAPI remains shared mode with Windows format conversion enabled: Kokoro's PCM16/24 kHz format is retained, while this endpoint mixes at 48 kHz. Endpoint selection uses a unique full or MME-truncated friendly-name prefix match; ambiguous/virtual/unavailable matches retain the original route. It is resolved per reply rather than pinned in an always-open stream. Open/start failures can fall back before samples are consumed. A partially started stream never replays already consumed PCM. Mock tests cover device selection changes, ambiguity, open/start errors and cancellation; physical hot-plugging was not tested.

Real interruption results, 20 injections each:

| Interval | p50 / p95 / p99 / max, ms |
|---|---:|
| Interruption token set to PortAudio abort return | 3.11 / 4.82 / 4.82 / 4.94 |
| Final native chunk cancellation to next warm voice ready | 3.22 / 12.81 / 12.81 / 14.92 |
| Ongoing native generation cancellation to generator released | 3.05 / 6.07 / 6.07 / 8.34 |
| Ongoing generation cancellation to next voice ready after reload | 1621.99 / 1861.61 / 1861.61 / 1880.60 |

Final-chunk cancellation reused the worker in 20/20 cases; ongoing synthesis intentionally terminated it in 20/20. The existing 50 ms completion-ack drain remains unchanged. Driver abort return is not acoustic silence detection. The device probe includes first-use output initialization; its largest readiness outlier was not removed. [Interruption results](artifacts/performance/phase4_interruption.json).

In the longer conversational candidate, empty output-frame gaps between native chunks were about 10.46 ms median on MME versus 0.46 ms on WASAPI, with no underflows. That candidate also changed recognition thread count, so this is supporting evidence rather than an isolated gap comparison. No sample trimming, word removal, sentence fragmentation or new audio post-processing was introduced. Listening for clicks and naturalness remains a separate manual check.

## Files, settings and tradeoffs

Phase 4 changes relative to the saved Phase 3 source in `artifacts/performance/phase4_baseline`:

| File | Purpose |
|---|---|
| `voice/playback.py` | Unique same-device WASAPI preference, original-driver fallback, first-callback timestamp |
| `voice/voice.py` | Capture completion and eager/lazy recognition spans; optional bounded CPU thread override, default behavior retained |
| `core/latency.py` | Separate PCM/queue/callback/DAC/capture/explicit-loopback intervals |
| `config.py` | `CAPRI_TTS_WASAPI=1`; `CAPRI_WHISPER_THREADS=0` |
| `benchmark_voice.py` | Paired AB/BA comparisons, full warm-up turns, opt-in passive loopback, paired bootstrap summaries |
| `benchmark_playback.py` (new) | Real fixed-PCM driver comparison and benchmark-only reuse probe |
| `benchmark_recognition.py` (new) | Saved corpus, reference errors, VAD thresholds, thread/filter/wait-policy experiments |
| `benchmarks/__init__.py`, `benchmarks/wasapi_loopback.py` (new) | Explicit native Windows capture helper; never loaded by normal operation |
| `tests/test_voice_phase4.py` (new) | Stage accuracy, reuse, bounded options, fallback/no-replay, loopback parsing/opt-in, bootstrap accuracy |
| `tests/test_voice_phase2.py` | Extend the existing replay regression to cover excluded warm-up turns |
| `tests/conftest.py` | Drain deferred Qt deletion and collect closed widgets on the GUI thread between tests |
| `PHASE4_VOICE_PERFORMANCE_REPORT.md`, `PERFORMANCE_REPORT.md` | Measurements and current-report index |

No new dependencies. In normal operation there is no loopback capture, additional League request, polling worker, persistent audio stream, model instance or GPU allocation. Recognition CPU defaults were retained. Driver callback counts fell in the matched output cohort; system-wide audio-engine CPU and actual game FPS were not measured, so no universal CPU/FPS guarantee is claimed. Existing working-tree changes from earlier phases were preserved.

## Regression results and quality checks

Final complete suite: **675 passed in 90.52 seconds**, `.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider`. Before the last two driver-cancellation cases were added, the complete 673-test suite also passed in 89.33 seconds. Final targeted Phase 4 cases: 19 passed. `git diff --check` passed; Git only printed LF/CRLF conversion notices. An offscreen main-entrypoint check constructed the real window, processed events, closed it and returned 0; preloads, real screen capture and League networking were disabled for that check. Real engine loading/output were exercised separately by the benchmarks.

The first full run exposed constructor-compatibility assertions: an explicitly passed default `cpu_threads=0` was unnecessary. The default/CUDA paths now retain the original constructor arguments, and thread options are passed only for explicit CPU overrides. An optional GUI benchmark initially failed in new warm-up bookkeeping; it was corrected and covered by 0/1-warm-up regressions. No failed-run measurements were included in the tables.

Two later full runs aborted natively around streaming cancellation, while the isolated 17-test streaming group and the 89-test leading sequence passed. The manual Qt test harness did not drain deferred deletions; teardown now drains those events and collects closed widget cycles on the GUI thread rather than leaving collection to a later transport/timer thread. The two subsequent complete runs above passed. The Python abort trace did not identify the precise C++ failure site, so this is not presented as a fully resolved native forensics diagnosis. No speculative production GUI rewrite or application GC policy was introduced.

The suite covers streaming order, queued/stale speech suppression, microphone races, cancellation, bounded queues and worker shutdown, recognition model reuse/filter preservation, unique/ambiguous device matching, original-driver fallback, and no replay after partially consumed audio. Added cancellation checks prevent a failed preferred driver from reopening obsolete output after interruption. League activation/reconnection/exit, dock geometry, movement locking, cached SVG transitions and normal-mode restoration remain covered without an active League client.

Subjective validation checklist, deliberately separate from automated evidence:

- [x] Existing voice/speaker/style assets, pronunciation preprocessing and spoken-text pipeline retained.
- [x] Identical saved PCM used for the isolated driver comparison; no underflows reported in measured output cohorts.
- [x] Reference recognition comparisons, text/audio ordering and interruption regressions completed.
- [ ] Human A/B listening for voice identity, champion names, emotional rhythm and missing/repeated words.
- [ ] Human listening for clicks and chunk transitions during long replies, interruptions and device changes.
- [ ] Calibrated acoustic/electrical timing and live match/FPS/vision ON/OFF validation.

Unchecked items are not claimed as completed merely because counters or ASR looked good.

## Reproduction and hardware validation

PowerShell, `F:\Neko`, existing environment/models:

```powershell
# Current defaults. Restart Capri after changing these.
$env:CAPRI_TTS_WASAPI = '1'
$env:CAPRI_WHISPER_THREADS = '0'
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider

# Repeat the final paired real-engine/output experiment on the same saved PCM.
# --audio explicitly plays replies; --loopback passively inspects the render mix.
.\.venv\Scripts\python.exe -B benchmark_voice.py --samples 20 --replay --audio --loopback --compare-output-backend --profile --recording artifacts/performance/phase4_greeting.wav --question "Say hello briefly." --output artifacts/performance/phase4-local-paired.json

# List devices; use the WASAPI index for the SAME physical endpoint, not blindly 22.
.\.venv\Scripts\python.exe -m sounddevice
.\.venv\Scripts\python.exe -B benchmark_playback.py --samples 20 --audio --loopback --wasapi-device 22 --recording artifacts/performance/phase3-quality-final/8/short.wav --output artifacts/performance/phase4-local-playback.json

# Recognition and endpoint experiments; no microphone or playback.
.\.venv\Scripts\python.exe -B benchmark_recognition.py --samples 20 --threads 2 4 8 --recognition --compare-filter --vad --output artifacts/performance/phase4-local-recognition.json
.\.venv\Scripts\python.exe -B benchmark_recognition.py --samples 20 --threads 4 6 8 --fixtures short long --recognition --passive-wait --output artifacts/performance/phase4-local-passive.json

# Existing cancellation path, including actual driver abort. No microphone.
.\.venv\Scripts\python.exe -B benchmark_voice.py --samples 20 --interrupt --audio --profile --output artifacts/performance/phase4-local-interrupt.json

# Mock League API with real offscreen GUI, speech engines and output.
.\.venv\Scripts\python.exe -B benchmark_voice.py --samples 20 --warmup-turns 1 --replay --league-fixture --desktop-ui --audio --loopback --profile --output artifacts/performance/phase4-local-desktop.json
.\.venv\Scripts\python.exe -B benchmark_voice.py --samples 20 --warmup-turns 1 --replay --league-fixture --companion --audio --loopback --profile --output artifacts/performance/phase4-local-companion.json

# Optional foreground/background model stress, generated image rather than screen capture.
.\.venv\Scripts\python.exe -B benchmark_voice.py --samples 20 --contention --model auto --output artifacts/performance/phase4-local-contention.json

# Production logging: collect >=20 real turns per desktop/champ-select/match scenario.
$env:CAPRI_LATENCY_METRICS = '1'
$env:CAPRI_VISION = '1'
.\.venv\Scripts\python.exe -B main.py *> artifacts/performance/phase4-live-vision-on.log
# Repeat with CAPRI_VISION=0 and comparable questions/game conditions.
# Microphone starts muted as before; enable it deliberately when testing voice.
```

Keep other output quiet for loopback onset tests. `--loopback-endpoint` can select an explicit Windows render endpoint ID if the default route is inappropriate. An ambiguous/no-onset run must be diagnosed, not reported as zero latency. Run performance jobs serially, away from pytest/model downloads. Add `--model qwen2.5:32b` to explicitly benchmark that conversational model; cold loading and GPU contention can dominate on this card.

For human validation, create a manifest with the following schema and supply 16-bit WAV recordings. Paths are relative to the manifest; endpoint annotations refer to 16 kHz samples after mono conversion. Include short/long questions, natural hesitations, quiet/noisy inputs, and separate nonspeech controls. Annotate the last intended spoken word, not just the first silence.

```json
{"kind":"annotated human recordings","fixtures":[
  {"name":"natural_pause","path":"natural_pause.wav","reference":"Could you explain gravity to me","speech_end_sample":48000},
  {"name":"room_noise","path":"room_noise.wav","reference":"","speech_end_sample":null}
]}
```

Run `benchmark_recognition.py --samples 20 --manifest path\manifest.json --threads 4 --recognition --vad --output artifacts/performance/phase4-human.json`. Reference transcripts remain local; reports contain word-error counts rather than recognized speech.

### A/B listening and true acoustic validation

Use exactly the same saved medium/long/emotional/champion-name PCM with `benchmark_playback.py --samples 1 --audio --wasapi-device <same-endpoint-index> --recording <fixture.wav> --output <report.json>`. It plays each driver once for warm-up and once for measurement, without changing voice text or volume. Listen for identity, names, emotional rhythm, missing/repeated words, clicks, transitions and recovery after interruption. No subjective listening score is claimed here. No-underflow counters alone do not prove absence of clicks or missing consonants.

For actual speech-end-to-audible timing, record the user's utterance and Capri's speaker with an external recorder on one continuous clock. Use a calibrated near-speaker microphone/second channel when separation is needed. Disable recorder monitoring, noise gates and automatic gain that could shift onset; annotate both endpoints consistently. Account for microphone/speaker distances and channel skew. Constant recorder ADC delay cancels between markers on the same clock; this does not calibrate a host-QPC-to-acoustic one-way measurement.

An appropriate audio interface can instead capture an electrical line-output reference and acoustic microphone simultaneously. Calibrate channel/ADC delay and any host-time reference trigger separately. Report an electrical/round-trip measurement as such; do not assume half a round trip equals one-way acoustic output. WASAPI loopback observes the digital mix and cannot establish when sound reaches the listener. No calibrated acoustic/electrical measurement was performed in this session.

Rollback of the new output preference is `$env:CAPRI_TTS_WASAPI='0'` before restarting. Leave `CAPRI_WHISPER_THREADS='0'`, Kokoro settings, silence threshold and six-second inference grace at their defaults unless local measurements justify changing them.

## Next measured bottleneck

For the short greeting, endpointing (~564 ms) and recognition (~434 ms) now exceed synthesis (~358 ms); text accumulation before natural speech adds ~249 ms. The next useful work is a representative human pause/hesitation corpus and complete-application CPU scheduling experiments, not another unvalidated silence-threshold reduction. The longer gravity reply still spent about 1034 ms in first-chunk synthesis. GPU model swapping and explicit 32B reasoning can add further delay; measure those with the existing foreground-priority policy intact. The roughly 14 ms WAV transport is still a lower-priority target than these stages.
