import os
from pathlib import Path


def list_files(folder: str) -> str:
    """
    List files and folders inside a directory.
    """

    folder = os.path.expanduser(folder)

    path = Path(folder)

    if not path.exists():
        return f"The folder '{folder}' does not exist."

    if not path.is_dir():
        return f"'{folder}' is not a folder."

    try:
        items = list(path.iterdir())

        if not items:
            return f"The folder '{folder}' is empty."

        result = [
            f"Contents of: {path.resolve()}"
        ]

        for item in items:
            if item.is_dir():
                result.append(f"[Folder] {item.name}")
            else:
                result.append(f"[File] {item.name}")

        return "\n".join(result)

    except Exception as e:
        return f"Failed to read the folder: {e}"


def search_file(filename: str, folder: str = ".") -> str:
    """
    Search for a file or folder by name.
    """

    folder = os.path.expanduser(folder)

    path = Path(folder)

    if not path.exists():
        return f"The folder '{folder}' does not exist."

    matches = []

    try:
        for item in path.rglob("*"):
            if item.name.lower() == filename.lower():
                matches.append(str(item))

                if len(matches) >= 20:
                    break

    except Exception as e:
        return f"Failed to search: {e}"

    if not matches:
        return f"I couldn't find '{filename}'."

    return (
        f"Search results for '{filename}':\n"
        + "\n".join(matches)
    )


def open_path(path: str) -> str:
    """
    Open a file or folder using Windows Explorer.
    """

    path = os.path.expanduser(path)

    if not os.path.exists(path):
        return f"I couldn't find '{path}'."

    try:
        os.startfile(path)

        return f"Opened path: {os.path.abspath(path)}"

    except Exception as e:
        return f"Failed to open '{path}': {e}"
