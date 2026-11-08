"""Private line-based Piper worker; the parent owns cancellation and lifetime."""

import json
import sys
import wave


def main():
    from piper import PiperVoice

    voice = PiperVoice.load(sys.argv[1])
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            with wave.open(request["output"], "wb") as output:
                voice.synthesize_wav(request["text"], output)
            response = {"ok": True}
        except Exception as error:
            response = {"error": str(error)}
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
