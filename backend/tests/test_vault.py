from datetime import UTC, datetime, timedelta

import pytest
from druks.accounts.models import Account
from druks.db import db_session
from druks.mcp import oauth
from druks.registry import service_registry
from druks.sandbox.constants import REFRESH_SANDBOX_SECRETS_WORKFLOW
from druks.secrets.datastructures import Audience
from druks.secrets.enums import SecretKind
from druks.secrets.exceptions import SecretRevokedError
from druks.secrets.models import VaultSecret
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy_encrypted_field import SecretDecryptError, utils


async def _static(audience: str = "mcp:linear", **fields) -> VaultSecret:
    row = VaultSecret(
        kind=SecretKind.STATIC,
        audience=audience,
        header="Authorization",
        secrets={"value": "lin_secret"},
        **fields,
    )
    db_session().add(row)
    await db_session().flush()
    return row


async def test_a_pasted_value_issues_itself_with_no_expiry(druks_db):
    row = await _static()

    assert await row.issue_token(()) == ("lin_secret", None)
    assert (
        await VaultSecret.lookup(
            db_session(), SecretKind.STATIC, "mcp:linear", header="Authorization"
        )
        is row
    )


async def test_one_row_per_audience_account_and_header_except_an_oauth_connection(druks_db):
    await _static()
    with pytest.raises(IntegrityError):
        await _static()
    await db_session().rollback()

    account = await Account.get_or_create(druks_db, "op@example.com")
    for _ in range(2):
        db_session().add(
            VaultSecret(
                kind=SecretKind.OAUTH,
                audience="gmail",
                account_id=account.id,
                secrets={"refresh_token": "rt"},
            )
        )
    await db_session().flush()

    assert len(await VaultSecret.list_connections(db_session(), "gmail")) == 2


async def test_a_revoked_secret_keeps_its_facts_and_loses_its_secrets(druks_db):
    row = await _static(identity={"slug": "linear"})

    await row.revoke("user")
    await row.revoke("server_removed")

    assert row.revoked_at and row.revoked_reason == "user"
    assert dict(row.secrets) == {}
    assert row.identity == {"slug": "linear"}
    assert not (await db_session().get(VaultSecret, row.id)).is_live
    assert not await VaultSecret.lookup(
        db_session(), SecretKind.STATIC, "mcp:linear", header="Authorization"
    )


async def test_revoking_tokens_spares_a_sibling_audience(druks_db):
    installation = await _static("resource:github/installation/7")
    repos = await _static("resource:github/installation/7/widgets")
    sibling = await _static("resource:github/installation/71")

    await VaultSecret.revoke_tokens(db_session(), "resource:github/installation/7")
    for row in (installation, repos, sibling):
        await db_session().refresh(row)

    assert installation.revoked_at and repos.revoked_at
    assert sibling.is_live


def _record_refreshes(monkeypatch) -> list[tuple[str, str]]:
    """The refreshes the vault queues, as the workflow and the row it names."""
    queued = []

    async def enqueue(session, options, secret_id):
        queued.append((options["workflow_name"], secret_id))

    monkeypatch.setattr("druks.durable.engine.enqueue_in_transaction", enqueue)
    return queued


async def test_a_change_to_a_stored_row_refreshes_its_sandbox_secrets(druks_db, monkeypatch):
    queued = _record_refreshes(monkeypatch)
    pasted = await VaultSecret.store(
        db_session(), SecretKind.STATIC, "mcp:linear", secrets={"value": "one"}
    )
    connection = await VaultSecret.connect(
        db_session(), "service:acme", account_id=None, refresh_token="rt", scopes=[]
    )
    # A new row is in no sandbox yet.
    assert queued == []

    for _ in range(2):
        # The second store keeps the value, so no sandbox holds an old one.
        await VaultSecret.store(
            db_session(), SecretKind.STATIC, "mcp:linear", secrets={"value": "two"}
        )
    await connection.reconnect(refresh_token="rt2", scopes=[], identity={})
    await pasted.update_secrets({"value": "three"}, expires_at=None)
    await pasted.revoke("user")
    await pasted.revoke("server_removed")

    assert queued == [
        (REFRESH_SANDBOX_SECRETS_WORKFLOW, pasted.id),
        (REFRESH_SANDBOX_SECRETS_WORKFLOW, connection.id),
        (REFRESH_SANDBOX_SECRETS_WORKFLOW, pasted.id),
        (REFRESH_SANDBOX_SECRETS_WORKFLOW, pasted.id),
    ]


async def test_a_write_queues_its_refresh_before_it_changes_the_row(druks_db, monkeypatch):
    row = await _static()
    was_live = []

    async def enqueue(session, options, secret_id):
        was_live.append(row.is_live)

    monkeypatch.setattr("druks.durable.engine.enqueue_in_transaction", enqueue)

    await row.revoke("user")

    # A session that commits each operation on its own then commits the two together.
    assert was_live == [True]


async def test_a_fingerprint_follows_the_secrets_of_its_own_row(druks_db):
    row = await _static("mcp:linear")
    twin = await _static("mcp:notion")
    fingerprint = row.get_fingerprint()

    # The same secrets in another row have another fingerprint.
    assert fingerprint != twin.get_fingerprint()
    assert "lin_secret" not in fingerprint
    row.identity = {"slug": "linear"}
    assert row.get_fingerprint() == fingerprint
    await row.revoke("user")
    assert row.get_fingerprint() != fingerprint


async def test_a_minted_token_refreshes_no_sandbox_secret(druks_db, monkeypatch):
    queued = _record_refreshes(monkeypatch)

    for audience in (Audience.oauth("acme", "c1"), Audience.resource("github", "installation", 7)):
        for value in ("one", "two"):
            minted = await VaultSecret.store(
                db_session(), SecretKind.STATIC, audience, secrets={"value": value}
            )
        await minted.revoke()

    assert queued == []


async def test_the_secrets_column_is_ciphertext_bound_to_the_vault(druks_db):
    row = await _static()

    stored = await db_session().scalar(
        text("SELECT secrets FROM vault WHERE id = :id"), {"id": row.id}
    )

    assert b"lin_secret" not in bytes(stored)
    assert b"lin_secret" in utils.decrypt(bytes(stored), "vault.secrets")
    with pytest.raises(SecretDecryptError):
        utils.decrypt(bytes(stored), "another_table.secrets")


@pytest.mark.parametrize("kind", list(SecretKind))
async def test_a_revoked_secret_issues_nothing(druks_db, kind):
    row = VaultSecret(kind=kind, audience="mcp:linear", secrets={"value": "x"})
    db_session().add(row)
    await db_session().flush()
    await row.revoke("user")

    with pytest.raises(SecretRevokedError, match="mcp:linear"):
        await row.issue_token(())


async def test_a_service_grant_issues_through_the_services_client(druks_db, monkeypatch):
    row = await VaultSecret.connect(
        db_session(),
        "service:acme",
        account_id=None,
        refresh_token="rt",
        scopes=[],
    )
    expiry = datetime.now(UTC) + timedelta(hours=1)

    class Client:
        async def get_access_token(self, session, *, connection):
            assert connection is row
            return "tok", expiry

    class Acme:
        @staticmethod
        async def get_oauth_client():
            return Client()

    monkeypatch.setattr(service_registry, "get", lambda slug: Acme if slug == "acme" else None)

    assert await row.issue_token(()) == ("tok", expiry)


async def test_an_mcp_grant_issues_through_the_servers_client(druks_db, monkeypatch):
    row = await VaultSecret.connect(
        db_session(),
        "mcp:linear",
        account_id=None,
        refresh_token="rt",
        scopes=[],
    )

    async def get_access_token(session, name, account_id):
        assert (name, account_id) == ("linear", None)
        return "tok", None

    monkeypatch.setattr(oauth, "get_access_token", get_access_token)

    assert await row.issue_token(()) == ("tok", None)
