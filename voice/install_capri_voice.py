"""Install the creator-published local voice assets with their published checksum."""

import hashlib
import json
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath

import httpx

import config

PACK_URL = "https://github.com/lachydotmcg/capri-desktop/releases/download/voice-pack-1/capri-voice-1.zip"
PACK_SHA256 = "9fae085696d68a580d6b1a953d7b253129bfe506a2c016421b306b7237d3214c"
PACK_SIZE = 316041808
LICENSE_URL = "https://raw.githubusercontent.com/lachydotmcg/capri-desktop/main/LICENSE.md"
ASSET_NAMES = {"model.onnx", "voices.bin", "tokens.txt", "lexicon-us-en.txt", "lexicon-gb-en.txt"}


def extract_assets(archive, destination):
    destination = Path(destination).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    installed = []
    with zipfile.ZipFile(archive) as bundle:
        models = [item.filename.replace("\\", "/") for item in bundle.infolist()
                  if PurePosixPath(item.filename.replace("\\", "/")).name == "model.onnx"]
        if len(models) != 1:
            raise ValueError("Expected exactly one voice model in the archive")
        prefix = models[0][:-len("model.onnx")]
        for item in bundle.infolist():
            path = PurePosixPath(item.filename.replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts or any(":" in part for part in path.parts):
                raise ValueError("Unsafe path in voice archive")
            if stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError("Symbolic links are not accepted in voice archives")
            name = path.as_posix()
            if not name.startswith(prefix):
                continue
            name = name[len(prefix):]
            if not (name in ASSET_NAMES or name.startswith("espeak-ng-data/")) or item.is_dir():
                continue
            target = (destination / name).resolve()
            if not target.is_relative_to(destination):
                raise ValueError("Voice asset escaped its install folder")
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(item) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            installed.append(name)
    required = ASSET_NAMES | {"espeak-ng-data/phontab"}
    missing = required - set(installed)
    if missing:
        raise ValueError("Voice archive is incomplete: " + ", ".join(sorted(missing)))
    return installed


def install(destination=None):
    destination = Path(destination or config.KOKORO_VOICE_DIR).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / ".voice-download.zip"
    print("Downloading creator's Capri voice (316 MB)...", flush=True)
    digest, size, next_progress = hashlib.sha256(), 0, 32 * 1024 * 1024
    with httpx.Client(timeout=httpx.Timeout(60, connect=15), follow_redirects=True, trust_env=False) as client:
        with client.stream("GET", PACK_URL) as response:
            response.raise_for_status()
            with archive.open("wb") as output:
                for chunk in response.iter_bytes(1024 * 1024):
                    size += len(chunk)
                    if size > PACK_SIZE:
                        raise ValueError("Voice download exceeds the published size")
                    digest.update(chunk)
                    output.write(chunk)
                    if size >= next_progress:
                        print(f"Downloaded {size // (1024 * 1024)} / {PACK_SIZE // (1024 * 1024)} MB", flush=True)
                        next_progress += 32 * 1024 * 1024
        if size != PACK_SIZE or digest.hexdigest() != PACK_SHA256:
            raise ValueError("Voice download did not match the creator's published checksum")
        license_response = client.get(LICENSE_URL)
        license_response.raise_for_status()
    installed = extract_assets(archive, destination)
    (destination / "LICENSE-CAPRI.md").write_text(license_response.text, encoding="utf-8")
    (destination / "ATTRIBUTION.md").write_text(
        "# Capri local voice\n\nVoice pack provided by Lachy and the Capri project:\n"
        "https://github.com/lachydotmcg/capri-desktop\n\n"
        "Creator-provided preset: Her own, speaker 53, speed 1.1, en-us.\n"
        "See LICENSE-CAPRI.md for the project's PolyForm Noncommercial license and required notices.\n"
        "Third-party Kokoro/sherpa-onnx components retain their upstream licenses.\n"
        "Runtime: https://github.com/k2-fsa/sherpa-onnx (Apache-2.0).\n"
        "Only model/text/phonemizer data were extracted; bundled native DLLs are not used.\n",
        encoding="utf-8")
    (destination / "install.json").write_text(json.dumps({
        "source": PACK_URL, "sha256": PACK_SHA256, "size": size,
        "preset": {"speaker": 53, "speed": 1.1, "language": "en-us"}, "files": installed,
    }, indent=2), encoding="utf-8")
    archive.unlink()
    print(f"Installed verified voice assets to {destination}", flush=True)


if __name__ == "__main__":
    install()
