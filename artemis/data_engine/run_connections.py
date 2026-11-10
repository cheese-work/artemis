from pathlib import Path

from artemis.data_engine import schema_revisions

REVISIONS = (("ALTER TABLE sessions ADD COLUMN connection_id TEXT",),)


def migrate(db_path: str | Path) -> schema_revisions.RevisionReport:
    return schema_revisions.apply(db_path, "run_connections", REVISIONS)
