from unittest.mock import AsyncMock

import pytest
from conftest import (
    CONFIG_PROBE,
    IDENTITY_HEADER,
    connect_anthropic_subscription,
    header_client,
    settings_client,
)
from druks.accounts.exceptions import AuthConfigurationError
from druks.accounts.models import Account
from druks.core.apis.slack import SlackClient
from druks.core.services import Slack
from druks.db import db_session
from druks.harnesses.config import get_config
from druks.harnesses.exceptions import HarnessNotConnectedError
from druks.secrets.datastructures import Audience
from druks.secrets.models import VaultSecret
from druks.testing import make_settings
from druks.user_settings.models import InstallationSettings, SettingsOverride
from druks.workflows import _run_instance
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize
from sqlalchemy.exc import IntegrityError


async def test_account_creation_copies_defaults_once(tmp_path, druks_db, monkeypatch):
    monkeypatch.setattr(
        "druks.accounts.models.get_settings",
        lambda: make_settings(tmp_path, timezone="Europe/Madrid"),
    )
    alice = await Account.get_or_create(druks_db, "alice@example.com")
    assert alice.timezone == "Europe/Madrid"
    assert alice.theme == "system"
    await alice.update_preferences(timezone="America/New_York")
    existing = await Account.get_or_create(druks_db, "alice@example.com")
    assert existing.id == alice.id
    assert existing.timezone == "America/New_York"
    with header_client(tmp_path) as client:
        assert client.get(
            "/api/settings/personal", headers={IDENTITY_HEADER: alice.username}
        ).json() == {
            "timezone": "America/New_York",
            "theme": "system",
            "notificationChannelId": None,
            "isTagRequired": False,
        }


class _SlackChannels(SlackClient):
    """The bot is a member of #ops and #alerts only."""

    async def users_conversations(self, **params):
        async def pages():
            yield {"channels": [{"id": "C3", "name": "ops"}]}
            yield {"channels": [{"id": "C1", "name": "alerts"}]}

        return pages()


@pytest.fixture
def slack_channels(monkeypatch):
    async def get_client():
        return _SlackChannels()

    monkeypatch.setattr(Slack, "get_client", get_client)


async def test_a_person_chooses_a_channel_the_bot_is_in(tmp_path, druks_db, slack_channels):
    with settings_client(tmp_path) as client:
        saved = client.patch("/api/settings/personal", json={"notificationChannelId": "C1"})
        assert saved.status_code == 200
        assert saved.json()["notificationChannelId"] == "C1"
        outside = client.patch("/api/settings/personal", json={"notificationChannelId": "C2"})
        assert outside.status_code == 422
        assert "Invite it to the channel" in outside.json()["detail"]
        assert client.get("/api/settings/personal").json()["notificationChannelId"] == "C1"
        direct = client.patch("/api/settings/personal", json={"notificationChannelId": None})
        assert direct.status_code == 200
        assert direct.json()["notificationChannelId"] is None


async def test_notification_channels_list_the_bot_s_channels_by_name(tmp_path, slack_channels):
    with settings_client(tmp_path) as client:
        listed = client.get("/api/notifications/channels")
    assert listed.json() == [{"id": "C1", "name": "alerts"}, {"id": "C3", "name": "ops"}]


async def test_the_database_refuses_duplicate_installation_settings(druks_db):
    await InstallationSettings.get_or_create(druks_db)
    with pytest.raises(IntegrityError):
        async with druks_db.begin_nested():
            druks_db.add(InstallationSettings(id=2))
            await druks_db.flush()


async def test_the_database_refuses_a_second_default_account(druks_db):
    first = await Account.get_or_create(druks_db, "alice@example.com")
    assert first.is_default
    with pytest.raises(IntegrityError):
        async with druks_db.begin_nested():
            druks_db.add(Account(username="bob@example.com", is_default=True))
            await druks_db.flush()


async def test_run_account_is_the_explicit_account_or_default(druks_db):
    default = await Account.get_or_create(druks_db, "alice@example.com")
    explicit = await Account.get_or_create(druks_db, "bob@example.com")

    assert (await Account.get_for_run(druks_db, account_id=None)).id == default.id
    assert (await Account.get_for_run(druks_db, account_id=explicit.id)).id == explicit.id


async def test_before_setup_execution_refuses_to_create_a_run(druks_db):
    note = await Note.create(body="before setup")
    with pytest.raises(AuthConfigurationError, match="Complete account setup"):
        await _run_instance(Summarize, subject=note.identity, input_={})


async def test_unattended_subscription_requires_a_connection(druks_db):
    assert not await Account.get_default(druks_db)
    with pytest.raises(
        HarnessNotConnectedError, match="connect your Anthropic subscription"
    ) as error:
        await get_config(druks_db, agent_name=CONFIG_PROBE.id, account_id=None)
    assert error.value.code == "not_connected"


async def test_unattended_api_key_uses_installation_config_without_a_default_account(druks_db):
    account = Account(username="alice@example.com")
    session = db_session()
    session.add(account)
    await session.flush()
    await VaultSecret.paste(
        druks_db,
        audience=Audience.provider("anthropic"),
        value="test-api-key",
        pasted_by=account,
    )
    installation = await InstallationSettings.get_or_create(druks_db)
    await installation.update(default_billing="api_key", default_effort="low")
    assert not await Account.get_default(druks_db)

    config = await get_config(druks_db, agent_name=CONFIG_PROBE.id, account_id=None)

    assert config.api_key.secrets["value"] == "test-api-key"
    assert not config.subscription
    assert config.effort == "low"


async def test_accounts_share_execution_defaults_and_keep_their_own_subscriptions(druks_db):
    alice = await connect_anthropic_subscription("alice@example.com")
    bob = await connect_anthropic_subscription("bob@example.com")
    installation = await InstallationSettings.get_or_create(druks_db)
    await installation.update(default_effort="low", default_timeout=600, fast_mode=True)

    for account_id, subscription in (
        (None, alice),
        (alice.account_id, alice),
        (bob.account_id, bob),
    ):
        config = await get_config(druks_db, agent_name=CONFIG_PROBE.id, account_id=account_id)
        assert config.subscription.id == subscription.id
        assert (config.effort, config.timeout, config.fast_mode) == ("low", 600, True)
    await SettingsOverride.set_agent_effort(druks_db, name=CONFIG_PROBE.id, value="high")
    for account_id in (None, alice.account_id, bob.account_id):
        config = await get_config(druks_db, agent_name=CONFIG_PROBE.id, account_id=account_id)
        assert config.effort == "high"


async def test_personal_api_is_scoped_and_does_not_retime_schedules(
    tmp_path, druks_db, monkeypatch
):
    apply_schedules = AsyncMock()
    monkeypatch.setattr("druks.user_settings.routes.apply_schedules", apply_schedules)
    with header_client(tmp_path) as client:
        alice = {IDENTITY_HEADER: "alice@example.com"}
        bob = {IDENTITY_HEADER: "bob@example.com"}
        inherited = client.get("/api/settings/personal", headers=alice).json()
        assert set(inherited) == {"timezone", "theme", "notificationChannelId", "isTagRequired"}
        assert inherited["theme"] == "system"
        saved_theme = client.patch("/api/settings/personal", headers=alice, json={"theme": "dark"})
        assert saved_theme.status_code == 200
        assert saved_theme.json()["theme"] == "dark"
        assert client.get("/api/settings/personal", headers=bob).json()["theme"] == "system"
        assert (await Account.get_for_username(druks_db, "alice@example.com")).theme == "dark"
        saved = client.patch(
            "/api/settings/personal", headers=alice, json={"timezone": "America/New_York"}
        )
        assert saved.status_code == 200
        account = await Account.get_for_username(druks_db, "alice@example.com")
        assert account.timezone == "America/New_York"
        assert client.get("/api/settings/personal", headers=bob).json()["timezone"] == "UTC"
        assert "timezone" not in client.get("/api/settings", headers=alice).json()
        apply_schedules.assert_not_awaited()
        assert (
            client.patch(
                "/api/settings/personal", headers=alice, json={"accountId": "someone-else"}
            ).status_code
            == 422
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("defaultHarness", "codex"),
        ("defaultModel", "openai/gpt-5.5"),
        ("defaultBilling", "api_key"),
        ("defaultEffort", "low"),
        ("fastMode", True),
        ("defaultTimeout", 600),
        ("timezone", "not-a-timezone"),
        ("theme", "sepia"),
    ],
)
async def test_invalid_edit_keeps_account_preferences(tmp_path, druks_db, field, value):
    with settings_client(tmp_path) as client:
        response = client.patch("/api/settings/personal", json={field: value})
        assert response.status_code == 422
        assert client.patch("/api/settings/personal", json={}).status_code == 200
    assert (await Account.get_default(druks_db)).timezone == "UTC"
    assert (await Account.get_default(druks_db)).theme == "system"


async def test_a_shared_agent_override_goes_to_the_installation_settings(tmp_path, druks_db):
    await Account.get_or_create(druks_db, "op@example.com")

    with settings_client(tmp_path) as client:
        response = client.patch(
            "/api/settings/apps", json={"instructionHarnesses": {CONFIG_PROBE.id: "claude"}}
        )

    assert response.status_code == 200
    assert await SettingsOverride.read(druks_db, f"agent_harness:{CONFIG_PROBE.id}") == "claude"
