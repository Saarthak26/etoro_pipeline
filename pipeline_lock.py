"""
pipeline_lock.py — Advisory file lock serializing all database-writing operations.

The scheduler daemon and the manual CLI commands (export / backfill / refresh /
sync-positions / screener-backfill / …) all write the same SQLite file. Running
two writers at once — e.g. a manual `export` while the scheduler fires — can
truncate or corrupt the database. This lock guarantees a single writer at a time:
whoever holds it wins; the other aborts (manual command) or skips to its next
scheduled run (scheduler job).

Implemented with fcntl.flock on a lock file next to the database. The kernel
releases the lock automatically when the holding process dies, so a crash or
kill can never leave a stale lock behind.

Do NOT nest acquisitions within a single process: each call opens a fresh file
descriptor, and flock treats two descriptors for the same file as conflicting
even inside one process. The pipeline acquires the lock once at the outermost
entry point (the main.py dispatcher, or one scheduler job) and never re-locks in
the functions it calls.
"""

import fcntl
import os
import logging
from contextlib import contextmanager

from config import DB_PATH

log = logging.getLogger(__name__)

LOCK_PATH = DB_PATH + ".lock"


class PipelineBusyError(RuntimeError):
    """Raised when the pipeline write-lock is already held by another process."""


@contextmanager
def pipeline_lock(label: str = "operation", wait: bool = False):
    """
    Hold the exclusive pipeline write-lock for the duration of the block.

    wait=False (default): raise PipelineBusyError immediately if another process
    already holds it. wait=True: block until it becomes free.
    """
    f = open(LOCK_PATH, "w")
    flags = fcntl.LOCK_EX if wait else (fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        try:
            fcntl.flock(f.fileno(), flags)
        except OSError as exc:
            raise PipelineBusyError(
                f"Another pipeline process is writing the database — '{label}' aborted "
                f"to avoid concurrent-writer corruption. Retry once it finishes."
            ) from exc
        try:
            f.write(f"{os.getpid()} {label}\n")
            f.flush()
        except OSError:
            pass
        log.debug("Acquired pipeline lock for '%s' (pid=%d)", label, os.getpid())
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()
