from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import time
import uuid


ACTIVE = frozenset({"queued", "running", "paused"})
TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})


class ChatStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.migrate()

    @contextmanager
    def transaction(self):
        with closing(sqlite3.connect(self.path, timeout=30)) as connection:
            connection.row_factory = sqlite3.Row
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                yield connection

    def migrate(self):
        with self.transaction() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS chat_sessions (
                session_id TEXT PRIMARY KEY, created_at REAL NOT NULL,
                launch_config TEXT, lease_pid INTEGER, idle_until REAL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS chat_turns (
                turn_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                number INTEGER NOT NULL, status TEXT NOT NULL,
                created_at REAL NOT NULL, UNIQUE(session_id, number))""")
            connection.execute("""CREATE TABLE IF NOT EXISTS chat_messages (
                sequence INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('human', 'agent', 'system')),
                kind TEXT NOT NULL DEFAULT 'message', content TEXT NOT NULL,
                client_id TEXT, reply_to INTEGER UNIQUE,
                acknowledged_at REAL, created_at REAL NOT NULL,
                UNIQUE(session_id, client_id))""")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS chat_turn_session ON chat_turns(session_id, number)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS chat_message_session ON chat_messages(session_id, sequence)"
            )
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
            ).fetchone():
                return
            connection.execute("""INSERT OR IGNORE INTO chat_sessions(session_id, created_at)
                SELECT session_id, coalesce(start_time, 0) FROM sessions""")
            connection.execute("""INSERT OR IGNORE INTO chat_turns(turn_id, session_id, number, status, created_at)
                SELECT session_id, session_id, 1,
                    CASE WHEN status IN ('pending', 'starting') THEN 'queued' ELSE coalesce(status, 'queued') END,
                    coalesce(start_time, 0) FROM sessions
                WHERE NOT EXISTS (SELECT 1 FROM chat_turns t WHERE t.turn_id=sessions.session_id)""")
            connection.execute("""INSERT INTO chat_messages(session_id, turn_id, role, content, client_id, acknowledged_at, created_at)
                SELECT t.session_id, s.session_id, 'human', s.initial_goal, 'opening:' || s.session_id,
                    CASE WHEN t.status != 'queued' THEN coalesce(s.start_time, 0) END, coalesce(s.start_time, 0)
                FROM sessions s JOIN chat_turns t ON t.turn_id=s.session_id
                WHERE s.initial_goal IS NOT NULL AND NOT EXISTS
                    (SELECT 1 FROM chat_messages m WHERE m.turn_id=s.session_id)""")

    @staticmethod
    def _root(connection, identity):
        row = connection.execute(
            "SELECT session_id FROM chat_turns WHERE turn_id=?", (identity,)
        ).fetchone()
        if row is None:
            row = connection.execute(
                "SELECT session_id FROM chat_sessions WHERE session_id=?", (identity,)
            ).fetchone()
        if row is None:
            raise KeyError(identity)
        return row["session_id"]

    def get(self, identity):
        with self.transaction() as connection:
            root = self._root(connection, identity)
            session = dict(
                connection.execute(
                    "SELECT * FROM chat_sessions WHERE session_id=?", (root,)
                ).fetchone()
            )
            session.pop("launch_config")
            session["turns"] = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM chat_turns WHERE session_id=? ORDER BY number", (root,)
                )
            ]
            session["messages"] = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM chat_messages WHERE session_id=? ORDER BY sequence", (root,)
                )
            ]
            return session

    def new_turn(self, identity, content, client_id):
        with self.transaction() as connection:
            root = self._root(connection, identity)
            previous = connection.execute(
                """SELECT t.* FROM chat_turns t JOIN chat_messages m ON m.turn_id=t.turn_id
                WHERE m.session_id=? AND m.client_id=?""",
                (root, client_id),
            ).fetchone()
            if previous:
                return dict(previous)
            if connection.execute(
                "SELECT 1 FROM chat_turns WHERE session_id=? AND status IN (?,?,?)",
                (root, *sorted(ACTIVE)),
            ).fetchone():
                raise ValueError("active_turn")
            number = connection.execute(
                "SELECT coalesce(max(number), 0)+1 FROM chat_turns WHERE session_id=?", (root,)
            ).fetchone()[0]
            turn_id = str(uuid.uuid4())
            created_at = time.time()
            connection.execute(
                "INSERT INTO chat_turns VALUES (?,?,?,?,?)",
                (turn_id, root, number, "queued", created_at),
            )
            connection.execute(
                """INSERT INTO chat_messages(session_id, turn_id, role, content, client_id, created_at)
                VALUES (?,?,?,?,?,?)""",
                (root, turn_id, "human", content, client_id, created_at),
            )
            return dict(
                connection.execute(
                    "SELECT * FROM chat_turns WHERE turn_id=?", (turn_id,)
                ).fetchone()
            )


def migrate(path):
    ChatStore(path)
