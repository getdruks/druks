import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from dbos import DBOS
from druks import tracing, workflows
from druks.accounts import jwt as assertion
from druks.accounts.models import Account
from druks.api.server import app
from druks.apps import App
from druks.database import create_engine_from_url
from druks.db import db_session
from druks.durable.dbos_state import DBOS_SYSTEM_SCHEMA, workflow_status
from druks.durable.enums import AgentCallStatus
from druks.harnesses.providers import AnthropicProvider
from druks.mcp.catalog import mcp_server_registry
from druks.models import Base
from druks.registry import _app_classes, _app_packages, app_registry, service_registry
from druks.sandbox.datastructures import AgentResult
from druks.secrets.datastructures import Audience
from druks.secrets.enums import SecretKind
from druks.secrets.models import VaultSecret
from druks.settings import Settings, Traces, configure_settings, get_settings
from druks.testing import (
    TEST_DATABASE_URL,
    configure_app_for_test,
    make_settings,
    seed_call,
    seed_run,
)
from druks.workflows import Workflow, current_workflow
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize
from fastapi.testclient import TestClient
from fastmcp.server.auth.providers.jwt import JWTVerifier
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.propagate import get_global_textmap, set_global_textmap
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.test.globals_test import reset_trace_globals
from sqlalchemy import update

# What a process propagates before tracing sets its own.
_TEXTMAP = get_global_textmap()


IDENTITY_HEADER = "X-ExeDev-Email"
MANAGER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
FORM_TEXT = (
    '[TextField(name="title", label="Title", is_required=True, placeholder="A short title"), '
    'TextAreaField(name="notes", label="Notes", help_text="Anything else to know."), '
    'NumberField(name="budget", label="Budget", minimum=0, maximum=100, step=5, value=10), '
    'SelectField(name="where", label="Where", options=[Option("Staging", value="staging"), '
    'Option("Production", value="production")], value="staging"), '
    'MultiSelectField(name="tags", label="Tags", options=[Option("Rack", value="rack"), '
    'Option("Shelf", value="shelf")], value=["rack"]), '
    'RadioField(name="size", label="Size", is_required=True, options=[Option("Small", '
    'value="s"), Option("Large", value="l")]), '
    'CheckboxField(name="notify", label="Notify the owner", value=True)]'
)
_MANAGER_JWKS = {
    "keys": [
        {
            **pyjwt.algorithms.RSAAlgorithm.to_jwk(MANAGER_KEY.public_key(), as_dict=True),
            "kid": "manager-1",
        }
    ]
}


class ConfigOutput(workflows.InstructionOutput):
    ok: bool


class Probes(App):
    name = "probes"
    config_probe = workflows.Instruction(prompt="probe.md", contract=ConfigOutput)


CONFIG_PROBE = Probes.config_probe


def settings_client(tmp_path: Path) -> TestClient:
    return TestClient(configure_app_for_test(settings=make_settings(tmp_path)))


def header_client(tmp_path: Path) -> TestClient:
    settings = make_settings(tmp_path, identity={"mode": "header", "header": IDENTITY_HEADER})
    return TestClient(configure_app_for_test(settings=settings, authenticated=False))


async def connect_anthropic_subscription(email: str) -> VaultSecret:
    return await connect_provider(
        AnthropicProvider, {"claudeAiOauth": {"accessToken": email}}, provider_email=email
    )


async def connect_service(slug: str, *, identity: dict, secrets: dict) -> VaultSecret:
    """Seed the vault row that a finished connect flow leaves for a service."""
    return await VaultSecret.store(
        db_session(),
        service_registry.get(slug).secret_kind,
        Audience.service(slug),
        identity=identity,
        secrets=secrets,
    )


async def install_service(
    slug: str,
    *,
    access_token: str,
    identity: dict,
    secrets: dict | None = None,
    scopes: tuple[str, ...] = (),
) -> VaultSecret:
    """Seed the rows that a finished install leaves: the service's row and its own grant,
    with the scopes that the install granted."""
    row = await connect_service(slug, identity=identity, secrets=secrets or {})
    await VaultSecret.connect(
        db_session(),
        Audience.service(slug),
        account_id=None,
        refresh_token="",
        scopes=list(scopes),
        secrets={"access_token": access_token},
    )
    return row


async def installation_key() -> VaultSecret:
    account = await Account.get_or_create(db_session(), "op@example.com")
    return await VaultSecret.paste(
        db_session(), Audience.provider("anthropic"), "test-key", pasted_by=account
    )


def capture_spans(settings: Settings) -> Callable[[], tuple[ReadableSpan, ...]]:
    """Trace into memory, through the privacy boundary of a real installation, until
    reset_tracing(). The reader returns the spans that a real installation sends out of the
    process."""
    exporter = InMemorySpanExporter()
    # The exporter in memory takes the place of the collector that the endpoint names.
    traces = Traces(endpoint="https://collector.example.com/v1/traces")
    with patch.object(tracing, "OTLPSpanExporter", return_value=exporter):
        tracing.init_tracing(settings.model_copy(update={"traces": traces}))

    def read() -> tuple[ReadableSpan, ...]:
        # The batch holds a span until a flush hands it to the exporter.
        tracing._provider.force_flush()
        return exporter.get_finished_spans()

    return read


def reset_tracing() -> None:
    """Leave tracing as a new process finds it."""
    tracing._provider.shutdown()
    HTTPXClientInstrumentor().uninstrument()
    tracing._provider = None
    reset_trace_globals()
    set_global_textmap(_TEXTMAP)


@pytest.fixture
def declared_services():
    # A Service subclass registers itself when Python defines it. A test declares its
    # services inside this fixture and leaves the registry as found.
    saved = dict(service_registry._items)
    yield
    service_registry._items.clear()
    service_registry._items.update(saved)


@pytest.fixture
def tmp_data_dir(tmp_path):
    # What a test writes under the data directory lands in its own tmp_path.
    configure_settings(get_settings().model_copy(update={"data_dir": tmp_path}))


@pytest.fixture
def registered_apps():
    # A test that calls load_installed_apps() leaves the registry as found.
    saved = dict(app_registry._items)
    yield
    app_registry._items.clear()
    app_registry._items.update(saved)


@pytest.fixture(autouse=True)
async def _redis(druks_redis):
    yield


@pytest.fixture(autouse=True)
def _no_durable_dispatch(request, monkeypatch):
    # Routes start durable work, and agent runs push a DBOS phase event. A test that
    # runs no DBOS engine gets a no-op for each of those calls. The *_durable tests run
    # the real engine.
    if "durable" in request.module.__name__:
        return

    async def _noop(*args, **kwargs):
        return ""

    async def _phase_noop(*args, **kwargs):
        pass

    async def _dbos_cancel(workflow_id: str) -> None:
        # DBOS's half of Run.cancel(). Without a launched engine, the real call raises. The
        # derived state needs the terminal status that the real call writes.
        await db_session().execute(
            update(workflow_status)
            .where(workflow_status.c.workflow_uuid == workflow_id)
            .values(status="CANCELLED")
        )

    # monkeypatch, not mock.patch: when a test patches one of these too, monkeypatch
    # undoes both in reverse order and puts the real attribute back.
    monkeypatch.setattr(Workflow, "start", classmethod(_noop))
    monkeypatch.setattr("druks.workflows.set_run_phase", _phase_noop)
    monkeypatch.setattr("dbos.DBOS.cancel_workflow_async", _dbos_cancel)
    monkeypatch.setattr("dbos.DBOS.start_workflow_async", _noop)
    monkeypatch.setattr("dbos.DebouncerClient.debounce_async", _noop)


@pytest.fixture(scope="session", autouse=True)
def _reset_test_database():
    # The repository suite owns its scratch database and starts from a clean
    # application and DBOS schema. The shipped fixtures never destroy schemas. This
    # runs before any test, so the shipped session fixture builds onto the fresh one.
    engine = create_engine_from_url(TEST_DATABASE_URL)
    with engine.connect() as connection:
        connection.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
        connection.exec_driver_sql(f"DROP SCHEMA IF EXISTS {DBOS_SYSTEM_SCHEMA} CASCADE")
        connection.commit()
    engine.dispose()
    yield


@pytest.fixture
def registry_state():
    # Catalog loads and test registrations change the MCP registry of the process. The
    # fixture restores it, so the entries of a test do not leak into the rest of the
    # suite.
    saved = dict(mcp_server_registry._items)
    yield
    mcp_server_registry._items.clear()
    mcp_server_registry._items.update(saved)


def snapshot_apps() -> Callable[[], None]:
    # An App class files itself, with what it declares, when Python defines it. A test
    # that defines one calls the returned function, and the apps are then as found.
    classes, packages = dict(_app_classes), dict(_app_packages)

    def restore() -> None:
        _app_classes.clear()
        _app_classes.update(classes)
        _app_packages.clear()
        _app_packages.update(packages)

    return restore


@pytest.fixture
def declared_apps():
    # A test that defines an App class inside this fixture leaves the apps as found.
    restore = snapshot_apps()
    yield
    restore()


# These modules manage their own engine and database, and commit for real. Each DBOS
# durable test has its own database, with worker connections that read across the
# commit. The alembic migration test has its own AUTOCOMMIT engine and drops the DDL
# that it makes. The rollback model hides their writes, or does not see them, so they
# opt out and reset themselves. Every other module gets transaction rollback, the
# durable unit tests that use the fixtures here included.
_OWN_DATABASE_MODULES = {
    "test_durable_sdk",
    "test_notifications_durable",
    "test_provider_subscription_persistence",
    "test_app_migrations",
    "test_proof_app_migration",
}


def pytest_collection_modifyitems(items):
    # A sync fixture cannot call getfixturevalue on the async druks_db. Each test
    # outside the own-database modules gets it in its fixture list, so the test
    # runs inside the rollback transaction.
    for item in items:
        module = item.module.__name__.rsplit(".", 1)[-1]
        if module in _OWN_DATABASE_MODULES:
            continue
        if not hasattr(item, "fixturenames") or "druks_db" in item.fixturenames:
            continue
        item.fixturenames.append("druks_db")


@pytest.fixture(autouse=True)
def _reset_app_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _no_druks_namespace_fetches(monkeypatch):
    """Answer every fetch from a ``.druks`` namespace with no file, as a 404 does.
    Without the fixture, ``render_prompt`` with a ``repo`` and the read of an app's config
    call GitHub, which needs App credentials that tests do not have. Prompts then use the
    bundled templates, and configs use their model defaults. A test of the override or
    config path patches ``fetch_file`` itself."""

    async def _none(**_kwargs):
        return None

    monkeypatch.setattr("druks.prompts.resolver.fetch_file", _none)
    monkeypatch.setattr("druks.apps.config.fetch_file", _none)


@pytest.fixture
def manager_keys(monkeypatch):
    assertion._verifier.cache_clear()

    async def fetch_jwks(self):
        return _MANAGER_JWKS

    monkeypatch.setattr(JWTVerifier, "_fetch_jwks", fetch_jwks)
    yield
    assertion._verifier.cache_clear()


def bind_ambient_session(session) -> None:
    db_session.registry.set(session)


def set_endpoint(endpoint: str) -> None:
    """Give the installation the address ``endpoint``."""
    settings = get_settings()
    urls = settings.urls.model_copy(update={"endpoint": endpoint})
    configure_settings(settings.model_copy(update={"urls": urls}))


def make_jwt(claims: dict) -> str:
    """An unsigned JWT that carries ``claims``. The providers read claims with no signature
    check."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


async def connect_provider(
    provider,
    payload: dict,
    *,
    provider_email: str = "op@example.com",
    refresh_token_expires_at: datetime | None = None,
):
    """Seed the vault row that a finished OAuth connect flow leaves for a subscription."""
    account = await Account.get_or_create(db_session(), provider_email)
    _, expires_at = provider._refresh_state(payload)
    return await VaultSecret.store(
        db_session(),
        SecretKind.SUBSCRIPTION,
        Audience.provider(provider.id),
        account_id=account.id,
        secrets=payload,
        identity={"email": provider_email},
        expires_at=expires_at,
        refresh_token_expires_at=refresh_token_expires_at,
    )


def make_agent_result(output, *, agent="agent", error=None, cost_usd=None, cost_metadata=None):
    # An AgentResult that a faked run_agent returns, so the agent call records and parses it.
    # The status follows the error, as in the sandbox: a failed result always says why.
    return AgentResult(
        output=output,
        run_id="run-test",
        sandbox_host_id="host-test",
        model="anthropic/claude-opus-4-7",
        agent=agent,
        status=AgentCallStatus.FAILED if error else AgentCallStatus.SUCCEEDED,
        started_at=datetime.now(UTC),
        cost_usd=cost_usd,
        cost_metadata=cost_metadata,
        error=error,
    )


async def finish_agent_run(call, *, status=None, last_error=None):
    # Mark a seeded AgentCall finished. In production, AgentCall.record builds the finished row.
    call.status = (status or AgentCallStatus.SUCCEEDED).value
    call.last_error = last_error
    call.finished_at = Base.utc_now()
    await db_session().flush()
    return call


async def make_test_note(body: str = "a note"):
    """The platform suite's subject. It belongs to the proof app, not to software_factory,
    because the behavior of the platform must hold for the rows of any app."""
    return await Note.create(body=body)


async def seed_note_run(session, *, note=None, state: str = "running", **kwargs):
    """A run on a note. The function seeds a note when the caller gives none."""
    subject = note or await make_test_note()
    return await seed_run(session, kind=Summarize.kind, subject=subject, state=state, **kwargs)


@pytest.fixture
def direct_steps(monkeypatch):
    """Run each DBOS step in place, and return the options of each step that ran. A run that
    parks sends its account no notification."""
    steps = []

    async def call_through(options, function, *args, **kwargs):
        steps.append(options)
        return await function(*args, **kwargs)

    monkeypatch.setattr(DBOS, "run_step_async", call_through)
    monkeypatch.setattr("druks.workflows._notify_account", AsyncMock())
    return steps


@pytest.fixture
async def summarize(druks_db, direct_steps):
    """The proof app's workflow, in the body of its run on a note."""
    note = await Note.create(body="A plan")
    run = await seed_run(druks_db, kind=Summarize.kind, subject=note)
    workflow = Summarize()
    workflow._workflow_id = run.id
    workflow._subject = note.identity
    workflow.subject = note
    token = current_workflow.set(workflow)
    yield workflow
    current_workflow.reset(token)


async def seed_note_agent_run(*, agent: str = "implement", model: str = "openai/gpt-5.5", **kwargs):
    """The one agent call of a run on a new note."""
    session = db_session()
    run = await seed_note_run(session, **kwargs)
    return await seed_call(session, run, agent, status="running", model=model)


class Answer(dict):
    """One page of a Slack answer, which iterates over itself and holds its data as the
    SDK's does."""

    @property
    def data(self) -> dict:
        return self

    def __aiter__(self):
        return self.pages()

    async def pages(self):
        yield self
