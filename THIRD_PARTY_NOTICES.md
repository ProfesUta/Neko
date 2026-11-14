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

## League of Legends

Capri is not endorsed by Riot Games and does not reflect the views or opinions
of Riot Games or anyone officially involved in producing or managing Riot Games
properties. Riot Games and all associated properties are trademarks or registered
trademarks of Riot Games, Inc.

Champion names, ability references, tips and item descriptions use cached
[Riot Data Dragon](https://developer.riotgames.com/docs/lol#data-dragon)
data. The read-only League integrations use the local Live Client Data API and
the unsupported League Client API. See [Riot's League documentation and policies](https://developer.riotgames.com/docs/lol).
No player input, process memory access, hidden identity lookup, or gameplay automation is used.

## Optional recorded-video analysis

Recorded-game analysis uses the CPU-only `opencv-python-headless` package,
provided by the [OpenCV project](https://pypi.org/project/opencv-python-headless/).
OpenCV and its bundled components retain their upstream licenses and package
notices. This optional dependency does not supply a second Qt GUI or run live
minimap tracking. User-supplied gameplay recordings and icon crops remain local.
