# Neko / Capri

Capri is a local Python/PySide6 desktop companion with voice, screen awareness
and read-only League integrations.

Run all commands below from the project root (`F:\Neko` on this workstation).

## Setup and launch

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -B -m voice.install_capri_voice
.\.venv\Scripts\python.exe -B main.py
```

Use the existing environment/model cache when already installed. Conversation
and screen analysis use the local Ollama service; settings live in `config.py`.
Text-only dependencies are in `requirements-core.txt`. Optional local League
vision/replay tools require `requirements-replay.txt`.

## Project layout

| Folder | Purpose |
|---|---|
| `core/` | Conversation, model clients, screen/spatial and League services |
| `gui/` | PySide6 UI, sprites, effects and workers |
| `voice/` | Capture, recognition, Kokoro, pronunciation and playback |
| `memory/` | Memory database code |
| `tools/` | Capri's callable assistant tools |
| `assets/` | Sprites and installed voice models |
| `benchmarks/` | Performance tools and their shared helpers |
| `scripts/` | Offline evaluation and visual preview utilities |
| `tests/` | Automated regressions and local test fixtures |
| `examples/` | Example calibration files |
| `docs/guides/` | League and personal-pronunciation instructions |
| `docs/reports/` | Historical measurements, evidence and limitations |
| `data/`, `artifacts/` | Private local state, caches and generated results |

## Tests and tools

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -r requirements-replay.txt
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider
.\.venv\Scripts\python.exe -B -m benchmarks.benchmark_voice --help
.\.venv\Scripts\python.exe -B -m benchmarks.benchmark_league_live --help
.\.venv\Scripts\python.exe -B -m scripts.evaluate_league_vision --help
.\.venv\Scripts\python.exe -B -m scripts.preview_teleport --help
.\.venv\Scripts\python.exe -B -m scripts.preview_pointing --help
```

Benchmark and evaluation scripts were moved out of the root. Use `python -m`
with the module paths above; their output/data paths still resolve from the
project root. Audio playback and diagnostic frame saving remain opt-in.

See the [League guide](docs/guides/LEAGUE_COMPANION_GUIDE.md),
[pronunciation guide](docs/guides/PERSONAL_PRONUNCIATION.md),
[live-observation report](docs/reports/PHASE7_LIVE_LEAGUE_REPORT.md), and
[third-party notices](THIRD_PARTY_NOTICES.md). Live competitive warnings remain
disabled pending verified Riot approval.
