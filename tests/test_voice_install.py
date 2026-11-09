import zipfile

import pytest

from voice.install_capri_voice import ASSET_NAMES, extract_assets


def test_voice_install_extracts_model_data_but_excludes_bundled_executables(tmp_path):
    archive = tmp_path / "voice.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for name in ASSET_NAMES | {"espeak-ng-data/phontab", "espeak-ng-data/lang/en"}:
            output.writestr("capri-voice-1/" + name, b"data")
        output.writestr("capri-voice-1/onnxruntime.dll", b"unused")
        output.writestr("capri-voice-1/sherpa-onnx-c-api.dll", b"unused")
    destination = tmp_path / "assets"
    installed = extract_assets(archive, destination)
    assert ASSET_NAMES.issubset(installed)
    assert (destination / "espeak-ng-data/phontab").read_bytes() == b"data"
    assert not list(destination.rglob("*.dll"))


@pytest.mark.parametrize("bad", ["../escape", "/absolute", "C:/drive", "capri-voice-1/../../escape"])
def test_archive_paths_cannot_escape_voice_folder(tmp_path, bad):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("model.onnx", b"model")
        output.writestr(bad, b"bad")
    with pytest.raises(ValueError, match="Unsafe path"):
        extract_assets(archive, tmp_path / "assets")


def test_missing_voice_data_fails_installation(tmp_path):
    archive = tmp_path / "incomplete.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("model.onnx", b"model")
    with pytest.raises(ValueError, match="incomplete"):
        extract_assets(archive, tmp_path / "assets")
