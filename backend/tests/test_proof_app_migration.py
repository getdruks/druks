from pathlib import Path

from alembic import command
from alembic.config import Config
from druks.testing import TEST_DATABASE_URL, init_db
from sqlalchemy import create_engine

_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "druks" / "alembic.ini"
_PACKAGE_ROOT = Path(__file__).resolve().parent / "druks-field_notes" / "druks_field_notes"
_VERSIONS = _PACKAGE_ROOT / "migrations" / "versions"


def _drop(connection) -> None:
    connection.exec_driver_sql(
        "DROP TABLE IF EXISTS field_notes_note, field_notes_repository, alembic_version_field_notes"
    )


def test_proof_migration_applies_under_its_own_version_table(request):
    """The proof app's shipped migrations run through the shared platform ``env.py``, as
    ``druks init-db`` runs the migrations of an installed app. They create the app's tables
    and record its head in ``alembic_version_field_notes``: its own history, not the shared
    table of core."""
    # The test manages its own DDL through an AUTOCOMMIT engine. It also drops its tables at
    # the end. conftest lists this module in ``_OWN_DATABASE_MODULES``, so that the rollback
    # isolation of the suite skips it.
    engine = create_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT")
    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("version_locations", str(_VERSIONS))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    config.attributes["version_table"] = "alembic_version_field_notes"
    with engine.connect() as connection:
        _drop(connection)
    try:
        command.upgrade(config, "head")
        with engine.connect() as connection:
            for table in ("field_notes_note", "field_notes_repository"):
                assert connection.exec_driver_sql(f"SELECT to_regclass('{table}')").scalar()
            head = connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version_field_notes"
            ).scalar()
            assert head == "field_notes_0003"
    finally:
        with engine.connect() as connection:
            _drop(connection)
        # field_notes is installed. Its tables are part of the schema that the rest of the
        # suite reads. Create them again for the tests that follow.
        init_db(engine)
        engine.dispose()
        request.getfixturevalue("_druks_engine").dispose()
