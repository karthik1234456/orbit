"""Application allowlist configuration for ORBIT.

To allow ORBIT to launch or close additional applications on your system,
edit this file by hand and add your desired application mapping to ALLOWED_APPS.

How to add an app:
1. Add a friendly lowercase alias key to ALLOWED_APPS (e.g. "my_app").
2. Set "launch" to the exact command argument list used to open the application.
3. Set "process" to the executable name (e.g. "MyApp.exe") that close_application
   is allowed to terminate. If the application should only be opened and never
   closed by ORBIT (for example, system shells or protocol links), set "process" to None.
"""

from typing import Any, Optional, TypedDict


class AppConfig(TypedDict, total=False):
    launch: list[str]
    process: Optional[str]
    candidates: Optional[list[str]]


# Allowlist mapping friendly application names to their launch arguments and process names.
# Only applications explicitly registered here can be launched or closed by ORBIT.
# The user edits this file by hand to customize allowed desktop applications.
ALLOWED_APPS: dict[str, dict[str, Any]] = {
    "notepad": {
        "launch": ["notepad.exe"],
        "process": "notepad.exe",
    },
    "calculator": {
        "launch": ["calc.exe"],
        # verify the name in Task Manager
        "process": "CalculatorApp.exe",
    },
    "explorer": {
        "launch": ["explorer.exe"],
        # closing it kills the taskbar
        "process": None,
    },
    "chrome": {
        "launch": ["chrome.exe"],
        "process": "chrome.exe",
        "candidates": [
            r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
            r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe",
            r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
        ],
    },
    "vscode": {
        "launch": ["code"],
        "process": "Code.exe",
    },
    "whatsapp": {
        "launch": ["explorer.exe", "whatsapp:"],
        # Store app, launched through its protocol link; fill in the process name after checking Task Manager
        "process": None,
    },
}

