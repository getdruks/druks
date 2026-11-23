from pathlib import Path

import pytest
from druks.apps import loader
from druks.apps.exceptions import AppNotFound, SecretFileError
from druks.settings import Settings, configure_settings
from druks.testing import configure_app_for_test, make_settings
from druks_field_notes.app import FieldNotes
from fastapi.testclient import TestClient


@pytest.fixture
def secrets(tmp_path, monkeypatch):
    path = tmp_path / "secrets"
    path.mkdir()
    monkeypatch.setenv("DRUKS_SECRETS_DIR", str(path))
    return path


def _sync_token(client: TestClient) -> dict:
    apps = client.get("/api/settings/apps").json()["apps"]
    field_notes = next(app for app in apps if app["name"] == "field_notes")
    return next(field for field in field_notes["settings"] if field["name"] == "sync_token")


async def test_a_secret_file_sets_the_setting_until_it_is_removed(tmp_path: Path, secrets):
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        stored = client.patch(
            "/api/settings/apps",
            json={"appSettings": {"field_notes": {"sync_token": "sk-stored"}}},
        )
    (secrets / "apps.field_notes.sync_token").write_text("sk-from-file\n")
    settings = make_settings(tmp_path)

    with TestClient(configure_app_for_test(settings=settings)) as client:
        field = _sync_token(client)
        changed = client.patch(
            "/api/settings/apps",
            json={"appSettings": {"field_notes": {"sync_token": "sk-pasted"}}},
        )
        configured = (await FieldNotes.settings()).sync_token

    assert stored.status_code == 200
    assert "sk-from-file" not in repr(settings)
    assert (field["managed"], field["secretSet"], field["value"]) == (True, True, None)
    assert changed.status_code == 422
    assert "apps.field_notes.sync_token" in changed.json()["detail"]
    assert "sk-pasted" not in changed.text
    assert configured.get_secret_value() == "sk-from-file"

    (secrets / "apps.field_notes.sync_token").unlink()
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        field = _sync_token(client)
        resolved = (await FieldNotes.settings()).sync_token

    assert not field["managed"]
    assert resolved.get_secret_value() == "sk-stored"


@pytest.mark.parametrize(
    "file, error, message",
    [
        ("apps.unknown.token", AppNotFound, r"each file apps\.unknown\.\*"),
        ("apps.field_notes.unknown", SecretFileError, r"apps\.field_notes\.unknown"),
        ("apps.field_notes.notebook", SecretFileError, r"apps\.field_notes\.notebook"),
    ],
)
def test_a_secret_file_that_names_no_secret_setting_refuses_to_start(
    secrets, registered_apps, file, error, message
):
    (secrets / file).write_text("value")
    configure_settings(Settings())

    with pytest.raises(error, match=message):
        loader.load_installed_apps()


def test_a_secret_file_for_a_disabled_app_refuses_to_start(
    tmp_path, monkeypatch, secrets, registered_apps
):
    configuration = tmp_path / "druks.toml"
    configuration.write_text("[apps.field_notes]\nenabled = false\n")
    monkeypatch.setenv("DRUKS_CONFIG", str(configuration))
    (secrets / "apps.field_notes.sync_token").write_text("sk-from-file")
    configure_settings(Settings())

    with pytest.raises(SecretFileError, match=r"apps\.field_notes\.sync_token"):
        loader.load_installed_apps()


async def test_a_secret_file_is_the_text_it_holds(tmp_path: Path, secrets):
    (secrets / "apps.field_notes.sync_signing_key").write_text('{"type": "service_account"}\n')

    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))):
        configured = (await FieldNotes.settings()).sync_signing_key

    assert configured.get_secret_value() == '{"type": "service_account"}'


def test_a_secret_file_cannot_switch_an_app(secrets):
    (secrets / "apps.field_notes.enabled").write_text("false")

    with pytest.raises(ValueError, match=r"apps\.field_notes\.enabled"):
        Settings()
