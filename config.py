import os
from pathlib import Path


MODEL = os.getenv("CAPRI_MODEL", "qwen2.5:32b")
CONVERSATION_MODEL = os.getenv("CAPRI_CONVERSATION_MODEL", "auto")
CONVERSATION_STREAMING_ENABLED = True
CONVERSATION_MAX_TOKENS = 256
CONVERSATION_HISTORY_TURNS = 6
MODEL_KEEP_ALIVE = "15m"
LEAGUE_LIVE_DATA_ENABLED = True
LEAGUE_REQUEST_TIMEOUT_SECONDS = 0.4
LEAGUE_CACHE_SECONDS = 1.0
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
VOICE_PRELOAD_ON_STARTUP = os.getenv("CAPRI_PRELOAD_VOICE", "1") != "0"
WHISPER_LANGUAGE = os.getenv("CAPRI_WHISPER_LANGUAGE", "en")
WHISPER_BEAM_SIZE = 1  # Greedy decoding for low-latency commands; set 5 for wider search.
VOICE_SILENCE_MS = 500
MAX_RECORD_SECONDS = 45
TTS_TIMEOUT_SECONDS = 90
TTS_BACKEND = os.getenv("CAPRI_TTS_BACKEND", "kokoro").lower()
KOKORO_VOICE_DIR = Path(os.getenv("CAPRI_VOICE_DIR", str(Path(__file__).resolve().parent / "assets" / "voices" / "capri-kokoro")))
KOKORO_SPEAKER_ID = 53
KOKORO_SPEED = 1.1
TTS_PERSISTENT_ENABLED = True
TTS_PREFETCH_ENABLED = True
TTS_STREAM_MIN_CHARS = 32  # Group tiny interjections into natural spoken phrases.
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
