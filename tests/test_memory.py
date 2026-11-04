from concurrent.futures import ThreadPoolExecutor

from memory import database


def test_concurrent_duplicate_saves_and_search():
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(database.save_memory, ["Likes Jazz", "likes jazz"] * 12))
    assert len(database.get_memories()) == 1
    assert "Jazz" in database.search_memories("jazz") or "jazz" in database.search_memories("jazz")
    assert "couldn't find" in database.search_memories("no such fact")


def test_empty_values_and_clear():
    assert database.save_memory(" ") == "No memory was provided."
    database.save_memory("A fact")
    database.delete_all_memories()
    assert database.get_memories() == []
