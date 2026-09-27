from pathlib import Path
import re
import wave
import winsound

from piper import PiperVoice


# --------------------------------------------------
# Piper settings
# --------------------------------------------------

VOICE_PATH = Path("F:/Neko/en_US-amy-medium.onnx")
OUTPUT_PATH = Path("F:/Neko/test_voice.wav")


# --------------------------------------------------
# Load Piper
# --------------------------------------------------

print("Loading Piper voice...")

voice = PiperVoice.load(str(VOICE_PATH))

print("Piper is ready!")
print()


# --------------------------------------------------
# Clean text before speaking
# --------------------------------------------------

def clean_for_speech(text):
    if not text:
        return ""

    # Remove stage directions such as:
    # *tilts head*
    # *crosses arms*
    text = re.sub(r"\*[^*]+\*", "", text)

    # Remove parenthesized tool/model notes such as:
    # (No tools needed for a simple greeting)
    text = re.sub(r"\([^)]*\)", "", text)

    # Replace awkward tsundere interjections.
    text = re.sub(r"\bHmph\.\.\.", "Hmm...", text, flags=re.IGNORECASE)
    text = re.sub(r"\bHmph\.", "Hmm.", text, flags=re.IGNORECASE)
    text = re.sub(r"\bHmph\b", "Hmm", text, flags=re.IGNORECASE)

    # Replace "Nya" sounds with something Piper pronounces naturally.
    text = re.sub(r"\bNyaa*~", "Meow.", text, flags=re.IGNORECASE)
    text = re.sub(r"\bNyaa*!", "Meow!", text, flags=re.IGNORECASE)
    text = re.sub(r"\bNyaa*\.\.\.", "Meow...", text, flags=re.IGNORECASE)
    text = re.sub(r"\bNyaa*\b", "Meow", text, flags=re.IGNORECASE)

    # Remove emoji and other non-ASCII symbols.
    text = re.sub(r"[^\x00-\x7F]+", "", text)

    # Clean up extra spaces.
    text = re.sub(r"[ \t]+", " ", text)

    # Clean up excessive blank lines.
    text = re.sub(r"\n\s*\n+", "\n", text)

    return text.strip()


# --------------------------------------------------
# Speak
# --------------------------------------------------

def speak(speech_text):
    if not speech_text:
        return

    speech_text = clean_for_speech(speech_text)

    if not speech_text:
        return

    with wave.open(str(OUTPUT_PATH), "wb") as wav_file:
        voice.synthesize_wav(
            speech_text,
            wav_file
        )

    winsound.PlaySound(
        str(OUTPUT_PATH),
        winsound.SND_FILENAME
    )


# --------------------------------------------------
# Standalone test
# --------------------------------------------------

if __name__ == "__main__":
    speak(
        "Hello, Uta. "
        "Hmm. "
        "I suppose I can talk to you for a little while, but..."
    )

    print()
    print("Done!")
