"""Portable launcher for the original dream-monitor web application."""
from pathlib import Path
import os
import shutil
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "vendor_deps"))
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

if __name__ == "__main__":
    os.chdir(ROOT)
    config = ROOT / "config.yaml"
    if not config.exists():
        shutil.copyfile(ROOT / "config.example.yaml", config)
    from gui_server import main
    raise SystemExit(main())
