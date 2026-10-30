"""Application allowlist configuration for ORBIT.

To allow ORBIT to launch or close additional applications on your system,
edit this file by hand and add your desired application mapping to ALLOWED_APPS.

How to add an app:
1. Add a friendly lowercase alias key to ALLOWED_APPS (e.g. "my_app").
2. Set "launch" to the exact command argument list used to open the application.
3. Set "process" to the executable name (e.g. "MyApp.exe") that close_application
   is allowed to terminate. If the application should only be opened and never
   closed by ORBIT (for example, system shells or protocol links), set "process" to None.
4. Set "windows" to the list of executable names used for window matching (e.g. ["MyApp.exe"]).
"""

from typing import Any, Optional, TypedDict


class AppConfig(TypedDict, total=False):
    launch: list[str]
    process: Optional[str]
    candidates: Optional[list[str]]
    windows: Optional[list[str]]


# Allowlist mapping friendly application names to their launch arguments, process names,
# and window matching executable names.
# Only applications explicitly registered here can be launched, closed, or controlled by ORBIT.
# The user edits this file by hand to customize allowed desktop applications.
# Note: "launch", "process" (for closing), and "windows" (for window matching) have separate lists.
ALLOWED_APPS: dict[str, dict[str, Any]] = {
    "notepad": {
        "launch": ["notepad.exe"],
        "process": "notepad.exe",
        "windows": ["notepad.exe"],
    },
    "calculator": {
        "launch": ["calc.exe"],
        # verify the name in Task Manager
        "process": "CalculatorApp.exe",
        # ApplicationFrameHost.exe is NOT used because it hosts many UWP apps; calculator uses CalculatorApp.exe only
        "windows": ["CalculatorApp.exe"],
    },
    "explorer": {
        "launch": ["explorer.exe"],
        # closing it kills the taskbar
        "process": None,
        "windows": ["explorer.exe"],
    },
    "chrome": {
        "launch": ["chrome.exe"],
        "process": "chrome.exe",
        "windows": ["chrome.exe"],
        "candidates": [
            r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
            r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe",
            r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
        ],
    },
    "vscode": {
        "launch": ["code"],
        "process": "Code.exe",
        "windows": ["Code.exe"],
    },
    "whatsapp": {
        "launch": ["explorer.exe", "whatsapp:"],
        # Store app, launched through its protocol link; fill in the process name after checking Task Manager
        "process": None,
        "windows": ["WhatsApp.exe", "WhatsApp.Root.exe"],
    },
}

# Allowlist of app keys permitted for typing actions (type_into_app).
# Only apps explicitly listed here can receive simulated keystrokes.
# Default allowlist is notepad and whatsapp; add e.g. "winword" if registered in ALLOWED_APPS.
TYPING_ALLOWLIST: tuple[str, ...] = ("notepad", "whatsapp")

# Default standard user subfolder names to search for files under the user profile and OneDrive.
# The user can edit this tuple by hand to add or remove default search folders (e.g. "Projects").
SEARCH_FOLDER_NAMES: tuple[str, ...] = (
    "Documents",
    "Downloads",
    "Desktop",
    "Pictures",
    "Videos",
    "Music",
)
