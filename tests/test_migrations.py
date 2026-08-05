"""Unit tests for forward-only migration discovery and replay behavior."""

import hashlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

import migrate


QUALITY_MIGRATIONS = [
    APP_DIR / "migrations" / "0006_quality_firewall_v1.sql",
    APP_DIR / "migrations" / "0007_quality_observation_normalization_history.sql",
]


class RecordingCursor:
    """Minimal cursor that records SQL submitted by the migration runner."""

    def __init__(self):
        self.statements = []

    def execute(self, statement, parameters=None):
        self.statements.append((str(statement), parameters))


class QualityFirewallMigrationTest(unittest.TestCase):
    """Cover applying 0006 to an empty sequence and skipping an applied file."""

    def test_quality_migration_is_applied_when_missing(self):
        cursor = RecordingCursor()
        with (
            patch.object(migrate, "get_applied_migrations", return_value={}),
            patch.object(migrate, "list_migration_files", return_value=QUALITY_MIGRATIONS),
        ):
            applied = migrate.apply_pending_migrations(cursor)

        self.assertEqual(applied, [path.name for path in QUALITY_MIGRATIONS])
        submitted_sql = "\n".join(statement for statement, _ in cursor.statements)
        self.assertIn("CREATE TABLE public.quality_observation", submitted_sql)
        self.assertIn("normalization_hash_version", submitted_sql)
        self.assertIn("INSERT INTO public.schema_migrations", submitted_sql)

    def test_quality_migration_is_not_reapplied_when_checksum_matches(self):
        cursor = RecordingCursor()
        checksums = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in QUALITY_MIGRATIONS
        }
        with (
            patch.object(
                migrate,
                "get_applied_migrations",
                return_value=checksums,
            ),
            patch.object(migrate, "list_migration_files", return_value=QUALITY_MIGRATIONS),
        ):
            applied = migrate.apply_pending_migrations(cursor)

        self.assertEqual(applied, [])
        self.assertEqual(cursor.statements, [])


if __name__ == "__main__":
    unittest.main()
