import sqlite3
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

DATA_DIR.mkdir(exist_ok=True)

DATABASE = DATA_DIR / "memory.db"



db = sqlite3.connect(DATABASE)
cursor = db.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory TEXT NOT NULL
)
""")

db.commit()


def get_memories():
    cursor.execute("SELECT memory FROM memories ORDER BY id")
    rows = cursor.fetchall()
    return [row[0] for row in rows]


def save_memory(memory):
    memory = memory.strip()

    if not memory:
        return "No memory was provided."

    cursor.execute(
        "SELECT 1 FROM memories WHERE LOWER(memory) = LOWER(?) LIMIT 1",
        (memory,)
    )

    if cursor.fetchone():
        return f"That memory already exists: {memory}"

    cursor.execute(
        "INSERT INTO memories (memory) VALUES (?)",
        (memory,)
    )

    db.commit()

    return f"Remembered: {memory}"


def remember_fact(fact: str) -> str:
    """
    Save a useful long-term fact about the user.

    This is intended for stable facts, preferences, identities,
    relationships, and other information that may be useful later.
    """

    fact = fact.strip()

    if not fact:
        return "No memory was provided."

    return save_memory(fact)


def search_memories(query: str) -> str:
    """
    Search long-term memories for information related to a query.
    """

    query = query.strip()

    if not query:
        return "You didn't provide anything to search for."

    cursor.execute(
        """
        SELECT memory
        FROM memories
        WHERE memory LIKE ?
        ORDER BY id
        LIMIT 20
        """,
        (f"%{query}%",)
    )

    rows = cursor.fetchall()

    if not rows:
        return f"I couldn't find any memories related to: {query}"

    results = [row[0] for row in rows]

    return (
        f"Memories related to '{query}':\n"
        + "\n".join(f"- {memory}" for memory in results)
    )


def delete_all_memories():
    cursor.execute("DELETE FROM memories")
    db.commit()
