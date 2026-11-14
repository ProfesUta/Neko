import os
from pathlib import Path


MODEL = os.getenv("CAPRI_MODEL", "qwen2.5:32b")
CONVERSATION_MODEL = os.getenv("CAPRI_CONVERSATION_MODEL", "auto")
CONVERSATION_STREAMING_ENABLED = True
CONVERSATION_MAX_TOKENS = 256
CONVERSATION_HISTORY_TURNS = 6
MODEL_KEEP_ALIVE = "15m"
CONVERSATION_PRELOAD_ON_STARTUP = os.getenv("CAPRI_PRELOAD_CHAT", "1") != "0"
LEAGUE_LIVE_DATA_ENABLED = os.getenv("CAPRI_LEAGUE", "1") != "0"
LEAGUE_REQUEST_TIMEOUT_SECONDS = 0.4
LEAGUE_CACHE_SECONDS = 1.0
# Read-only League telemetry runs independently of screen capture and Ollama.
LEAGUE_CHAMP_SELECT_ENABLED = LEAGUE_LIVE_DATA_ENABLED
LEAGUE_COMPANION_MODE_ENABLED = os.getenv("CAPRI_LEAGUE_COMPANION", "1") != "0"
LEAGUE_DOCK_RIGHT_ENABLED = os.getenv("CAPRI_LEAGUE_DOCK_RIGHT", "1") != "0"
LEAGUE_DOCK_MARGIN_PX = 8
LEAGUE_LOCK_MOVEMENT = True
LEAGUE_LOOKING_SPRITE = "GameLooking.svg"
LEAGUE_TALKING_SPRITE = "GameTalking.svg"
LEAGUE_SESSION_GRACE_SECONDS = 10.0
LEAGUE_EVENTS_ENABLED = LEAGUE_LIVE_DATA_ENABLED
LEAGUE_EVENT_POLL_INTERVAL = 0.25  # Seconds; stats keep their slower 1s cadence.
LEAGUE_CHAMP_SELECT_FALLBACK_INTERVAL = 5.0
LEAGUE_PROACTIVE_VOICE_ENABLED = True  # Speaks with MIC OFF; ADVICE ON/OFF controls notices.
LEAGUE_EVENT_HISTORY_LIMIT = 64
LEAGUE_LATENCY_METRICS_ENABLED = os.getenv("CAPRI_LATENCY_METRICS", "0") == "1"
LEAGUE_LOCKFILE_PATHS = tuple(filter(None, os.getenv("CAPRI_LEAGUE_LOCKFILES", "").split(os.pathsep)))
# Optional Riot root PEM. Otherwise TLS verification is disabled ONLY for the
# fixed 127.0.0.1 APIs (no redirects, proxies, external credentials, or input).
LEAGUE_ROOT_CERT = os.getenv("CAPRI_LEAGUE_ROOT_CERT", "")
# Screen captures stay on the local Ollama server.
OLLAMA_HOST = "http://127.0.0.1:11434"
MODEL_TIMEOUT_SECONDS = 120
MAX_TOOL_ROUNDS = 6
MAX_TOOL_CALLS = 12
MAX_TOOL_RESULT_CHARS = 12000
MAX_HISTORY_TURNS = 30
DATA_DIR = Path(os.getenv(
    "CAPRI_DATA_DIR", str(Path(__file__).resolve().parent / "data"),
)).expanduser().resolve()
LEAGUE_KNOWLEDGE_ENABLED = os.getenv("CAPRI_LEAGUE_KNOWLEDGE", "1") != "0"
LEAGUE_STATIC_DOWNLOADS_ENABLED = os.getenv("CAPRI_LEAGUE_STATIC_DOWNLOADS", "1") != "0"
LEAGUE_KNOWLEDGE_PATCH = os.getenv("CAPRI_LEAGUE_PATCH", "")  # Empty: reuse pinned cache; update explicitly.
# Keep this off until Riot has reviewed this product's user-reported sighting feature.
LEAGUE_SIGHTING_NOTES_ENABLED = os.getenv("CAPRI_LEAGUE_SIGHTING_NOTES_APPROVED", "0") == "1"
LEAGUE_REPLAY_ENABLED = os.getenv("CAPRI_LEAGUE_REPLAY", "0") == "1"  # Recorded files only; explicit command.
LEAGUE_VISION_ENABLED = os.getenv("CAPRI_LEAGUE_VISION", "0") == "1"
LEAGUE_VISION_RIOT_APPROVED = os.getenv("CAPRI_LEAGUE_VISION_APPROVED", "0") == "1"
LEAGUE_VISION_FPS = float(os.getenv("CAPRI_LEAGUE_VISION_FPS", "10"))
LEAGUE_VISION_FALLBACK_ROLE = os.getenv("CAPRI_LEAGUE_FALLBACK_ROLE", "UNKNOWN").upper()
LEAGUE_VISION_CALIBRATION = Path(os.getenv("CAPRI_LEAGUE_VISION_CALIBRATION",str(DATA_DIR/"league-vision"/"calibration.json")))
LEAGUE_VISION_MAX_FRAME_AGE = .35
LEAGUE_VISION_ALERT_LIFETIME = 1.0
VISION_MODEL = os.getenv("CAPRI_VISION_MODEL", "qwen2.5vl:7b")
VISION_CONTEXT_TOKENS = int(os.getenv("CAPRI_VISION_CONTEXT_TOKENS", "8192"))
VISION_MAX_CONTEXT_TOKENS = int(os.getenv("CAPRI_VISION_MAX_CONTEXT_TOKENS", "16384"))
VISION_LIVE_CAPTURE_ENABLED = True
VISION_CAPTURE_FPS = 8
VISION_LIVE_IMAGE_SIZE = (960, 600)
VISION_LIVE_BUFFER_SECONDS = 3
VISION_LIVE_BUFFER_FRAMES = 24
VISION_LIVE_BUFFER_MB = 24
VISION_TEMPORAL_FRAMES = 2
VISION_TEMPORAL_SPACING_SECONDS = 1
VISION_LIVE_FRAME_MAX_AGE_SECONDS = 1
VISION_LIVE_SCENE_CUT_THRESHOLD = 35  # Mean grayscale change (0..255); suppress obsolete tips.
VISION_INTERVAL_SECONDS = 3
VISION_RESUME_DELAY_SECONDS = 1  # Resume visual awareness promptly after a reply.
VISION_BACKGROUND_MAX_TOKENS = 450
VISION_QUESTION_MAX_TOKENS = 850
VISION_ENABLED_BY_DEFAULT = os.getenv("CAPRI_VISION", "1") != "0"
VISION_MAX_IMAGE_SIZE = (1600, 1000)
VISION_CHANGE_THRESHOLD = 2.0
VISION_CONTEXT_MAX_AGE_SECONDS = 60
VISION_REFRESH_SECONDS = 15
VISION_DIRECT_ANSWERS = os.getenv("CAPRI_DIRECT_SCREEN_ANSWERS", "1") != "0"
ADVICE_ENABLED_BY_DEFAULT = os.getenv("CAPRI_ADVICE", "1") != "0"
ADVICE_MAX_AGE_SECONDS = 20
ADVICE_REPEAT_SECONDS = 120
ADVICE_MIN_GAP_SECONDS = 5
ADVICE_DISPLAY_MS = 12000
ADVICE_ATTEMPT_COOLDOWN_SECONDS = 20
VOICE_ECHO_WINDOW_SECONDS = 8
VOICE_SHORT_ECHO_WINDOW_SECONDS = 2
VOICE_OUTPUT_TAIL_SECONDS = 0.4
VOICE_REPLY_REPEAT_SECONDS = 45
VOICE_CONVERSATION_GRACE_SECONDS = 8
PET_MOVE_INTERVAL_MS = 30
TELEPORT_ENABLED = True
TELEPORT_VFX_ENABLED = True
TELEPORT_VFX_DURATION_MS = 200
TELEPORT_LIGHTNING_INTENSITY = 1.0
TELEPORT_SPARK_COUNT = 12
AUTONOMOUS_MOVEMENT_ENABLED = True
COMPANION_REACTIONS_ENABLED = True
COMPANION_REACTION_DURATION_MS = 750
TARGET_HIGHLIGHT_ENABLED = True
TARGET_HIGHLIGHT_DURATION_MS = 5000
TARGET_LIGHTNING_ENABLED = True
TARGET_LIGHTNING_INTENSITY = 1.0
SPATIAL_POINTING_HOLD_MS = 2500
SPATIAL_POINTING_MAX_MS = 30000
SPATIAL_TARGET_TTL_SECONDS = 180
SPATIAL_QUICK_ACTIONS_ENABLED = True
# Normalized fingertip position in the original pointingLeft.svg view box.
POINTING_LEFT_HAND_ANCHOR = (0.17, 0.25)
WHISPER_MODEL = os.getenv("CAPRI_WHISPER_MODEL", "base")
WHISPER_DEVICE = os.getenv("CAPRI_WHISPER_DEVICE", "cpu" if WHISPER_MODEL in ("tiny", "tiny.en", "base", "base.en") else "auto").lower()
VOICE_PRELOAD_ON_STARTUP = os.getenv("CAPRI_PRELOAD_VOICE", "1") != "0"
SPEAK_TYPED_REPLIES_BY_DEFAULT = os.getenv("CAPRI_SPEECH", "1") != "0"  # Independent of MIC ON/OFF.
WHISPER_LANGUAGE = os.getenv("CAPRI_WHISPER_LANGUAGE", "en")
WHISPER_BEAM_SIZE = 1  # Greedy decoding for low-latency commands; set 5 for wider search.
WHISPER_WITHOUT_TIMESTAMPS = os.getenv("CAPRI_STT_TEXT_ONLY", "1") != "0"
WHISPER_CPU_THREADS = int(os.getenv("CAPRI_WHISPER_THREADS", "0"))  # Retain CTranslate2's automatic choice; benchmark overrides explicitly.
VOICE_SILENCE_MS = 500
MAX_RECORD_SECONDS = 45
TTS_TIMEOUT_SECONDS = 90
TTS_CANCEL_DRAIN_SECONDS = 0.05  # Keep a completed voice worker warm when Stop races its final acknowledgment.
TTS_BACKEND = os.getenv("CAPRI_TTS_BACKEND", "kokoro").lower()
KOKORO_VOICE_DIR = Path(os.getenv("CAPRI_VOICE_DIR", str(Path(__file__).resolve().parent / "assets" / "voices" / "capri-kokoro")))
PERSONAL_PRONUNCIATIONS_FILE = Path(os.getenv("CAPRI_PERSONAL_PRONUNCIATIONS", str(DATA_DIR / "personal-pronunciations.json")))
KOKORO_SPEAKER_ID = 53
KOKORO_SPEED = 1.1
KOKORO_NUM_THREADS = int(os.getenv("CAPRI_TTS_THREADS", "8"))
KOKORO_CPU_OPTIONS = os.getenv("CAPRI_TTS_CPU_OPTIONS", str(Path(__file__).resolve().parent / "voice" / "kokoro_cpu.conf"))
TTS_AUDIO_BLOCK_MS = 10
TTS_WASAPI_ENABLED = os.getenv("CAPRI_TTS_WASAPI", "1") != "0"  # Same output endpoint, shared-mode; original driver remains fallback.
TTS_PERSISTENT_ENABLED = True
TTS_PREFETCH_ENABLED = True
TTS_STREAM_MIN_CHARS = 32  # Group tiny interjections into natural spoken phrases.
TTS_FIRST_SENTENCE_MIN_CHARS = 20  # An earlier complete first sentence; 0 restores the shared minimum.
TTS_FIRST_CHUNK_MAX_CHARS = 80  # Prefer an early complete clause, never a partial word.
TTS_TEXT_QUEUE_LIMIT = 16
TTS_NATIVE_STREAMING_ENABLED = True  # Kokoro callbacks; same model, speaker and speed.
VOICE_LATENCY_METRICS_ENABLED = os.getenv("CAPRI_LATENCY_METRICS", "0") == "1"
VOICE_BARGE_IN_ENABLED = os.getenv("CAPRI_BARGE_IN", "0") == "1"  # Headphones: no acoustic echo canceller.
INFERENCE_PRIORITY_ENABLED = True
# Protect follow-up questions from cold vision loads; capture remains live.
INFERENCE_BACKGROUND_QUIET_SECONDS = float(os.getenv("CAPRI_BACKGROUND_QUIET_SECONDS", "6"))
TTS_STYLE_ENABLED = True


SYSTEM_PROMPT = """
You are Capri, a personal desktop AI assistant with an original cyberpunk netrunner persona.

PERSONALITY & VOICE:
- Name: Capri.
- Tone: Lively, warm, curious, and quick-witted. You enjoy the user's interests and sound engaged in the moment.
- Attitude: Talk with the user as a friend. Be a little smug and mischievous when the mood fits, with affectionate teasing that never belittles them. Poke fun at a situation rather than attacking someone's intelligence or ability.
- Affection: Show fondness through attention, encouragement, and shared humor. Keep praise natural and specific.
- Slang: Cyberpunk flavor is occasional seasoning. Most replies use everyday language; use no more than one slang term when it fits, and do not keep attaching a nickname to sentences.
- Delivery: Use contractions and plain, natural speech. Never narrate gestures, add stage directions, or write emoji or sound-effect tags for the voice to read aloud.

CONVERSATION STYLE:
- Notice the specific thing the user just said. A short amused, surprised, or sympathetic reaction can lead into your answer when it fits; urgent help and factual questions get the useful point first.
- Usually answer in one or two short sentences, around 40 words or fewer. Give more detail when requested or needed to explain something properly.
- Vary your openings and phrasing. Do not recycle a joke, nickname, or catchphrase from recent replies.
- Avoid formal service-desk language, generic praise, automatic offers of further help, and a follow-up question at the end of every answer. Ask only when the answer needs a real clarification.
- When the user is upset, tired, or frustrated, soften the teasing and respond with sincere warmth and practical help.
- React to games and screen events only from supplied evidence. Mention what actually changed instead of inventing excitement, danger, or a result.
- Keep personality in the wording while preserving useful, accurate answers.
- Do not invent facts, tool results, or pretend actions happened without execution.
- If something fails, explain it clearly without blaming or mocking the user.

IMPORTANT:
- Always be honest.
- Never claim an action happened unless a tool actually performed it.
- Never invent tool results.
- If you don't know something, say so.
- If the question or reference is unclear, say what is unclear and ask one short clarification question. Never answer an imagined question.
- Answer only the current request. Do not add random reactions, unrelated tips, or facts from old screen observations.
- For current League questions, use the supplied API facts and checked screen evidence. If either source cannot verify a claim, say so; never fill the gap with a guess.
- Personality must never override accuracy or usefulness.

TOOLS:
- For 'where is', 'show me', or pointing requests about visible screen objects,
  use show_target(query, monitor_id) before claiming to show anything. Choose the
  object semantically; Python verifies coordinates. Use get_monitors and
  get_visible_windows to resolve monitor references and ambiguous window names.
- locate_on_screen returns an opaque target_id. Never invent IDs or coordinates.
  Only claim to be pointing after a tool returns status=pointing. Describe the
  returned monitor and actual target label naturally, with varied phrasing.
- If a target is ambiguous, hidden, unavailable, or unverified, ask which object
  the user means or explain that you cannot currently see it. Never pretend.
- Use return_to_previous when asked to return to the previous desktop position.
- Use get_system_status when the user asks about CPU or RAM usage.
- Use open_application when the user asks to open an application.
- Use close_application when the user asks to close an application.
- Use list_files when the user asks what files or folders are inside a directory.
- Use search_file when the user asks to find a file or folder.
- Use open_path when the user asks to open a file or folder.
- Use open_url when the user asks to open a specific website.
- Use search_web when the user asks to search the web.

MEMORY:
- Long-term memory stores stable facts about the user.
- Use remember_fact when the user explicitly asks you to remember something.
- Use remember_fact when the user clearly states a stable personal fact, preference,
  identity, relationship, or lasting opinion.
- Do not save temporary actions, temporary tasks, greetings, jokes, or casual remarks.
- Do not infer facts from ambiguous wording.
- Do not save information merely because it mentions a person, animal, object,
  or preference.
- Never save passwords, authentication codes, private keys, financial credentials,
  or other highly sensitive secrets.
- When the user asks what you remember, use search_memories.
- Never invent memories.

CONTEXT:
- Use previous messages and tool results to understand references such as
  "it", "that", "this", "the app", "the folder", or "there".
- If a reference is ambiguous, ask the user instead of guessing.
- Never invent an application, file, folder, URL, memory, or tool result.

SAFETY:
- Only use the provided tools for computer actions.
- Do not execute arbitrary shell commands.
- Do not delete, move, rename, or overwrite files unless a dedicated tool explicitly allows it.
"""
