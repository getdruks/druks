import asyncio
import os

import psycopg
import pytest
from drukbox_sdk import SandboxAPI
from drukbox_sdk.exceptions import SandboxUnavailableError
from druks.accounts.models import Account
from druks.database import configure_session, get_session, session_scope
from druks.db import db_session
from druks.durable.engine import configure_engine, get_dbos_client, init_dbos, launch, shutdown
from druks.sandbox.models import SandboxIdentity, SecretRef
from druks.secrets.datastructures import Audience
from druks.secrets.enums import SecretKind
from druks.secrets.models import VaultSecret
from druks.settings import Settings, configure_settings, get_settings
from druks.testing import init_db
from sqlalchemy import NullPool, create_engine, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Session

# A vault change queues the refresh in its own transaction, and DBOS runs it after the
# commit. Only a launched engine on a database with real commits shows both halves.

PG_BASE = os.environ.get("DRUKS_TEST_PG", "postgresql://druks:druks@localhost:5432")
DB = "druks_sandbox_refresh_durable_test"
URL = f"{PG_BASE.replace('postgresql://', 'postgresql+psycopg://')}/{DB}"


def _pg_up() -> bool:
    try:
        psycopg.connect(f"{PG_BASE}/postgres", connect_timeout=2).close()
        return True
    except psycopg.Error:
        return False


pytestmark = [
    pytest.mark.skipif(not _pg_up(), reason="test Postgres not reachable"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest.fixture(scope="module", autouse=True)
async def engine():
    original_settings = get_settings()
    admin = psycopg.connect(f"{PG_BASE}/postgres", autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
    admin.execute(f"CREATE DATABASE {DB}")
    admin.close()

    schema_engine = create_engine(URL)
    init_db(schema_engine)
    with Session(schema_engine) as session:
        session.add(Account(username="op@example.com", is_default=True))
        session.commit()
    schema_engine.dispose()
    created = create_async_engine(URL, poolclass=NullPool)
    configure_engine(created)
    configure_session(created)
    configure_settings(Settings(database_url=URL))  # pyright: ignore[reportCallIssue]
    # An earlier module can leave a client that it built on its own database.
    get_dbos_client.cache_clear()
    init_dbos()
    await launch()
    try:
        yield created
    finally:
        shutdown()
        await created.dispose()
        configure_settings(original_settings)


async def test_a_changed_secret_is_refreshed_until_each_sandbox_holds_it(engine, monkeypatch):
    refreshed = []

    async def refresh_secret(self, host_id, name):
        refreshed.append(host_id)
        if len(refreshed) == 1:
            raise SandboxUnavailableError("the exchange did not answer")
        # The exchange of the box fetches from the issuer, which records its answer.
        async with session_scope(engine) as session:
            identity = await session.scalar(
                select(SandboxIdentity).where(SandboxIdentity.host_id == host_id)
            )
            ref = identity.get_secret_ref(name)
            await ref.record_fingerprint(ref.secret.get_fingerprint())

    monkeypatch.setattr(SandboxAPI, "refresh_secret", refresh_secret)
    session = get_session(engine)
    db_session.registry.set(session)
    try:
        account = await Account.get_default(session)
        subscription = await VaultSecret.store(
            session,
            SecretKind.SUBSCRIPTION,
            Audience.provider("anthropic"),
            account_id=account.id,
            secrets={"token": "old"},
        )
        for host_id in ("host-1", "host-2"):
            identity, _ = await SandboxIdentity.create(
                session,
                account_id=account.id,
                run_id=None,
                scoped_to="chat",
                secret_refs=[SecretRef(name="anthropic", secret_id=subscription.id)],
                secrets_hash="",
            )
            await identity.bind(host_id)

        await VaultSecret.store(
            session,
            SecretKind.SUBSCRIPTION,
            Audience.provider("anthropic"),
            account_id=account.id,
            secrets={"token": "new"},
        )
        await session.commit()
    finally:
        await db_session.remove()
        await session.close()

    # The first attempt asks both sandboxes. The second asks only the one that Drukbox did
    # not reach: the other came back to the issuer.
    deadline = asyncio.get_event_loop().time() + 30
    while len(refreshed) < 3 and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.1)
    assert len(refreshed) == 3 and refreshed[0] == refreshed[2] != refreshed[1], (
        f"The refreshes were {refreshed}."
    )
