"""Explicit controlled-test tone only. Not a live positional alert."""
def play_test_sound():
    import math
    import struct
    import wave
    import config
    from voice.tts import _play_wav
    path=config.DATA_DIR/"league-vision"/"test-tone.wav"
    if not path.is_file():
        path.parent.mkdir(parents=True,exist_ok=True)
        rate=24000;count=round(rate*.045)
        pcm=b"".join(struct.pack("<h",round(4000*math.sin(2*math.pi*880*i/rate)*min(1,i/120,(count-i)/120))) for i in range(count))
        with wave.open(str(path),"wb") as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(rate);wav.writeframes(pcm)
    from threading import Event
    _play_wav(path,Event())


def measure_test_sound(samples=1):
    from core.latency import VoiceMetrics,LatencyTrace,trace_scope
    metrics=VoiceMetrics(True)
    for _ in range(samples):
        with trace_scope(LatencyTrace(metrics)):
            from core.latency import mark
            mark("league_detected")
            play_test_sound()
    return {"samples":samples,"metrics":metrics.report(),
            "measurement":"Software callback/DAC presentation estimates only; not acoustic output or detection/voice latency"}
