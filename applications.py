import subprocess


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

        return f"Opened {name}."

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
                    "Get-Process CalculatorApp -ErrorAction SilentlyContinue | Stop-Process"
                ],
                capture_output=True,
                text=True
            )

            return "Closed calculator."

        executable = APPLICATIONS[name]

        result = subprocess.run(
            ["taskkill", "/IM", executable, "/T"],
            capture_output=True,
            text=True
        )

        if result.returncode == 0:
            return f"Closed {name}."

        return f"{name} does not appear to be running."

    except Exception as e:
        return f"Failed to close {name}: {e}"
