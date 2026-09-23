import sqlite3


DATABASE = "memory.db"


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