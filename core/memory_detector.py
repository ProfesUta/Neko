import ollama

from config import MODEL


DETECTOR_PROMPT = """
You are a strict long-term memory detector.

Decide whether the user's message contains a fact that the user clearly states about themselves, their preferences, identity, relationships, or lasting opinions.

Return ONLY one of:

SAVE: <short normalized memory>
NO

IMPORTANT:
- Only save facts explicitly stated by the user.
- Never infer a fact from context.
- Never turn a greeting into a fact.
- Never turn a joke into a fact.
- If the statement is ambiguous, return NO.
- When unsure, return NO.

Do not save temporary actions, temporary tasks, web searches, tool results, greetings, jokes, or casual conversation.

Do not save passwords, authentication codes, private keys, financial credentials, or other highly sensitive information.
"""


def detect_memory(user_message):
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
