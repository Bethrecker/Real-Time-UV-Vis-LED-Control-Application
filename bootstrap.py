"""Project-local Tcl/Tk scripts and Matplotlib cache; no global settings."""
import os
from pathlib import Path


def configure():
    root = Path(__file__).resolve().parent
    for name, variable in [("tcl_library", "TCL_LIBRARY"), ("tk_library", "TK_LIBRARY")]:
        location = root / "runtime" / "tcl" / name
        if location.is_dir():
            os.environ[variable] = str(location)
    os.environ.setdefault("MPLCONFIGDIR", str(root / ".mpl-cache"))
