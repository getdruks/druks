import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from logging.config import dictConfig
from pathlib import Path

import pytest
from dbos._logger import init_logger
from druks import database
from druks.apps import loader
from druks.durable import engine as durable_engine
from druks.exceptions import SessionNotBoundError
from druks.harnesses.exceptions import HarnessNotConnectedError
from druks.settings import Settings, ensure_data_dirs, setup_logging
from druks.testing import TEST_DATABASE_URL, make_settings
from fastmcp.utilities.logging import configure_logging as configure_fastmcp_logging
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
from pydantic import ValidationError
from uvicorn.config import LOGGING_CONFIG

_SECRETS_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


async def test_a_request_reads_what_startup_learned(druks_client, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a request re-read what startup already knows")

    monkeypatch.setattr(Settings, "__init__", refuse)
    monkeypatch.setattr(loader, "entry_points", refuse)

    for path in ("/api/services", "/api/settings/apps", "/api/apps"):
        assert (await druks_client.get(path)).status_code == 200, path


def test_auth_defaults_to_none_and_blesses_no_header(tmp_path):
    settings = make_settings(tmp_path)
    assert settings.identity.mode == "none"
    assert not settings.identity.header


@pytest.mark.parametrize("header", ["", "   "])
def test_header_mode_requires_the_operator_to_name_the_header(tmp_path, header):
    with pytest.raises(ValidationError):
        make_settings(tmp_path, identity={"mode": "header", "header": header})


def test_jwt_mode_requires_its_verification_targets(tmp_path):
    complete = {
        "header": "X-Edge-Assertion",
        "jwks_url": "https://edge.example.com/jwks.json",
        "jwt_issuer": "https://edge.example.com",
        "jwt_audience": "druks",
    }
    settings = make_settings(tmp_path, identity={"mode": "jwt", **complete})
    assert settings.identity.jwt_identity_claim == "/email"
    # Each required field, blanked in turn, refuses jwt mode.
    for name in complete:
        with pytest.raises(ValidationError):
            make_settings(
                tmp_path,
                identity={"mode": "jwt", **complete, name: "  "},
            )
    with pytest.raises(ValidationError, match="jwt_identity_claim must be a JSON Pointer"):
        make_settings(tmp_path, identity={"mode": "jwt", **complete, "jwt_identity_claim": "email"})


@pytest.mark.parametrize("mode", ["header", "jwt"])
def test_the_pat_slot_cannot_be_the_identity_header(tmp_path, mode):
    # Authorization always parses bearer-first, so an assertion configured
    # there could never be read — a total lockout, refused at startup.
    with pytest.raises(ValidationError):
        make_settings(
            tmp_path,
            identity={
                "mode": mode,
                "header": "authorization",
                "jwks_url": "https://edge.example.com/jwks.json",
                "jwt_issuer": "https://edge.example.com",
                "jwt_audience": "druks",
            },
        )


def _write_secrets(tmp_path, monkeypatch, secrets):
    """Give Druks its secrets as files, and no secret in the environment."""
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    for name, value in {"secrets_key": _SECRETS_KEY, **secrets}.items():
        (secrets_dir / name).write_text(value)
    monkeypatch.setenv("DRUKS_SECRETS_DIR", str(secrets_dir))
    for variable in ("DRUKS_SECRETS_KEY", "DRUKS_DATABASE_URL", "DRUKS_REDIS_URL"):
        monkeypatch.delenv(variable)


def test_toml_sets_the_tables_and_the_secrets_directory_sets_the_secrets(tmp_path, monkeypatch):
    config_path = tmp_path / "druks.toml"
    config_path.write_text(
        """
timezone = "Europe/Madrid"
[identity]
mode = "header"
header = "X-Edge-Email"

[urls]
endpoint = "https://druks.example.com"
webhook_host = "hooks.example.com"

[sandbox]
service_url = "https://sandbox.example.com"
image = "sandbox:latest"
timeout = 180
""".strip()
        + "\n"
    )
    monkeypatch.setenv("DRUKS_CONFIG", str(config_path))
    _write_secrets(
        tmp_path,
        monkeypatch,
        {
            "database_url": "postgresql+psycopg://druks:database-password@db/druks",
            "redis_url": "redis://:redis-password@redis:6379/3",
            "sandbox.service_token": "sandbox-token",
        },
    )

    settings = Settings()

    assert settings.identity.mode == "header"
    assert settings.identity.header == "X-Edge-Email"
    assert settings.urls.endpoint == "https://druks.example.com"
    assert settings.urls.webhook_host == "hooks.example.com"
    assert settings.secrets_key.get_secret_value() == _SECRETS_KEY
    assert settings.sandbox.service_token.get_secret_value() == "sandbox-token"
    assert settings.redis_url.get_secret_value() == "redis://:redis-password@redis:6379/3"
    assert settings.sandbox.service_url == "https://sandbox.example.com"
    assert settings.sandbox.image == "sandbox:latest"
    assert settings.sandbox.timeout == 180.0
    assert settings.timezone == "Europe/Madrid"
    for secret in (_SECRETS_KEY, "database-password", "redis-password", "sandbox-token"):
        assert secret not in repr(settings)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('secrets_key = "key"', "secrets_key is a secret"),
        ('[sandbox]\nservice_token = "token"', "sandbox.service_token is a secret"),
        ('[traces]\nheaders = "x-api-key=key"', "traces.headers is a secret"),
        ('data_dir = "/home/op/druks-data"', "data_dir is not a druks.toml key"),
    ],
)
def test_druks_toml_refuses_a_secret_and_a_setting_of_the_environment(
    tmp_path, monkeypatch, body, message
):
    config_path = tmp_path / "druks.toml"
    config_path.write_text(body + "\n")
    monkeypatch.setenv("DRUKS_CONFIG", str(config_path))

    with pytest.raises(ValueError, match=message):
        Settings()


def test_the_environment_sets_a_secret_under_its_prefixed_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DRUKS_SANDBOX_SERVICE_TOKEN", "environment-token")
    monkeypatch.setenv("DRUKS_TIMEZONE", "Asia/Tokyo")
    # Drukbox reads these names, and they share the environment file of an install.
    monkeypatch.setenv("DATABASE_URL", "sqlite:///drukbox")
    monkeypatch.setenv("SECRETS_KEY", "drukbox-key")

    settings = Settings()

    assert settings.sandbox.service_token.get_secret_value() == "environment-token"
    assert settings.database_url.get_secret_value() == TEST_DATABASE_URL
    assert settings.timezone == "UTC"


def test_a_secret_in_the_environment_and_in_a_file_refuses_construction(tmp_path, monkeypatch):
    _write_secrets(tmp_path, monkeypatch, {})
    monkeypatch.setenv("DRUKS_SECRETS_KEY", _SECRETS_KEY)

    with pytest.raises(ValueError, match="secrets_key is set in the environment and in a secret"):
        Settings()


def test_only_an_explicit_issuer_url_changes_the_mint_base(tmp_path):
    public = {"endpoint": "https://druks.example.com", "webhook_host": "hooks.example.com"}
    assert make_settings(tmp_path, urls=public).sandbox.issuer_url == "http://127.0.0.1:8001"

    settings = make_settings(tmp_path, urls=public, sandbox={"issuer_url": "http://10.0.0.5:8001"})

    assert settings.sandbox.issuer_url == "http://10.0.0.5:8001"


@pytest.mark.parametrize(
    "mcp_url", ["druks.internal/mcp", "/mcp", "ftp://druks.internal/mcp", "http://:8000/mcp"]
)
def test_an_mcp_url_that_is_not_an_absolute_http_url_stops_startup(tmp_path, monkeypatch, mcp_url):
    config_path = tmp_path / "druks.toml"
    config_path.write_text(f'[urls]\nmcp_url = "{mcp_url}"\n')
    monkeypatch.setenv("DRUKS_CONFIG", str(config_path))

    with pytest.raises(ValidationError, match="urls.mcp_url is not an absolute"):
        Settings()


def test_auth_mode_environment_variable_is_ignored(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DRUKS_CONFIG", raising=False)
    monkeypatch.setenv("DRUKS_AUTH_MODE", "header")

    settings = Settings()

    assert settings.identity.mode == "none"


def test_blank_toml_value_uses_submodel_default(tmp_path, monkeypatch):
    config_path = tmp_path / "druks.toml"
    config_path.write_text('[identity]\njwt_identity_claim = ""\n')
    monkeypatch.setenv("DRUKS_CONFIG", str(config_path))

    settings = Settings()

    assert settings.identity.jwt_identity_claim == "/email"


def test_harness_config_root_expands_the_environment_path(tmp_path, monkeypatch):
    monkeypatch.setenv("DRUKS_HARNESS_CONFIG_ROOT", "~/harness-config")

    settings = make_settings(tmp_path)

    assert settings.harness_config_root == Path.home() / "harness-config"


def test_harness_config_root_defaults_to_the_druks_config_directory(tmp_path):
    settings = make_settings(tmp_path)

    assert settings.harness_config_root == Path.home() / ".config/druks/harnesses"


def test_missing_explicit_config_refuses_construction(tmp_path, monkeypatch):
    config_path = tmp_path / "missing.toml"
    monkeypatch.setenv("DRUKS_CONFIG", str(config_path))

    with pytest.raises(ValueError, match=re.escape(str(config_path))):
        Settings()


def test_ensure_data_dirs_provisions_skills_dir(tmp_path):
    # Startup creates skills_dir, or the first skill install's write raises OSError.
    skills_dir = tmp_path / "shared" / "skills"
    settings = make_settings(tmp_path, sandbox_skills_dir=skills_dir)
    ensure_data_dirs(settings)
    assert settings.skills_dir.is_dir()
    assert settings.files_dir.is_dir()


def test_installation_timezone_defaults_to_utc(tmp_path):
    assert make_settings(tmp_path).timezone == "UTC"


@pytest.mark.parametrize("timezone", ["Not/A/Zone", "/etc/passwd", "../UTC"])
def test_installation_timezone_rejects_invalid_zones(tmp_path, timezone):
    with pytest.raises(ValidationError, match="Unknown IANA timezone"):
        make_settings(tmp_path, timezone=timezone)


def test_development_example_pins_the_installation_timezone(tmp_path, monkeypatch):
    example = Path(__file__).resolve().parents[2] / "druks.toml.example"
    config = tmp_path / "druks.toml"
    config.write_text(example.read_text())
    monkeypatch.setenv("DRUKS_CONFIG", str(config))
    monkeypatch.setenv("DRUKS_TIMEZONE", "Asia/Tokyo")
    assert Settings().timezone == "UTC"


def test_pool_settings_reach_the_app_engine_and_dbos(tmp_path, monkeypatch):
    settings = make_settings(
        tmp_path, database_pool_size=3, database_max_overflow=4, dbos_pool_size=5
    )
    monkeypatch.setattr(database, "get_settings", lambda: settings)
    monkeypatch.setattr(durable_engine, "get_settings", lambda: settings)
    monkeypatch.setattr(durable_engine, "_initialized", False)
    configs = []
    monkeypatch.setattr(durable_engine, "DBOS", lambda config: configs.append(config))

    pool = database.create_async_engine_from_url(settings.database_url.get_secret_value()).pool
    durable_engine.init_dbos()

    assert (pool.size(), pool._max_overflow) == (3, 4)
    assert configs[0]["sys_db_pool_size"] == 5


def test_dbos_logging_hides_only_expected_workflow_failures(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(durable_engine, "get_settings", lambda: make_settings(tmp_path))
    monkeypatch.setattr(durable_engine, "_initialized", False)
    monkeypatch.setattr(durable_engine, "DBOS", lambda config: None)
    durable_engine.init_dbos()
    logger = logging.getLogger("dbos")

    with caplog.at_level(logging.ERROR, logger="dbos"):
        logger.error("workflow failed", exc_info=HarnessNotConnectedError("not connected"))
        logger.error("workflow failed", exc_info=SessionNotBoundError())

    assert [type(record.exc_info[1]) for record in caplog.records] == [SessionNotBoundError]


@contextmanager
def _logging_as_found() -> Iterator[None]:
    """Start from a process that has not set up logging, and leave logging as the test
    found it."""
    loggers = [
        logging.getLogger(name)
        for name in ("", "dbos", "fastmcp", "uvicorn", "uvicorn.error", "uvicorn.access")
    ]
    saved = [(logger.handlers, logger.propagate, logger.level) for logger in loggers]
    for logger in loggers:
        logger.handlers = []
        logger.setLevel(logging.NOTSET)
    try:
        yield
    finally:
        for logger, (handlers, propagate, level) in zip(loggers, saved, strict=True):
            logger.handlers = handlers
            logger.propagate = propagate
            logger.setLevel(level)


def test_json_log_format_writes_an_exception_in_a_span_as_one_line(tmp_path, capsys):
    context = SpanContext(
        trace_id=0xABC, span_id=0xDEF, is_remote=False, trace_flags=TraceFlags(TraceFlags.SAMPLED)
    )
    with _logging_as_found(), trace.use_span(NonRecordingSpan(context)):
        setup_logging(make_settings(tmp_path, log_level="INFO", log_format="json"))
        try:
            raise ValueError("boom")
        except ValueError:
            logging.getLogger("druks.probe").exception("failed %s", "here")

    [line] = capsys.readouterr().err.splitlines()
    record = json.loads(line)
    assert record.keys() == {
        "time",
        "level",
        "logger",
        "message",
        "exception",
        "trace_id",
        "span_id",
    }
    assert (record["level"], record["logger"], record["message"]) == (
        "ERROR",
        "druks.probe",
        "failed here",
    )
    assert record["exception"].startswith("Traceback (most recent call last):")
    assert record["exception"].endswith("ValueError: boom")
    assert record["trace_id"] == "00000000000000000000000000000abc"
    assert record["span_id"] == "0000000000000def"


def test_json_log_format_names_no_trace_outside_a_span(tmp_path, capsys):
    with _logging_as_found():
        setup_logging(make_settings(tmp_path, log_level="INFO", log_format="json"))
        logging.getLogger("druks.probe").info("hello")

    record = json.loads(capsys.readouterr().err)
    assert record.keys() == {"time", "level", "logger", "message", "exception"}
    assert record["exception"] is None


def test_json_log_format_names_no_trace_in_a_span_that_druks_does_not_send(tmp_path, capsys):
    # The sampler drops the request of a repeated dashboard read, whose span stays valid.
    context = SpanContext(trace_id=0xABC, span_id=0xDEF, is_remote=False)
    with _logging_as_found(), trace.use_span(NonRecordingSpan(context)):
        setup_logging(make_settings(tmp_path, log_level="INFO", log_format="json"))
        logging.getLogger("uvicorn.access").info("GET /api/runs 200")

    record = json.loads(capsys.readouterr().err)
    assert record.keys() == {"time", "level", "logger", "message", "exception"}


def test_json_log_format_takes_over_the_loggers_of_dbos_fastmcp_and_uvicorn(tmp_path, capsys):
    with _logging_as_found():
        # uvicorn configures its loggers before it imports the app, FastMCP when it is
        # imported, and DBOS adds its handler when it starts, after setup_logging.
        dictConfig(LOGGING_CONFIG)
        configure_fastmcp_logging()
        setup_logging(make_settings(tmp_path, log_level="INFO", log_format="json"))
        init_logger()
        logging.getLogger("dbos").info("workflow recovered")
        logging.getLogger("fastmcp.server.server").info("Error calling tool 'x'")
        logging.getLogger("uvicorn.error").info("Started server process [%d]", 7)
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", "/health", "1.1", 200
        )

    captured = capsys.readouterr()
    assert captured.out == ""
    records = [json.loads(line) for line in captured.err.splitlines()]
    assert [(record["logger"], record["message"]) for record in records] == [
        ("dbos", "workflow recovered"),
        ("fastmcp.server.server", "Error calling tool 'x'"),
        ("uvicorn.error", "Started server process [7]"),
        ("uvicorn.access", '127.0.0.1:5000 - "GET /health HTTP/1.1" 200'),
    ]
