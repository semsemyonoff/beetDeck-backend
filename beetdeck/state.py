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
rescan_proc = None  # subprocess.Popen | None — current subprocess (internal, used by controller)
rescan_snapshot = None  # {item_id: (title, artist, album_id, path)} | None — pre-import snapshot
rescan_run_id = None  # str | None — identifier for the current/last run
rescan_log_path = None  # str | None — path to the current run log file
rescan_phase = "idle"  # idle | importing | updating | done | error
rescan_result = None  # dict | None — persisted finish-state (status, returncode, added, removed)
rescan_thread = None  # threading.Thread | None — controller thread (used in tests)

# Metadata batch write state
metadata_write_lock = threading.Lock()
