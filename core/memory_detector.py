import ollama

from config import MODEL


DETECTOR_PROMPT = """
You are a memory detector for a personal AI assistant.

Your job is to decide whether the user's message contains a useful
long-term personal fact, preference, identity, relationship, or opinion
that should be remembered for future conversations.

Return ONLY one of these two formats:

SAVE: <short normalized memory>
NO

SAVE examples:
User: I'm Uta.
SAVE: The user's name is Uta.

User: I hate coding.
SAVE: The user dislikes coding.

User: I like programming but don't like coding.
SAVE: The user likes programming but dislikes coding.

User: My favorite animal is cats.
SAVE: The user's favorite animal is cats.

User: You are my daughter.
SAVE: Neko is the user's daughter.

NO examples:
User: Hello.
NO

User: Open Calculator.
NO

User: Search for Python tutorials.
NO

User: What files are in F:\\Neko?
NO

User: I'm opening Notepad.
NO

Do not save temporary actions, temporary tasks, web searches,
tool results, or ordinary conversation.

Do not save passwords, authentication codes, private keys,
financial credentials, or other highly sensitive secrets.

If you are unsure, return NO.
"""


def detect_memory(user_message: str):
    response = ollama.chat(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": DETECTOR_PROMPT
            },
            {
                "role": "user",
                "content": user_message
            }
        ]
    )

    result = response.message.content.strip()

    if result.upper() == "NO":
        return None

    if result.upper().startswith("SAVE:"):
        memory = result[5:].strip()

        if memory:
            return memory

    return None