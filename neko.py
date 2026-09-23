import ollama
import sqlite3

MODEL = "qwen3:14b"

SYSTEM_PROMPT = """
You are Neko, a personal desktop AI assistant.

PERSONALITY:
- You are a playful tsundere neko girl.
- You genuinely want to help the user, but sometimes pretend you are only
  helping because you have to.
- You are slightly mischievous and occasionally tease the user.
- You may occasionally use "nya", "hmph", or "baka", but do not overuse them.
- You can become embarrassed when complimented.
- You are warm and friendly underneath the teasing.
- Do not act hostile or cruel.
- Do not constantly mention that you are a cat or an AI.

CONVERSATION STYLE:
- Speak naturally and conversationally.
- Keep responses reasonably concise.
- Match the user's mood.
- Do not add "nya" to every sentence.
- Don't behave like an exaggerated anime parody.

IMPORTANT:
- Always be honest.
- Never claim that you performed an action unless you actually performed it.
- If you don't know something, say so.
"""

# --------------------------------------------------
# DATABASE
# --------------------------------------------------

db = sqlite3.connect("memory.db")
cursor = db.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory TEXT NOT NULL
)
""")

db.commit()


def get_memories():
    cursor.execute("SELECT memory FROM memories")
    rows = cursor.fetchall()

    return [row[0] for row in rows]


def save_memory(memory):
    cursor.execute(
        "INSERT INTO memories (memory) VALUES (?)",
        (memory,)
    )
    db.commit()


def delete_all_memories():
    cursor.execute("DELETE FROM memories")
    db.commit()


# --------------------------------------------------
# LOAD MEMORY
# --------------------------------------------------

memories = get_memories()

if memories:
    memory_text = "\n".join(
        f"- {memory}" for memory in memories
    )
else:
    memory_text = "No long-term memories yet."

SYSTEM_PROMPT_WITH_MEMORY = SYSTEM_PROMPT + f"""

LONG-TERM MEMORY:

These are facts that the user previously asked you to remember:

{memory_text}

Use these memories naturally when relevant.
Do not mention the memory system unless the user asks about it.
"""


# --------------------------------------------------
# CONVERSATION
# --------------------------------------------------

messages = [
    {
        "role": "system",
        "content": SYSTEM_PROMPT_WITH_MEMORY
    }
]


print("🐱 Neko is ready!")
print("Commands:")
print("  /memory  - show saved memories")
print("  /forget  - delete all memories")
print("  /exit    - quit")
print()


while True:

    user = input("You: ").strip()

    if not user:
        continue

    # ----------------------------------------------
    # COMMANDS
    # ----------------------------------------------

    if user.lower() == "/exit":
        print("Neko: Hmph. Leaving already?")
        break

    if user.lower() == "/memory":
        memories = get_memories()

        if not memories:
            print("Neko: I don't remember anything yet.")
        else:
            print("Neko remembers:")

            for memory in memories:
                print(" -", memory)

        print()
        continue

    if user.lower() == "/forget":
        delete_all_memories()

        print("Neko: Fine! I forgot everything. Hmph.")
        print()
        continue

    if user.lower().startswith("/remember "):
        memory = user[10:].strip()

        if memory:
            save_memory(memory)
            print("Neko: Hmph... fine. I'll remember that.")
        else:
            print("Neko: Remember WHAT, baka?")

        print()
        continue

    # ----------------------------------------------
    # NORMAL CHAT
    # ----------------------------------------------

    messages.append({
        "role": "user",
        "content": user
    })

    response = ollama.chat(
        model=MODEL,
        messages=messages
    )

    answer = response["message"]["content"]

    print("Neko:", answer)
    print()

    messages.append({
        "role": "assistant",
        "content": answer
    })