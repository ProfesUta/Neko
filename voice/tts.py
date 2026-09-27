import re
import subprocess
import sys
from pathlib import Path


VOICE_PATH = Path("F:/Neko/assets/voices/en_US-amy-medium.onnx")
OUTPUT_FILE = Path("F:/Neko/voice/tests/test_voice.wav")


def clean_for_speech(text: str) -> str:
    """
    Clean Neko's response before sending it to Piper.

    This only affects what Piper speaks.
    The original response shown in the chat remains unchanged.
    """

    if not text:
        return ""

    # Remove stage directions.
    # Example:
    # *crosses arms*
    # *tilts head*
    text = re.sub(r"\*[^*]+\*", "", text)

    # Remove parenthesized notes.
    text = re.sub(r"\([^)]*\)", "", text)

    # Make "Hmph" easier for Piper to pronounce.
    text = re.sub(r"\bHmph\.\.\.", "Hmm...", text, flags=re.IGNORECASE)
    text = re.sub(r"\bHmph\.", "Hmm.", text, flags=re.IGNORECASE)
    text = re.sub(r"\bHmph\b", "Hmm", text, flags=re.IGNORECASE)

    # Replace Nya sounds with something Piper pronounces naturally.
    text = re.sub(r"\bNyaa*~", "Meow.", text, flags=re.IGNORECASE)
    text = re.sub(r"\bNyaa*!", "Meow!", text, flags=re.IGNORECASE)
    text = re.sub(r"\bNyaa*\.\.\.", "Meow...", text, flags=re.IGNORECASE)
    text = re.sub(r"\bNyaa*\b", "Meow", text, flags=re.IGNORECASE)

    # Remove emoji and other non-ASCII characters.
    text = text.encode("ascii", "ignore").decode("ascii")

    # Remove symbols that should never be spoken.
    #
    # Keep normal punctuation:
    # . , ! ? : ; ' " - ...
    #
    # Remove:
    # ~ @ # $ % ^ & * _ = + < > / \ | { } [ ] `
    text = re.sub(
        r"[~@#$%^&*_+=<>/\\|{}\[\]`]",
        "",
        text
    )

    # Remove text hearts such as <3.
    text = re.sub(r"<\s*3", "", text)

    # Clean spaces before punctuation.
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)

    # Clean repeated spaces.
    text = re.sub(r"[ \t]+", " ", text)

    # Clean excessive blank lines.
    text = re.sub(r"\n\s*\n+", "\n", text)

    return text.strip()


def speak(text: str):
    """
    Send text to Piper and play the generated WAV file.
    """

    cleaned_text = clean_for_speech(text)

    if not cleaned_text:
        return

    try:
        print("[DEBUG] Sending response to Piper...")

        # Use the Python module directly instead of relying on
        # the Windows PATH containing a "piper.exe" executable.
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "piper",
                "--model",
                str(VOICE_PATH),
                "--output_file",
                str(OUTPUT_FILE),
            ],
            input=cleaned_text,
            text=True,
            capture_output=True,
        )

        if process.returncode != 0:
            print("[TTS ERROR]")
            print(process.stderr)
            return

        # Play the generated WAV file.
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    f'(New-Object Media.SoundPlayer '
                    f'"{OUTPUT_FILE}").PlaySync()'
                ),
            ],
            capture_output=True,
            text=True,
        )

        print("[DEBUG] Piper finished speaking.")

    except Exception as e:
        print(f"[TTS ERROR] {e}")