import psutil

from applications import open_application, close_application
from files import list_files, search_file, open_path
from browser import open_url, search_web


def get_system_status() -> str:
    """
    Get current CPU and RAM usage.
    """

    cpu = psutil.cpu_percent(interval=1)

    memory = psutil.virtual_memory()

    used_gb = memory.used / (1024 ** 3)
    total_gb = memory.total / (1024 ** 3)

    return (
        f"CPU usage: {cpu:.1f}%\n"
        f"RAM usage: {used_gb:.1f} GB / {total_gb:.1f} GB\n"
        f"RAM usage percentage: {memory.percent:.1f}%"
    )


AVAILABLE_FUNCTIONS = {
    "get_system_status": get_system_status,

    "open_application": open_application,
    "close_application": close_application,

    "list_files": list_files,
    "search_file": search_file,
    "open_path": open_path,

    "open_url": open_url,
    "search_web": search_web,
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
]