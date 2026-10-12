from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
import time
import uuid


ACTIVE = frozenset({"queued", "running", "paused"})
TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})
TRANSITIONS = {
    "queued": frozenset({"running", *TERMINAL}),
    "running": frozenset({"paused", *TERMINAL}),
    "paused": frozenset({"running", *TERMINAL}),
    **{status: frozenset() for status in TERMINAL},
}


class ChatStore:
    def save_launch_config(self, identity, config):
        with self.transaction() as connection:
            root = self._root(connection, identity)
            connection.execute(
                "UPDATE chat_sessions SET launch_config=coalesce(launch_config, ?) WHERE session_id=?",
                (json.dumps(config), root),
            )

    def fail_admission(self, turn_id, reason):
        with self.transaction() as connection:
            self._transition(connection, turn_id, "failed")
            now = time.time()
            pending = connection.execute(
                "SELECT * FROM chat_messages WHERE turn_id=? AND acknowledged_at IS NULL AND role IN ('human','system')",
                (turn_id,),
            ).fetchall()
            for message in pending:
                connection.execute(
                    "INSERT INTO chat_messages(session_id, turn_id, role, content, reply_to, created_at) VALUES (?,?,?,?,?,?)",
                    (message["session_id"], turn_id, "agent", reason, message["sequence"], now),
                )
            connection.execute(
                "UPDATE chat_messages SET acknowledged_at=? WHERE turn_id=? AND acknowledged_at IS NULL AND role IN ('human','system')",
                (now, turn_id),
            )

    def launch_config(self, identity):
        with self.transaction() as connection:
            root = self._root(connection, identity)
            value = connection.execute(
                "SELECT launch_config FROM chat_sessions WHERE session_id=?", (root,)
            ).fetchone()[0]
            return json.loads(value) if value else None

    def next_turn(self, identity):
        with self.transaction() as connection:
            root = self._root(connection, identity)
            row = connection.execute(
                "SELECT * FROM chat_turns WHERE session_id=? AND status='queued' ORDER BY number LIMIT 1",
                (root,),
            ).fetchone()
            return dict(row) if row else None

    def finish(self, turn_id, status):
        with self.transaction() as connection:
            self._transition(connection, turn_id, status)
            pending = connection.execute(
                "SELECT * FROM chat_messages WHERE turn_id=? AND role='human' AND acknowledged_at IS NULL ORDER BY sequence",
                (turn_id,),
            ).fetchall()
            if pending:
                root = self._root(connection, turn_id)
                next_turn = connection.execute(
                    "SELECT turn_id FROM chat_turns WHERE session_id=? AND status='queued' ORDER BY number LIMIT 1",
                    (root,),
                ).fetchone()
                if next_turn:
                    next_id = next_turn["turn_id"]
                else:
                    number = connection.execute(
                        "SELECT max(number)+1 FROM chat_turns WHERE session_id=?", (root,)
                    ).fetchone()[0]
                    next_id = str(uuid.uuid4())
                    connection.execute(
                        "INSERT INTO chat_turns VALUES (?,?,?,?,?)",
                        (next_id, root, number, "queued", time.time()),
                    )
                connection.execute(
                    "UPDATE chat_messages SET turn_id=? WHERE turn_id=? AND role='human' AND acknowledged_at IS NULL",
                    (next_id, turn_id),
                )
            connection.execute(
                "UPDATE chat_messages SET acknowledged_at=? WHERE turn_id=? AND role='system' AND acknowledged_at IS NULL",
                (time.time(), turn_id),
            )

    def claim_worker(self, identity, pid, birth):
        from artemis.runtime.process_probe import pid_is_alive

        with self.transaction() as connection:
            root = self._root(connection, identity)
            row = connection.execute(
                "SELECT lease_pid, lease_birth FROM chat_sessions WHERE session_id=?", (root,)
            ).fetchone()
            if row["lease_pid"] and pid_is_alive(row["lease_pid"], row["lease_birth"]):
                raise ValueError("session_worker_active")
            connection.execute(
                "UPDATE chat_sessions SET lease_pid=?, lease_birth=?, idle_until=NULL WHERE session_id=?",
                (pid, birth, root),
            )

    def idle(self, identity, pid, deadline):
        with self.transaction() as connection:
            root = self._root(connection, identity)
            connection.execute(
                "UPDATE chat_sessions SET idle_until=? WHERE session_id=? AND lease_pid=?",
                (deadline, root, pid),
            )

    def release_worker(self, identity, pid, *, only_if_idle=False):
        with self.transaction() as connection:
            root = self._root(connection, identity)
            if (
                only_if_idle
                and connection.execute(
                    "SELECT 1 FROM chat_turns WHERE session_id=? AND status IN (?,?,?)",
                    (root, *sorted(ACTIVE)),
                ).fetchone()
            ):
                return False
            connection.execute(
                "UPDATE chat_sessions SET lease_pid=NULL, lease_birth=NULL, idle_until=NULL WHERE session_id=? AND lease_pid=?",
                (root, pid),
            )
            return True

    def turn(self, turn_id):
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM chat_turns WHERE turn_id=?", (turn_id,)
            ).fetchone()
            if row is None:
                raise KeyError(turn_id)
            return dict(row)

    @staticmethod
    def _transition(connection, turn_id, status):
        row = connection.execute(
            "SELECT status FROM chat_turns WHERE turn_id=?", (turn_id,)
        ).fetchone()
        if row is None:
            raise KeyError(turn_id)
        before = row["status"]
        if before != status and status not in TRANSITIONS.get(before, ()):
            raise ValueError(f"invalid_transition:{before}:{status}")
        connection.execute("UPDATE chat_turns SET status=? WHERE turn_id=?", (status, turn_id))

    def transition(self, turn_id, status):
        with self.transaction() as connection:
            self._transition(connection, turn_id, status)

    @staticmethod
    def _validate_message(content, client_id):
        if not content.strip() or len(content) > 32768 or not client_id or len(client_id) > 128:
            raise ValueError("invalid_message")

    def send(self, identity, content, client_id, *, kind="message"):
        self._validate_message(content, client_id)
        with self.transaction() as connection:
            root = self._root(connection, identity)
            previous = connection.execute(
                "SELECT * FROM chat_messages WHERE session_id=? AND client_id=?", (root, client_id)
            ).fetchone()
            if previous:
                if previous["content"] != content or previous["kind"] != kind:
                    raise ValueError("client_id_conflict")
                return dict(previous)
            turn = connection.execute(
                "SELECT * FROM chat_turns WHERE session_id=? AND status IN (?,?,?) ORDER BY number DESC LIMIT 1",
                (root, *sorted(ACTIVE)),
            ).fetchone()
            if turn is None:
                if kind != "message":
                    raise ValueError("no_active_turn")
                number = connection.execute(
                    "SELECT coalesce(max(number),0)+1 FROM chat_turns WHERE session_id=?", (root,)
                ).fetchone()[0]
                turn_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO chat_turns VALUES (?,?,?,?,?)",
                    (turn_id, root, number, "queued", time.time()),
                )
            else:
                turn_id = turn["turn_id"]
            cursor = connection.execute(
                "INSERT INTO chat_messages(session_id, turn_id, role, kind, content, client_id, created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    root,
                    turn_id,
                    "human" if kind == "message" else "system",
                    kind,
                    content,
                    client_id,
                    time.time(),
                ),
            )
            return dict(
                connection.execute(
                    "SELECT * FROM chat_messages WHERE sequence=?", (cursor.lastrowid,)
                ).fetchone()
            )

    def pending(self, turn_id):
        with self.transaction() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM chat_messages WHERE turn_id=? AND role IN ('human','system') AND acknowledged_at IS NULL ORDER BY sequence",
                    (turn_id,),
                )
            ]

    def acknowledge(self, sequence, reply, *, status=None):
        with self.transaction() as connection:
            message = connection.execute(
                "SELECT * FROM chat_messages WHERE sequence=?", (sequence,)
            ).fetchone()
            if message is None:
                raise KeyError(sequence)
            if message["acknowledged_at"] is not None:
                return
            if status is not None:
                self._transition(connection, message["turn_id"], status)
            now = time.time()
            connection.execute(
                "INSERT INTO chat_messages(session_id, turn_id, role, content, reply_to, created_at) VALUES (?,?,?,?,?,?)",
                (message["session_id"], message["turn_id"], "agent", reply, sequence, now),
            )
            connection.execute(
                "UPDATE chat_messages SET acknowledged_at=? WHERE sequence=?", (now, sequence)
            )

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.migrate()

    @contextmanager
    def transaction(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self):
        with self.transaction() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS chat_sessions (
                session_id TEXT PRIMARY KEY, created_at REAL NOT NULL,
                launch_config TEXT, lease_pid INTEGER, idle_until REAL)""")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(chat_sessions)")}
            if "lease_birth" not in columns:
                connection.execute("ALTER TABLE chat_sessions ADD COLUMN lease_birth REAL")
            connection.execute("""CREATE TABLE IF NOT EXISTS chat_turns (
                turn_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                number INTEGER NOT NULL, status TEXT NOT NULL,
                created_at REAL NOT NULL, UNIQUE(session_id, number))""")
            connection.execute("""CREATE TABLE IF NOT EXISTS chat_messages (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
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
                SELECT session_id, coalesce(start_time, 0) FROM sessions
                WHERE NOT EXISTS (SELECT 1 FROM chat_turns t WHERE t.turn_id=sessions.session_id)""")
            connection.execute("""INSERT OR IGNORE INTO chat_turns(turn_id, session_id, number, status, created_at)
                SELECT session_id, session_id, 1,
                    CASE WHEN status IN ('pending', 'starting') THEN 'queued' WHEN status='success' THEN 'completed' ELSE coalesce(status, 'queued') END,
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
