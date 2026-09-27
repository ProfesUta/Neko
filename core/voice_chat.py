from core.chat import chat
from voice import record_until_silence, transcribe_audio


def voice_chat(messages, context):
    print()
    print("🐱 Hands-free VAD voice mode started.")
    print("Neko will listen until you stop speaking.")
    print("Press Ctrl+C to stop voice mode.")
    print()

    try:
        while True:
            audio = record_until_silence()

            if audio is None:
                continue

            text = transcribe_audio(audio)

            if not text:
                print("Neko: I didn't understand anything.")
                print()
                continue

            print(f"You: {text}")
            print("🧠 Neko is thinking...")

            messages.append({
                "role": "user",
                "content": text
            })

            answer = chat(messages, context)

            print(f"Neko: {answer}")
            print()

    except KeyboardInterrupt:
        print()
        print("🐱 Voice mode stopped.")
        print()