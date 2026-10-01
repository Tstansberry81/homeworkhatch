"""Former location of the startup step; kept so an older start command (`python -m app.boot`)
still works. Use `python boot.py`: importing from the app package loads the whole app first."""

import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parent.parent / "boot.py"), run_name="__main__")
