"""Executable entry point for the ORBIT desktop application."""

import tkinter as tk

from core.bootstrap import build_app
from ui.app import OrbitWindow
from ui.controller import ChatController
from ui.launch import make_provider


def main() -> None:
    """Initialize and run the ORBIT desktop user interface."""
    # 1. Create the controller first
    controller = ChatController()

    # 2. Configure provider and startup mode
    provider, mode_label, notice = make_provider()

    # 3. Wire the core application components with bootstrap
    app = build_app(
        provider=provider,
        confirm_callback=controller.confirm_callback,
        on_event=controller.on_agent_event,
    )

    # 4. Attach app to controller
    controller.attach(app)

    # 5. Create Tk root and OrbitWindow, then run mainloop
    root = tk.Tk()
    OrbitWindow(root, controller, mode_label=mode_label, notice=notice)
    root.mainloop()


if __name__ == "__main__":
    main()
