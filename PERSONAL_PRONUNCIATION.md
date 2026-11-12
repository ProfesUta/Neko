# Private Kokoro name pronunciation

## Confirmed cause

Direct synthesis with the installed sherpa-onnx 2.0.1 reproduced the problem without an LLM, conversation history, audio playback or conversation streaming. The displayed name survived `clean_for_speech()` and `prepare_kokoro_text()` unchanged and reached the worker correctly. Native diagnostics reported `OOV word skipped` because the name was absent from the voice/champion lexicons.

Name-only output was approximately **0.190 s**; adding the missing name to an otherwise valid sentence barely changed its duration. Nonzero RMS did not prove the name was spoken: punctuation can still produce a short sound. The configured test profile produced approximately **0.791 s** of name audio, in both full and native-streamed synthesis, with **zero retained OOV diagnostics** across the verification fixtures. This establishes that the name is no longer discarded; subjective pronunciation still requires listening.

The installed Kokoro frontend is lexicon-based. Its old `data_dir`/eSpeak settings are ignored in this runtime; arbitrary phonetic spelling is not a working G2P fallback. References: [runtime configuration](https://raw.githubusercontent.com/k2-fsa/sherpa-onnx/v2.0.1/sherpa-onnx/csrc/offline-tts-kokoro-model-config.h), [native lexicon lookup](https://raw.githubusercontent.com/k2-fsa/sherpa-onnx/v2.0.1/sherpa-onnx/csrc/offline-tts-kokoro-impl.h).

## Configuration

The active profile is local: `data/personal-pronunciations.json`. Its contents and derivative/editor files are Git-ignored. `CAPRI_PERSONAL_PRONUNCIATIONS` can select another private path; use `none` to disable the feature. Keep custom paths outside the checkout or explicitly ignore them.

Example format only—use your own displayed spelling and listening-verified pronunciation:

```json
{
  "entries": [
    {
      "display_name": "Robin",
      "spoken_pronunciation": "robin",
      "phonemes": null
    }
  ]
}
```

- `display_name` selects the existing name in spoken text. It does not rename conversation memory, the user, or GUI text.
- `spoken_pronunciation` is a preferred spelling/hint. Without an override, each word/hyphen-separated part must exist in the installed lexicons; their actual phonemes are composed. Unknown syllables are rejected, not silently skipped.
- `phonemes`, when supplied, takes precedence. It accepts an array or whitespace-separated string of the **individual symbol strings in this installed voice's `tokens.txt`**, including its stress symbols. It is not an arbitrary IPA word, slash-delimited transcription, ARPABET string, or list of token IDs. An illustrative model-token sequence is `["ɹ", "ˈ", "ɑ", "b", "ɪ", "n"]`; token validity alone does not establish a preferred pronunciation.
- `"enabled": false` retains a draft without applying it. A blank/invalid active entry raises a generic configuration error without printing private values.

The current user's requested syllables are stored only in the ignored local profile, with an explicit validated model-token override. They are not added to public configuration, source dictionaries, application logs or this document.

Matching is confined to complete names, case-insensitively, with punctuation/quotes, possessives and multiword names supported. It does not replace substrings in longer words, handles or unrelated names. Use a distinctive name/full name when ordinary words are ambiguous; this is a pronunciation profile, not an entity-resolution model. Conflicts with champion dictionary names are rejected to preserve champion announcements.

## Direct audible verification

PowerShell in the repository, with the existing voice pack:

```powershell
# Uses the local profile. Baseline intentionally demonstrates the old failure,
# followed by the profile, at the same speaker/speed, without LLM or microphone.
.\.venv\Scripts\python.exe -B -m voice.verify_pronunciation --audio

# Prompt locally, listen, and save only if you explicitly answer y.
# Other existing profile entries, including disabled drafts, are retained.
.\.venv\Scripts\python.exe -B -m voice.verify_pronunciation --configure

# Inspect accepted model symbols; does not start an engine or play audio.
.\.venv\Scripts\python.exe -B -m voice.verify_pronunciation --tokens

# Silent generation plus explicit private stage trace and paired latency check.
.\.venv\Scripts\python.exe -B -m voice.verify_pronunciation --trace --latency-samples 20

# Hardware-independent regressions; installed-engine tests synthesize, never play.
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider
```

Fixtures exercise the name alone, sentence beginning/middle/end, quotes, possessive and multiple sentences, using full generation and native callbacks. Audio and numeric summaries are saved under ignored `artifacts/pronunciation/verification`. `--trace` additionally writes displayed, cleaned, prepared and worker-transformed native text to `private-trace.json` there. It never prints that data into normal logs. Saved streaming WAVs concatenate the actual delivered chunks in order at their reported rate.

Listen specifically for the initial vowel, stressed syllable, final consonants, surrounding words and natural rhythm. Confirm both full and streamed samples; a longer waveform or ASR spelling alone is not proof of correct pronunciation. Restart Capri once after this code update. Later profile edits/removal automatically reload the existing resident worker on its next request; there is no per-sentence model initialization or JSON parsing.

## Implementation and preserved behavior

`voice/personal_pronunciation.py` compiles the optional profile once when the worker loads. It validates against the actual token inventory and maps complete names to internal alphabetic lexicon keys. A small adapter changes only the text handed to the same native `generate()` method. Parent cleaning/preparation, chat text and `PreparedSpeech.text` remain intact. All original speaker, speed, style, callback and PCM behavior is retained.

The extra UTF-8 lexicon is loaded **after** the unchanged base/champion files, with separate internal keys. The native constructor copies it into memory, so its temporary file is deleted immediately. Original `model.onnx`, `voices.bin`, `tokens.txt`, lexicons and champion dictionary are never rewritten. The parent includes the private source file in the existing worker signature so editing/removing it causes one coherent reload. An in-flight worker keeps its own consistent profile.

No additional TTS backend, phonemizer, normal-operation worker, network request or dependency was introduced. Native streaming, bounded queues, final-chunk cancellation, startup warm-up, Phase 2–4 timing/CPU/output settings and League behavior remain in place.

Changed/created files for this task:

| File | Change |
|---|---|
| `voice/personal_pronunciation.py` | Private schema, validation, exact-name adapter and lexicon compilation |
| `voice/kokoro_worker.py` | Optional profile startup flag; append ephemeral lexicon to existing native constructor |
| `voice/kokoro_session.py` | Track private configuration in the existing persistent-worker signature |
| `config.py` | Optional private profile path |
| `.gitignore` | Keep default private profile, temporary and editor variants out of commits |
| `voice/verify_pronunciation.py` | Direct full/streamed A/B audio, private tracing, listening configuration and warm timing |
| `tests/test_personal_pronunciation.py` | Pronunciation, privacy, boundaries, callbacks, configuration and worker-reload regressions |
| `tests/conftest.py` | Isolate tests from real personal profiles |
| `PERSONAL_PRONUNCIATION.md` | Cause, setup, verification and measured limits |

## Latency observations

Twenty paired warm samples per condition, alternating AB/BA, on identical **name-free** text, same installed engine/voice/eight-thread CPU settings. Temporary baseline/profile worker instances exist only inside this explicit diagnostic; normal Capri still has one voice worker.

| Text-to-audio-ready, ms | p50 | p95 | p99 | Maximum | n |
|---|---:|---:|---:|---:|---:|
| Baseline | 687.45 | 741.47 | 741.47 | 808.40 | 20 |
| Profile enabled | 695.28 | 747.38 | 747.38 | 791.14 | 20 |

The observed median difference is +7.84 ms (1.14%), with overlapping timing variation; this does not prove zero overhead or establish a causal 7.84 ms parsing cost. Profile JSON/regex/lexicon compilation is outside warm generation. The first sequential startup observations were single samples affected by cache/load conditions, so no cold-start speedup is claimed. Speaking the restored name also requires real additional phonemes/audio compared with incorrectly skipping it; that additional synthesis is expected.

Private diagnostics: `artifacts/pronunciation/before`, `after`, `selected`, and `paired_latency.json`. These contain personal audio/text where explicitly requested and are excluded from Git. No subjective listening or acoustic latency guarantee is claimed by the automated tests.

## Validation results

- Targeted pronunciation tests: **21 passed**, including an installed-engine test using the resident worker with full synthesis and native callbacks. No microphone or playback device is needed by these tests.
- Complete regression suite: **696 passed in 131.69 s** using the pytest command above. The captured result is in ignored `artifacts/pronunciation/pytest.txt`.
- Direct verification: **28 generated cases**, covering seven sentence fixtures in baseline/profile and full/streamed modes. The profile's 14 cases produced zero retained native OOV messages. The explicit `--audio` run also completed through the existing playback path.
- Complete-name matching, split LLM token delivery, unchanged displayed text, champion protection, malformed/private configuration, listening confirmation, warm-worker reuse and profile reload/removal are covered.

The playback run verifies delivery, not a listener's preferred pronunciation or physical speaker timing. The user should confirm the saved profile audio by listening and adjust the private profile if necessary.
