# Kokoro word-loss investigation

## Confirmed cause and scope

The installed sherpa-onnx 2.0.1 Kokoro frontend silently skips words absent from its lexicons. The original voice-pack dictionary has no `what's` or `wanted` entry. Direct full synthesis emitted `OOV word skipped: 'What's'` and `OOV word skipped: 'wanted'`, independently of conversation streaming, prefetch and playback. Offline Whisper recognition of the resulting WAVs heard “on your mind” and “I to ask you something.” These observations were repeated through native callbacks and the conversational TTS route.

The installed runtime ignores the old eSpeak settings; having `espeak-ng-data` in the pack does not provide a G2P fallback. See the [installed-version native implementation](https://raw.githubusercontent.com/k2-fsa/sherpa-onnx/v2.0.1/sherpa-onnx/csrc/offline-tts-kokoro-impl.h). The diagnosis comes from actual local synthesis, not just that source or text inspection.

The fix adds **two model-token lexicon entries**, loaded between the unchanged base and champion dictionaries. It does not expand contractions, prepend text, trim silence, change segmentation, delay output, or replace the model/voice. `I'm`, `You're`, `Don't`, `Can't`, and `It's` already synthesize without OOV warnings and retain their existing pronunciations. Native callbacks, worker persistence and cancellation are unchanged. The supplement participates in the existing worker signature, so an edit causes one coherent reload.

The earlier raw personal-name failure also involved an absent dictionary entry, but was tested separately. The active, ignored personal profile now supplies the user's previously specified pronunciation. Its display spelling and phoneme configuration are unchanged by this patch. The brief-name failure could **not** be reproduced with that active profile. Twelve separate real digital name captures covered full/native/conversational paths, initial/consecutive use, and sentence-initial/middle placement. They had zero OOV warnings and exact device callback PCM; local recognition recovered the displayed name in generated and Windows-captured speech. Across those 12 captures, minimum source/mix correlation was 0.999700 overall and 0.999959 over the first 300 ms of source signal. This is evidence against a dropped name buffer; whether the sound matches the user's preference still requires listening. A newly reported name problem is not assumed to have the same cause as the old unconfigured-name problem.

## Pipeline and audio evidence

The exact ten requested sentences were exercised, plus six isolated contraction probes. Personal fixture names are read from the local profile rather than committed in source. Each route was exercised initially and consecutively:

1. Direct full-utterance Kokoro synthesis.
2. Native Kokoro callback synthesis, with delivered chunks concatenated in order.
3. The application's `StreamingReply` → `tts.stream_speech()` → prepared chunk → playback path, with simulated model deltas split every three characters. This uses the existing machinery, without an LLM, microphone or GUI scheduling.
4. Real output through the configured Windows endpoint, using existing shared-mode WASAPI. An explicit diagnostic tap saved the actual callback buffers and checked their source sample ranges, first through last. This tap exists only in the diagnostic command, not normal playback.

Original response, displayed text, sentence segments, cleaned text, prepared text and worker-adapter input are available only in explicitly requested private traces. All words survived the text pipeline. Personal names alone are converted into private lexicon keys immediately before native synthesis. The raw complete utterance already lacked the affected words in its generated audio before the fix.

After the fix, all **216 synthesis replays** had zero OOV warnings. For both affected sentences, local recognition recovered the complete sentence in full, native-streamed and conversational output, in both initial and consecutive runs. Recognition is supporting evidence, not definitive listening proof: the recognizer still rendered an isolated “What's?” as “What?”, demonstrating why an ASR result alone is insufficient.

The initial **96 actual-output replays** also had zero OOV warnings and exact PCM source-to-callback matches, with no skipped, repeated, overwritten or truncated source ranges. Generated WAV recognition recovered both complete affected sentences in all three routes. A further **46 actual-output conversational replays** included 20 warm samples per affected phrase and two long replies, each delivered in four chunks. All 142 output cases preserved their source PCM. Full and streamed synthesis are compared as utterances, not assumed bit-identical: the installed engine can vary sample counts slightly between generations.

The output route resolved to **Speakers (High Definition Audio Device), Windows WASAPI**, with shared-mode conversion enabled. Matching callback PCM establishes delivery to the driver, not what a speaker physically emitted. No production audio recording, permanent output stream, extra voice worker or background diagnostic service was introduced.

## Warm readiness measurements

Twenty samples per phrase/route/condition, same machine, installed engine, speaker 53, speed 1.1, eight CPU synthesis threads and existing CPU tuning. Each cohort used identical fixture text, excluded model loading from warm samples, and ran serially. No League, Ollama or vision load was added. These are **real-engine controlled replays**, not full microphone-to-speaker measurements. Explicit artifact copies/tracing add diagnostic work; they are not application-wide telemetry.

| Phrase / route | Before p50 / p95 / p99 / max, ms | After p50 / p95 / p99 / max, ms | n per condition |
|---|---:|---:|---:|
| What's on your mind? — full ready | 448.08 / 488.27 / 488.27 / 503.47 | 479.94 / 509.85 / 509.85 / 512.32 | 20 |
| What's on your mind? — first native chunk | 440.13 / 466.32 / 466.32 / 525.67 | 488.41 / 553.97 / 553.97 / 585.92 | 20 |
| What's on your mind? — conversational first chunk | 446.61 / 473.37 / 473.37 / 479.80 | 489.02 / 523.11 / 523.11 / 529.94 | 20 |
| I wanted to ask you something. — full ready | 606.12 / 642.97 / 642.97 / 700.17 | 635.08 / 714.87 / 714.87 / 733.80 | 20 |
| I wanted to ask you something. — first native chunk | 587.70 / 725.47 / 725.47 / 797.43 | 651.83 / 734.84 / 734.84 / 742.50 | 20 |
| I wanted to ask you something. — conversational first chunk | 607.18 / 645.26 / 645.26 / 667.40 | 656.61 / 720.12 / 720.12 / 738.60 | 20 |

These are correctness comparisons, **not latency improvements**: the baseline synthesized an incomplete utterance. The restored words require additional inference and audio. Conversational readiness medians increased by 42.41/49.43 ms, respectively. No artificial startup padding was added. Raw summaries include variance, frame counts, chunk counts, cold first-use observations and completion timing. Single cold observations are not presented as latency distributions.

Separate **after-fix real-output cohort**, 20 warm samples per phrase through the conversational route:

| Text available to stage | What's on your mind? p50 / p95 / p99 / max, ms | I wanted to ask you something. p50 / p95 / p99 / max, ms |
|---|---:|---:|
| First audio ready | 471.02 / 517.30 / 517.30 / 534.87 | 612.08 / 636.38 / 636.38 / 643.73 |
| Estimated first non-silent DAC presentation | 692.58 / 742.66 / 742.66 / 762.75 | 834.65 / 864.57 / 864.57 / 873.54 |
| Reply complete after software stream drain | 1737.21 / 1806.87 / 1806.87 / 1807.08 | 2406.13 / 2427.27 / 2427.27 / 2436.63 |

This separate output cohort is not a paired speedup comparison against the synthesis-only cohort. Callback taps, diagnostic file copies and tracing were enabled. A DAC estimate is not measured digital loopback or physical speaker onset; software completion is not an acoustic end timestamp. No missing-chunk or source-range mismatch was observed in these 40 warm output replies.

## Paired digital-output verification

Initial loopback attempts were rejected by the existing quiet-output guard. After the user paused other output, a numeric-only calibration still measured a low noise floor: recent packet RMS approximately 0.000478 full scale, peak up to 0.002262. The default 0.001 signal detector was triggering on that floor. The explicit diagnostic used **0.005** for both conditions; neither playback volume nor PCM was changed. The existing probe's default remains 0.001, so previous Phase 4 measurements are not silently redefined. This threshold defines digital signal onset above the measured floor, not the earliest physical audible sound.

Twenty **paired AB/BA** warm samples per affected phrase/condition used the exact saved pre-fix worker and corrected worker, same voice, device and threshold. Two existing `KokoroSession` instances were kept warm only inside the explicit diagnostic and shut down afterward. Normal Capri still owns one worker. Trial zero included startup and was excluded. All 80 warm captures, plus four startup observations, passed exact callback PCM-range checks and the bounded capture's discontinuity/timestamp checks. The probe recorded one initial discontinuity outside accepted armed captures and zero invalid timestamps.

| Text available to stage | Before p50 / p95 / p99 / max, ms | After p50 / p95 / p99 / max, ms | n per condition |
|---|---:|---:|---:|
| What's on your mind? — first audio ready | 445.27 / 466.89 / 466.89 / 471.33 | 478.51 / 514.35 / 514.35 / 537.13 | 20 |
| What's on your mind? — DIGITAL loopback onset | 541.89 / 564.74 / 564.74 / 569.72 | 573.95 / 609.74 / 609.74 / 629.18 | 20 |
| What's on your mind? — software completion | 1317.08 / 1339.12 / 1339.12 / 1344.87 | 1502.18 / 1546.84 / 1546.84 / 1561.68 | 20 |
| I wanted to ask you something. — first audio ready | 581.60 / 621.04 / 621.04 / 659.99 | 625.66 / 647.79 / 647.79 / 667.34 | 20 |
| I wanted to ask you something. — DIGITAL loopback onset | 682.87 / 716.71 / 716.71 / 758.21 | 725.52 / 747.53 / 747.53 / 773.49 | 20 |
| I wanted to ask you something. — software completion | 1989.24 / 2019.42 / 2019.42 / 2061.64 | 2172.82 / 2184.36 / 2184.36 / 2211.72 | 20 |

The before audio is incomplete, so faster baseline timing is not a desirable performance result. In this paired cohort, restored-word first-audio medians increased by 33.24/44.06 ms; digital onset by 32.06/42.66 ms. Full completion also includes the restored word's duration. No STT/LLM time is included in these text-to-output intervals.

Offline recognition of generated **and digital-captured** audio at trials 0, 1 and 20 consistently recovered “on your mind” / “I to ask you something” before, and both complete requested sentences afterward. As an independent sample-delivery check, captures were aligned against their generated PCM after converting the 48 kHz stereo mix to 24 kHz mono. Across all 20 corrected captures per phrase, minimum normalized correlations were:

| Corrected phrase | Whole utterance | First 300 ms of source signal | Last 300 ms of source signal |
|---|---:|---:|---:|
| What's on your mind? | 0.998970 | 0.997912 | 0.999644 |
| I wanted to ask you something. | 0.999534 | 0.999963 | 0.999118 |

These waveform comparisons support retention of the opening/final audio through Windows, rather than merely counting non-silent frames. They do not prove a listener's preferred pronunciation or physical speaker timing. ASR is corroborating evidence, not the sole correctness test. No calibrated acoustic/electrical measurement was performed.

## Files for this fix

| File | Purpose |
|---|---|
| `voice/conversation-pronunciations.lexicon` | The two confirmed missing ordinary-word pronunciations |
| `voice/kokoro_worker.py` | Load the supplement into the same native engine |
| `voice/kokoro_session.py` | Include its signature in the existing reload mechanism |
| `voice/verify_speech.py` | Explicit real-engine/stream/device replay, private traces, ordered PCM files and optional ASR/loopback |
| `benchmarks/wasapi_loopback.py` | Optional diagnostic packet sink and calibrated signal threshold; existing defaults retained |
| `tests/test_speech_integrity.py` | Text/PCM boundaries, prefetch, cancellation, consecutive output, supplement reload and actual-engine regressions |
| `tests/test_kokoro_session.py` | Updated expected dictionary chain |
| `VOICE_WORD_LOSS_REPORT.md` | Evidence, measurements and reproduction instructions |

No new dependency or production configuration setting. The private pronunciation profile remains supported. `core/streaming.py`, `voice/tts.py`, `voice/pronunciation.py` and `voice/playback.py` were verified byte-identical to this task's starting snapshot. All unpushed Phase 2–4 work is retained. GUI, League, SVGs, docking, movement, inference scheduling and voice assets were not modified.

## Regression results

Final complete suite: **714 passed in 135.20 seconds**, using the pytest command below. Earlier complete checkpoints passed 710 and 713 tests before the final diagnostic-validation cases were added. Targeted speech-integrity tests passed before the threshold-calibration test was added; that final case is included in the 714-test run. Full output is in ignored `artifacts/speech-integrity/pytest-verified.txt`.

Coverage includes complete first/final PCM ranges, contractions, native chunk totals/order, no duplicate whole-WAV fallback, prefetch/consecutive responses, cancellation and queued cleanup, supplement reload, actual installed-engine output, personal-profile protection, bounded/invalid diagnostic captures, and explicit noise-threshold validation. Existing League notifications, priority, SVG/docking, worker cleanup and interruption regressions passed as part of the full suite. No live-match or microphone test is claimed. `git diff --check` passed with only existing LF/CRLF conversion notices.

- [x] Actual synthesis and rendered digital audio checked, beyond text preservation.
- [x] All ten requested sentences exercised through full/native/conversational generation and actual device callbacks.
- [x] Twenty warm samples per affected short case; paired before/after digital capture completed.
- [x] Source-to-device PCM ranges and source-to-digital opening/final waveforms checked.
- [x] Existing approved personal pronunciation retained and independently captured through all three routes.
- [ ] Listener confirmation of preferred name pronunciation, voice identity, rhythm, clicks and interruption naturalness.
- [ ] Calibrated physical acoustic/electrical output timing.

## Listening and local validation

Restart Capri once to load the code update. PowerShell in `F:\Neko`:

```powershell
# Quick direct/native/conversational check: sentence, personal name, and wanted.
# Plays controlled fixtures only; no microphone or LLM is used.
.\.venv\Scripts\python.exe -B -m voice.verify_speech --samples 0 --fixtures 0 2 3 6 --audio --trace --output artifacts/speech-integrity/local-listening

# Longer multi-chunk reply; check the first and final words and every transition.
.\.venv\Scripts\python.exe -B -m voice.verify_speech --samples 0 --fixtures 16 --audio --trace --output artifacts/speech-integrity/local-long

# Twenty warm samples per affected phrase, including actual output.
.\.venv\Scripts\python.exe -B -m voice.verify_speech --modes conversation --fixtures 0 6 --samples 20 --audio --trace --output artifacts/speech-integrity/local-warm

# Optional passive DIGITAL output capture, only when other output is quiet.
# Explicitly saves bounded loopback WAVs. It never enables microphone input.
.\.venv\Scripts\python.exe -B -m voice.verify_speech --modes conversation --fixtures 0 6 --samples 20 --audio --loopback --loopback-threshold 0.005 --trace --recognize --output artifacts/speech-integrity/local-loopback

# Full regression suite; no active League match, Ollama, microphone or speakers needed.
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider
```

`--recognize` uses the already-installed Whisper model in local-only mode; it does not download a recognizer. Omit it if unavailable. `--loopback-endpoint` selects an explicit Windows render endpoint. The 0.005 threshold above was calibrated for this endpoint; use an appropriately measured threshold locally, not a value that hides other audible output. Loopback captures the digital render mix, not physical acoustic output. Keep other output quiet throughout capture; do not use a mixed/noisy sample as evidence of Capri's onset or pronunciation. Use a fresh output folder for each run. Incomplete, over-bound or timestamp/discontinuity-affected captures are rejected.

Each replay folder contains `generated.wav`, ordered `chunk-NN.wav`, and, when `--audio` is enabled, `submitted.wav`. `--loopback` additionally saves `loopback.wav`. Submitted WAVs include callback padding between chunks; they are driver-submission diagnostics. `--trace` writes private text/native diagnostics and optional ASR observations. All default artifacts and the personal profile are Git-ignored.

Existing saved comparisons:

- `artifacts/speech-integrity/before-synthesis/full-1-00/generated.wav`: original missing “What's”.
- `artifacts/speech-integrity/after-output/full-1-00/generated.wav`: corrected full synthesis.
- `artifacts/speech-integrity/after-output/native-1-00/generated.wav`: corrected native stream.
- `artifacts/speech-integrity/after-output/conversation-1-00/generated.wav`: corrected conversational stream.
- The same folders' `submitted.wav`: corresponding PCM delivered to the real output callback.
- `artifacts/speech-integrity/after-output/conversation-1-02/generated.wav` and `conversation-1-03/generated.wav`: personal-name fixtures using the approved private pronunciation.
- `artifacts/speech-integrity/warm-output/conversation-1-16/generated.wav`: four-chunk conversational reply, with `submitted.wav` alongside it.
- `artifacts/speech-integrity/paired-loopback/after-00-01/loopback.wav`: corrected question recorded from the digital Windows render mix; compare `before-00-01/loopback.wav`.
- `artifacts/speech-integrity/paired-loopback/after-06-01/loopback.wav`: corrected `wanted` sentence recorded from Windows; compare `before-06-01/loopback.wav`.
- `artifacts/speech-integrity/name-loopback/conversation-1-02/loopback.wav`: configured personal name and corrected question recorded from Windows. Fixture `03` checks the name inside a sentence.

Raw summaries reside in `before-synthesis`, `after-synthesis`, `after-output`, `warm-output`, `paired-loopback`, and `name-loopback` under ignored `artifacts/speech-integrity/`. The initial `before-run.log` records the rejected noisy-loopback attempt; it is excluded from all timing tables. Source snapshots taken before the fix are in `artifacts/speech-integrity/source-before`. The local paired runner is `artifacts/speech-integrity/measure_loopback.py`, and `compare_digital.py` repeats the waveform comparison; run them serially with the environment's Python interpreter and other output quiet. Both are explicit local diagnostics, not background application services.

Listen for the complete initial contraction, `wanted`, final words, the configured personal name/stress, missing/duplicated syllables, voice identity, rhythm, clicks, and chunk gaps. Repeat after an interruption and in consecutive replies. No human listening score or calibrated acoustic timing is claimed by automated tests. A listener's confirmation of the personal pronunciation remains necessary.

The supplement fixes the demonstrated lexical omissions; the runtime still has no general G2P fallback for arbitrary new words. This patch does not claim universal unknown-word coverage, physical speaker verification, live-game/FPS measurements or new GPU benchmarks.
