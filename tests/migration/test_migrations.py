"""Migration lifecycle: clean install, rollback, re-upgrade and drift."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = ROOT / "database" / "postgres" / "alembic.ini"


def alembic(*args: str, database_url: str) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "AIOBS_DATABASE__URL": database_url}
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), *args],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def table_names(path: Path) -> set[str]:
    import sqlite3

    with sqlite3.connect(path) as connection:
        return {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


@pytest.fixture
def database(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "migration.db"
    return path, f"sqlite+aiosqlite:///{path}"


class TestMigrationLifecycle:
    def test_clean_install_creates_every_table(self, database: tuple[Path, str]) -> None:
        path, url = database
        result = alembic("upgrade", "head", database_url=url)
        assert result.returncode == 0, result.stderr

        from aiobs_api.storage.postgres.models import Base

        created = table_names(path)
        expected = set(Base.metadata.tables) | {"alembic_version"}
        assert expected <= created, f"missing tables: {sorted(expected - created)}"

    def test_upgrade_is_idempotent(self, database: tuple[Path, str]) -> None:
        path, url = database
        alembic("upgrade", "head", database_url=url)
        before = table_names(path)
        result = alembic("upgrade", "head", database_url=url)
        assert result.returncode == 0, result.stderr
        assert table_names(path) == before

    def test_downgrade_then_upgrade_restores_the_schema(self, database: tuple[Path, str]) -> None:
        """Rollback must be exercised, not assumed: a downgrade nobody has run
        is a downgrade that does not work."""
        path, url = database
        alembic("upgrade", "head", database_url=url)
        installed = table_names(path)

        result = alembic("downgrade", "base", database_url=url)
        assert result.returncode == 0, result.stderr
        assert table_names(path) == {"alembic_version"}

        result = alembic("upgrade", "head", database_url=url)
        assert result.returncode == 0, result.stderr
        assert table_names(path) == installed

    def test_no_drift_between_models_and_migrations(self, database: tuple[Path, str]) -> None:
        """A model changed without a migration is caught here rather than in
        production."""
        _, url = database
        alembic("upgrade", "head", database_url=url)
        result = alembic("check", database_url=url)
        assert result.returncode == 0, (
            "models have drifted from the migrations; run `make migration MSG=...`\n"
            + result.stdout
            + result.stderr
        )

    def test_offline_sql_can_be_generated_for_review(self, database: tuple[Path, str]) -> None:
        """A DBA must be able to read the SQL before it runs in production."""
        _, url = database
        result = alembic("upgrade", "head", "--sql", database_url=url)
        assert result.returncode == 0, result.stderr
        assert result.stdout.count("CREATE TABLE") >= 25

    def test_the_recorded_revision_survives_the_connection_closing(
        self, database: tuple[Path, str]
    ) -> None:
        """Regression: without an explicit commit the version table update was
        rolled back, leaving a migrated schema recorded as un-migrated."""
        _, url = database
        alembic("upgrade", "head", database_url=url)
        result = alembic("current", database_url=url)
        assert "head" in result.stdout, result.stdout


class TestSeedCompatibility:
    def test_bootstrap_runs_against_a_freshly_migrated_database(
        self, database: tuple[Path, str], tmp_path: Path
    ) -> None:
        _, url = database
        alembic("upgrade", "head", database_url=url)

        environment = {
            **os.environ,
            "AIOBS_DATABASE__URL": url,
            "AIOBS_ANALYTICS__SQLITE_PATH": str(tmp_path / "analytics.db"),
            "AIOBS_OBJECTS__ROOT_PATH": str(tmp_path / "objects"),
        }
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "aiobs_api.cli",
                "bootstrap",
                "--email",
                "migration@test.invalid",
                "--password",
                "migration-test-password",
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "Bootstrap complete" in result.stdout
        assert "aiobs_test_" in result.stdout  # an API key was issued
