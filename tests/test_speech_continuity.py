from threading import Event

import pytest

import config
from core.cancellation import OperationCancelled, check_cancelled
from core.interaction import InteractionState
from core.streaming import StreamingReply
from voice import tts


class Segment:
    def __init__(self, text):
        self.text, self.closed = text, False

    def close(self):
        assert not self.closed, "Segment was discarded twice"
        self.closed = True


def test_next_segment_is_generated_during_current_playback():
    playing, next_ready = Event(), Event()
    created, spoken = [], []

    def prepare(text):
        if created:
            assert playing.wait(1)
            next_ready.set()
        segment = Segment(text)
        created.append(segment)
        return segment

    def play(segment):
        if not spoken:
            playing.set()
            assert next_ready.wait(1), "Next synthesis waited for playback to finish"
        spoken.append(segment.text)

    reply = StreamingReply(Event(), InteractionState(), "Explain", lambda text: None,
                           play, prepare=prepare, discard=lambda segment: segment.close())
    try:
        reply.feed("First sentence has useful context. Second sentence adds the details.")
        reply.finish()
        assert spoken == ["First sentence has useful context.", "Second sentence adds the details."]
        assert all(segment.closed for segment in created)
    finally:
        reply.abort()


def test_stop_discards_current_queued_and_in_progress_segments():
    playing, full = Event(), Event()
    created, spoken = [], []

    def prepare(text):
        segment = Segment(text)
        created.append(segment)
        if len(created) == 4:
            full.set()
        return segment

    def play(segment):
        spoken.append(segment.text)
        playing.set()
        reply.cancellation.wait(2)
        check_cancelled(reply.cancellation)

    reply = StreamingReply(Event(), InteractionState(), "Explain", lambda text: None,
                           play, prepare=prepare, discard=lambda segment: segment.close())
    try:
        reply.feed(" ".join(f"Detail number {i} is worth discussing." for i in range(10)) + " ")
        assert playing.wait(1) and full.wait(1)
        assert len(created) <= 4, "Synthesis ran ahead without a bound"
        reply.abort()
        assert len(spoken) == 1
        assert all(segment.closed for segment in created)
        assert not reply._thread.is_alive() and not reply._producer.is_alive()
    finally:
        reply.abort()


def test_decoder_failure_stops_playback_and_preserves_the_real_error():
    playing, ended = Event(), Event()
    created = []

    def prepare(text):
        if created:
            assert playing.wait(1)
            raise RuntimeError("Speech decoder failed")
        segment = Segment(text)
        created.append(segment)
        return segment

    def play(segment):
        playing.set()
        try:
            reply.cancellation.wait(2)
            check_cancelled(reply.cancellation)
        finally:
            ended.set()

    reply = StreamingReply(Event(), InteractionState(), "Explain", lambda text: None,
                           play, prepare=prepare, discard=lambda segment: segment.close())
    try:
        reply.feed("This is the first useful sentence. Here comes another sentence.")
        with pytest.raises(RuntimeError, match="Speech decoder failed"):
            reply.finish()
        assert ended.is_set() and all(segment.closed for segment in created)
    finally:
        reply.abort()


def test_prefetched_duplicate_is_rechecked_before_playing():
    interaction, both_prepared = InteractionState(), Event()
    created, spoken = [], []
    sentence = "Let's work through the issue one step at a time."

    def prepare(text):
        segment = Segment(text)
        created.append(segment)
        if len(created) == 2:
            both_prepared.set()
        return segment

    def play(segment):
        assert both_prepared.wait(1)
        spoken.append(segment.text)
        interaction.record_spoken(segment.text)

    reply = StreamingReply(Event(), interaction, "Explain", lambda text: None,
                           play, prepare=prepare, discard=lambda segment: segment.close())
    try:
        reply.feed(sentence + " " + sentence)
        reply.finish()
        assert spoken == [sentence]
        assert all(segment.closed for segment in created)
    finally:
        reply.abort()


def test_tiny_interjections_are_grouped_and_final_short_answers_still_play():
    prepared = []
    reply = StreamingReply(Event(), InteractionState(), "Hello", lambda text: None,
                           lambda item: None, prepare=lambda text: prepared.append(text) or text,
                           min_segment_chars=32)
    try:
        reply.feed("Hey. ")
        reply.feed("Take a breath. ")
        assert not prepared
        reply.feed("We'll figure this out together. ")
        reply.finish()
        assert prepared == ["Hey. Take a breath. We'll figure this out together."]
    finally:
        reply.abort()
    prepared.clear()
    reply = StreamingReply(Event(), InteractionState(), "Hello", lambda text: None,
                           lambda item: None, prepare=lambda text: prepared.append(text) or text,
                           min_segment_chars=32)
    reply.feed("Got it.")
    reply.finish()
    assert prepared == ["Got it."]


def test_preparation_defers_audio_and_target_confirmation_until_playback(monkeypatch):
    calls = []
    def synthesize(text, path, cancellation):
        calls.append(("generate", text))
        path.write_text(text)
    def confirm():
        calls.append(("confirm", None))
        return "The target moved. I need to locate it again."
    def play(path, cancellation, started):
        calls.append(("play", path.read_text()))
        started()
    monkeypatch.setattr(tts, "_synthesize", synthesize)
    monkeypatch.setattr(tts, "_play_wav", play)
    prepared = tts.prepare_speech("Right there!", before_playback=confirm,
                                  on_playback_start=lambda: calls.append(("started", None)))
    path = prepared.path
    assert calls == [("generate", "Right there!")]
    with prepared:
        prepared.play()
    assert [kind for kind, _ in calls] == ["generate", "confirm", "generate", "play", "started"]
    assert calls[3][1] == "The target moved. I need to locate it again."
    assert not path.exists() and not path.parent.exists()
    with pytest.raises(RuntimeError, match="consumed"):
        prepared.play()


def test_failed_preparation_and_cancelled_playback_remove_temporary_files(monkeypatch):
    paths = []
    def fail(text, path, cancellation):
        path.write_text(text)
        paths.append(path)
        raise RuntimeError("Synthesis failed")
    monkeypatch.setattr(tts, "_synthesize", fail)
    with pytest.raises(RuntimeError):
        tts.prepare_speech("Hello")
    assert not paths[0].parent.exists()
    monkeypatch.setattr(tts, "_synthesize", lambda text, path, token: path.write_text(text))
    monkeypatch.setattr(tts, "_play_wav", lambda *a: pytest.fail("Obsolete audio played"))
    stop = Event()
    prepared = tts.prepare_speech("Hello", cancel_event=stop)
    path = prepared.path
    stop.set()
    with pytest.raises(OperationCancelled), prepared:
        prepared.play()
    assert not path.parent.exists()


def test_conversation_worker_uses_prefetch_and_retains_delivery_callbacks(monkeypatch):
    import ollama
    from core import conversation
    from core.context import Context
    from gui.workers import ChatWorker

    monkeypatch.setattr(config, "CONVERSATION_STREAMING_ENABLED", True)
    monkeypatch.setattr(config, "TTS_PREFETCH_ENABLED", True)
    monkeypatch.setattr(config, "TTS_STREAM_MIN_CHARS", 0)
    monkeypatch.setattr(conversation, "conversation_model", lambda: "test-chat")
    first_playing, next_ready = Event(), Event()
    prepared, played, styles = [], [], []

    class Speech(Segment):
        def __init__(self, text, options):
            super().__init__(text)
            self.options = options
        def play(self):
            self.options["on_playback_start"]()
            if not played:
                first_playing.set()
                assert next_ready.wait(1)
            played.append(self.text)

    def prepare(text, **options):
        segment = Speech(text, options)
        prepared.append(segment)
        styles.append(options["style"])
        if len(prepared) == 2:
            next_ready.set()
        return segment

    def generate(**kwargs):
        kwargs["on_text"]("Let's take this one step at a time. ")
        assert first_playing.wait(1), "First speech waited for model completion"
        kwargs["on_text"]("Gravity pulls objects toward each other.")
        return ollama.ChatResponse(message={"role": "assistant", "content":
            "Let's take this one step at a time. Gravity pulls objects toward each other."})

    monkeypatch.setattr(tts, "prepare_speech", prepare)
    monkeypatch.setattr(tts, "speak", lambda *a, **kw: pytest.fail("Used sequential speech"))
    monkeypatch.setattr(conversation, "request_chat", generate)
    worker = ChatWorker([], Context(), "I'm frustrated. Explain gravity.", speak_reply=True)
    errors = []
    worker.error.connect(errors.append)
    worker.run()
    assert not errors and len(played) == 2
    assert styles == ["warm", "warm"] and all(segment.closed for segment in prepared)


def test_prefetch_failure_cancels_model_and_reports_decoder_error(monkeypatch):
    from core import conversation
    from core.context import Context
    from gui.workers import ChatWorker
    monkeypatch.setattr(config, "CONVERSATION_STREAMING_ENABLED", True)
    monkeypatch.setattr(config, "TTS_PREFETCH_ENABLED", True)
    monkeypatch.setattr(config, "TTS_STREAM_MIN_CHARS", 0)
    monkeypatch.setattr(conversation, "conversation_model", lambda: "test-chat")
    def fail(*a, **kw):
        raise RuntimeError("Decoder unavailable")
    def generate(**kwargs):
        kwargs["on_text"]("Here is the first sentence. ")
        assert kwargs["cancel_event"].wait(1)
        check_cancelled(kwargs["cancel_event"])
    monkeypatch.setattr(tts, "prepare_speech", fail)
    monkeypatch.setattr(conversation, "request_chat", generate)
    worker = ChatWorker([], Context(), "Explain gravity", speak_reply=True)
    errors = []
    worker.error.connect(errors.append)
    worker.run()
    assert errors == ["Decoder unavailable"]


