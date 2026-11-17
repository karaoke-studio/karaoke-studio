"""Launch the shared YouTube login window, or run its local smoke check."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from krok_helper.video_download.youtube_login_window import main

if __name__ == "__main__":
    raise SystemExit(main())
