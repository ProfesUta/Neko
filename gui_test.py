import sys

from PySide6.QtWidgets import QApplication

from gui.styles import DARK_THEME
from config import SYSTEM_PROMPT
from core.context import Context
from gui.window import NekoWindow
from memory import database as memory


def build_messages():
    context = Context()

    memories = memory.get_memories()

    if memories:
        memory_text = "\n".join(
            f"- {item}"
            for item in memories
        )
    else:
        memory_text = "No long-term memories yet."

    system_prompt = SYSTEM_PROMPT + f"""

LONG-TERM MEMORY:

These are facts that the user previously asked you to remember:

{memory_text}

Use these memories naturally when relevant.
Do not mention the memory system unless the user asks about it.

SHORT-TERM CONTEXT:

{context.get_text()}

Use this context to understand references such as:
- "it"
- "that"
- "this"
- "there"
- "the app"
- "the folder"
- "the website"

If the reference is ambiguous, ask the user instead of guessing.
"""

    messages = [
        {
            "role": "system",
            "content": system_prompt,
        }
    ]

    return messages, context


app = QApplication(sys.argv)

app.setStyleSheet(DARK_THEME)

messages, context = build_messages()

window = NekoWindow(
    messages,
    context,
)

window.show()

sys.exit(app.exec())