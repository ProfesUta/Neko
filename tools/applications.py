import subprocess
import os
from pathlib import PureWindowsPath


APPLICATIONS = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "paint": "mspaint.exe",
    "explorer": "explorer.exe",
    "edge": "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
}


def open_application(name: str) -> str:
    """
    Open an approved application.
    """

    name = name.lower().strip()

    if name not in APPLICATIONS:
        available = ", ".join(APPLICATIONS.keys())

        return (
            f"I don't know how to open '{name}'. "
            f"Available applications: {available}"
        )

    try:
        subprocess.Popen(
            APPLICATIONS[name],
            shell=False
        )

        return f"Opened application: {name}"

    except Exception as e:
        return f"Failed to open {name}: {e}"


def close_application(name: str) -> str:
    """
    Close an approved application.
    """

    name = name.lower().strip()

    if name not in APPLICATIONS:
        available = ", ".join(APPLICATIONS.keys())

        return (
            f"I don't know how to close '{name}'. "
            f"Available applications: {available}"
        )

    try:
        if name == "calculator":
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "$p = Get-Process CalculatorApp -ErrorAction SilentlyContinue; "
                    "if (-not $p) { exit 1 }; $p | Stop-Process -ErrorAction Stop"
                ],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

            if result.returncode == 0:
                return "Closed application: calculator"
            return "calculator does not appear to be running, or could not be closed."

        executable = PureWindowsPath(APPLICATIONS[name]).name

        result = subprocess.run(
            ["taskkill", "/IM", executable, "/T"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

        if result.returncode == 0:
            return f"Closed application: {name}"

        return f"{name} does not appear to be running."

    except Exception as e:
        return f"Failed to close {name}: {e}"
