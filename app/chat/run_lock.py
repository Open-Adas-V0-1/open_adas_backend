"""In-process 'is a turn currently running for this session' tracker (T6b Step 3a).

Deliberately narrow interface (try_acquire/release) so it can be swapped for a
Postgres advisory lock later (multi-instance deployment) without touching the router.
Safe under asyncio's single-threaded cooperative model: try_acquire does a plain
check-and-add with no `await` in between, so no race is possible between concurrent
requests on the same event loop.

Deliberately NOT the source of truth for "awaiting input" -- that is derived fresh
from the checkpointer's own pending-interrupt state on every request (see
app/chat/turn.py), so there is exactly ONE source of truth for that question. This
module only ever answers "is a turn actively streaming right now".
"""
import uuid

_running: set[uuid.UUID] = set()


def try_acquire(session_id: uuid.UUID) -> bool:
    """True if this call just acquired the lock (caller MUST call release() when the
    turn ends, success or failure). False if another turn is already running for this
    session -- caller must not proceed.
    """
    if session_id in _running:
        return False
    _running.add(session_id)
    return True


def release(session_id: uuid.UUID) -> None:
    _running.discard(session_id)
