"""Shared in-memory state for beetDeek.

These module-level globals are shared across all request threads.
The application requires a single gunicorn worker to keep this state coherent.
"""

import threading

# Identification + cover preview state
# Cover previews are stored here too, keyed as f"cover_{album_id}"
identify_tasks: dict = {}
identify_lock = threading.Lock()

# Rescan state
rescan_lock = threading.Lock()
rescan_proc = None  # subprocess.Popen | None
rescan_snapshot = None  # {item_id: (title, artist, album_id, path)} | None
