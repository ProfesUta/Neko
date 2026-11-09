# Third-party components

## Capri's Kokoro voice

The `capri-voice-1` model pack is provided by Lachy and the
[Capri project](https://github.com/lachydotmcg/capri-desktop).
The active preset is "Her own": speaker 53, speed 1.1, American English.
It is the voice used for `artifacts/voice-preview/capri-own.wav`.

The installer preserves the project's license and required notices in
`assets/voices/capri-kokoro/LICENSE-CAPRI.md` and adds `ATTRIBUTION.md` beside it.
The creator's project license is
[PolyForm Noncommercial 1.0.0](https://github.com/lachydotmcg/capri-desktop/blob/main/LICENSE.md).
Downloaded model assets are excluded from the repository's tracked files.

Speech runs through the official
[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) Python package (Apache-2.0).
The pack's bundled native DLLs are not extracted or executed. Third-party model
components retain their upstream licenses.
