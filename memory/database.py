"""SQLite facts, using short-lived connections safe across worker threads."""

import sqlite3
from contextlib import contextmanager
from threading import RLock

from config import DATA_DIR

DATABASE = DATA_DIR / "memory.db"
db_lock = RLock()


@contextmanager
def _connection():
    with db_lock:
        DATABASE.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(DATABASE)
        try:
            with db:
                db.execute("""
                    CREATE TABLE IF NOT EXISTS memories (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        memory TEXT NOT NULL
                    )
                """)
                yield db
        finally:
            db.close()


def get_memories():
    with _connection() as db:
        rows = db.execute("SELECT memory FROM memories ORDER BY id").fetchall()
    return [row[0] for row in rows]


def save_memory(memory):
    memory = memory.strip()
    if not memory:
        return "No memory was provided."
    with _connection() as db:
        if db.execute(
            "SELECT 1 FROM memories WHERE LOWER(memory) = LOWER(?) LIMIT 1",
            (memory,),
        ).fetchone():
            return f"That memory already exists: {memory}"
        db.execute("INSERT INTO memories (memory) VALUES (?)", (memory,))
    return f"Remembered: {memory}"


def remember_fact(fact: str) -> str:
    """Save a stable fact, preference, identity, or relationship about the user."""
    return save_memory(fact)


def search_memories(query: str) -> str:
    """Search long-term memories for information related to a query."""
    query = query.strip()
    if not query:
        return "You didn't provide anything to search for."
    with _connection() as db:
        rows = db.execute(
            "SELECT memory FROM memories WHERE memory LIKE ? ORDER BY id LIMIT 20",
            (f"%{query}%",),
        ).fetchall()
    if not rows:
        return f"I couldn't find any memories related to: {query}"
    return (
        f"Memories related to '{query}':\n"
        + "\n".join(f"- {row[0]}" for row in rows)
    )


def delete_all_memories():
    with _connection() as db:
        db.execute("DELETE FROM memories")
