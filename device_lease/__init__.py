"""Shared, dependency-free lease transaction and identity (CHE-2099 / DLP-1)."""

from contextlib import contextmanager


def canonical_ids(aliases, resolve):
    """Resolve aliases once, omit unknowns and keep stable canonical order."""
    return list(dict.fromkeys(device_id for alias in aliases if (device_id := resolve(alias))))


@contextmanager
def write_transaction(conn, clock, *, refusals=()):
    """Sample time after SQLite's writer lock; interruptions roll back all changes.

    Only explicit domain refusals commit maintenance (expiry/fencing). Callers
    must finish refusal checks before mutating a grant. No external I/O here.
    """
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    commit = False
    try:
        try:
            yield conn, clock()
            commit = True
        except refusals:
            commit = True
            raise
    finally:
        conn.execute("COMMIT" if commit else "ROLLBACK")
