from config import SYSTEM_PROMPT
import memory
from chat import chat
from context import Context

context = Context()

# ==================================================
# MEMORY
# ==================================================

memories = memory.get_memories()


if memories:

    memory_text = "\n".join(
        f"- {item}" for item in memories
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
        "content": system_prompt
    }
]


# ==================================================
# START
# ==================================================

print("🐱 Neko is ready!")
print()

print("Commands:")
print("  /memory  - show saved memories")
print("  /remember <text> - save a memory")
print("  /forget  - delete all memories")
print("  /exit    - quit")
print()


# ==================================================
# MAIN LOOP
# ==================================================

while True:

    user = input("You: ").strip()


    if not user:
        continue


    # ----------------------------------------------
    # EXIT
    # ----------------------------------------------

    if user.lower() == "/exit":

        print("Neko: Hmph. Leaving already?")

        break


    # ----------------------------------------------
    # MEMORY
    # ----------------------------------------------

    if user.lower() == "/memory":

        memories = memory.get_memories()


        if not memories:

            print("Neko: I don't remember anything yet.")

        else:

            print("Neko remembers:")

            for item in memories:

                print(" -", item)

        print()

        continue


    # ----------------------------------------------
    # REMEMBER
    # ----------------------------------------------

    if user.lower().startswith("/remember "):

        new_memory = user[10:].strip()


        if new_memory:

            memory.save_memory(new_memory)

            print(
                "Neko: Hmph... fine. I'll remember that."
            )

        else:

            print(
                "Neko: Remember WHAT, baka?"
            )

        print()

        continue


    # ----------------------------------------------
    # FORGET
    # ----------------------------------------------

    if user.lower() == "/forget":

        memory.delete_all_memories()

        print(
            "Neko: Fine! I forgot everything. Hmph."
        )

        print()

        continue


    # ----------------------------------------------
    # CHAT
    # ----------------------------------------------

    messages.append({
        "role": "user",
        "content": user
    })


    answer = chat(messages, context)


    print("Neko:", answer)
    print()