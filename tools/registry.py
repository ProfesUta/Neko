from tools.system import get_system_status
from tools.applications import open_application, close_application
from tools.files import list_files, search_file, open_path
from tools.browser import open_url, search_web

from memory.database import remember_fact, search_memories


AVAILABLE_FUNCTIONS = {
    "get_system_status": get_system_status,
    "open_application": open_application,
    "close_application": close_application,
    "list_files": list_files,
    "search_file": search_file,
    "open_path": open_path,
    "open_url": open_url,
    "search_web": search_web,
    "remember_fact": remember_fact,
    "search_memories": search_memories,
}


TOOLS = [
    get_system_status,
    open_application,
    close_application,
    list_files,
    search_file,
    open_path,
    open_url,
    search_web,
    remember_fact,
    search_memories,
]
