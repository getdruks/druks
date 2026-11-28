from pathlib import Path

import pytest
from conftest import connect_provider, connect_service
from druks.accounts.models import Account
from druks.durable.exceptions import AgentCallNotFound
from druks.durable.reads import get_agent_call_files
from druks.harnesses.providers import AnthropicProvider
from druks.sandbox.models import SandboxIdentity, SecretIssue, SecretRef
from druks.testing import configure_app_for_test, make_settings, seed_call, seed_run
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, druks_db):
    # druks_db seeds the database that create_app opens. A plain app is thus enough.
    # Each route handler opens its own Session on the same engine and reads the
    # seeded rows. AgentCall.artifact_dir comes from get_settings().artifacts_dir.
    # The settings set it to tmp_path, so that the property and the test read run
    # files in the same place.
    settings = make_settings(tmp_path)
    app = configure_app_for_test(settings=settings)
    with TestClient(app) as client:
        yield client


async def _seed_call_id(
    druks_db,
    *,
    finished: bool = True,
    run_state: str = "running",
) -> str:
    note = await Note.create(body="agent transcript")
    run = await seed_run(druks_db, kind=Summarize.kind, subject=note, state=run_state)
    status = "succeeded" if finished else "running"
    call = await seed_call(druks_db, run, "summarize", status=status)

    # The harness writes every file for a call into run-<run_id>/<call_id>/.
    call_dir = call.call_dir
    call_dir.mkdir(parents=True, exist_ok=True)
    (call_dir / "output.json").write_text('{"ok":true}')
    (call_dir / "metadata.json").write_text('{"pid": 1}')
    (call_dir / "stdout.jsonl").write_bytes(b"hello stdout output")
    (call_dir / "stderr.log").write_bytes(b"warning text")

    return call.id


async def test_list_files_inventories_call_artifacts(
    client: TestClient,
    druks_db,
):
    call_id = await _seed_call_id(druks_db)

    response = client.get(f"/api/field_notes/transcripts/{call_id}/files")

    assert response.status_code == 200
    files = response.json()
    # The slot that a file takes is its role. Each slot carries the file name that the
    # client puts into the download URL.
    assert files["stdout"]["name"] == "stdout.jsonl"
    assert files["stderr"]["name"] == "stderr.log"
    assert files["response"]["name"] == "output.json"
    assert files["metadata"]


async def test_get_agent_call_files_raises_for_unknown_call(druks_db):
    with pytest.raises(AgentCallNotFound):
        await get_agent_call_files(druks_db, "missing")


def test_transcript_unknown_call_returns_unified_404(client: TestClient, druks_db):
    response = client.get(
        "/api/field_notes/transcripts/missing",
        params={"stream": "stdout"},
    )

    assert response.status_code == 404
    assert response.json() == {
        "code": "AGENT_CALL_NOT_FOUND",
        "message": "No agent call missing.",
        "retryable": False,
    }


def test_file_listing_unknown_call_returns_unified_404(client: TestClient, druks_db):
    response = client.get("/api/field_notes/transcripts/missing/files")

    assert response.status_code == 404
    assert response.json() == {
        "code": "AGENT_CALL_NOT_FOUND",
        "message": "No agent call missing.",
        "retryable": False,
    }


def test_file_download_unknown_call_returns_unified_404(client: TestClient, druks_db):
    response = client.get("/api/field_notes/transcripts/missing/files/output.json")

    assert response.status_code == 404
    assert response.json() == {
        "code": "AGENT_CALL_NOT_FOUND",
        "message": "No agent call missing.",
        "retryable": False,
    }


async def test_a_call_under_another_apps_address_reads_as_unknown(
    client: TestClient,
    druks_db,
):
    call_id = await _seed_call_id(druks_db)

    for path in ("", "/stream", "/files", "/files/output.json"):
        response = client.get(
            f"/api/software_factory/transcripts/{call_id}{path}", params={"stream": "stdout"}
        )
        unknown = client.get(
            f"/api/software_factory/transcripts/missing{path}", params={"stream": "stdout"}
        )
        assert response.status_code == 404
        assert response.json()["code"] == unknown.json()["code"]


async def test_transcript_range_fetch_paginates(
    client: TestClient,
    druks_db,
):
    call_id = await _seed_call_id(druks_db)

    first = client.get(
        f"/api/field_notes/transcripts/{call_id}",
        params={"stream": "stdout", "limit": 5},
    )
    assert first.status_code == 200
    assert first.headers["cache-control"] == "private, max-age=31536000, immutable"
    data = first.json()
    assert data["offset"] == 0
    assert data["nextOffset"] == 5
    assert data["eof"] is False
    assert data["text"] == "hello"

    second = client.get(
        f"/api/field_notes/transcripts/{call_id}",
        params={"stream": "stdout", "offset": 5, "limit": 1024},
    )
    assert second.status_code == 200
    data = second.json()
    assert data["text"] == " stdout output"
    assert data["eof"] is True


async def test_transcript_of_a_running_call_is_never_cached(
    client: TestClient,
    druks_db,
):
    call_id = await _seed_call_id(druks_db, finished=False)

    response = client.get(
        f"/api/field_notes/transcripts/{call_id}",
        params={"stream": "stdout", "limit": 5},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


async def test_transcript_of_an_abandoned_call_is_cached_immutably(
    client: TestClient,
    druks_db,
):
    # The call never wrote finished_at. But its run failed. Nothing thus appends to that
    # log again. The chunk is as permanent as the chunk of a finished call.
    call_id = await _seed_call_id(druks_db, finished=False, run_state="failed")

    response = client.get(
        f"/api/field_notes/transcripts/{call_id}",
        params={"stream": "stdout", "limit": 5},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, max-age=31536000, immutable"


async def test_transcript_missing_file_returns_eof(
    client: TestClient,
    druks_db,
):
    note = await Note.create(body="missing transcript")
    run = await seed_run(druks_db, kind=Summarize.kind, subject=note)
    call = await seed_call(druks_db, run, "summarize", status="running")

    response = client.get(
        f"/api/field_notes/transcripts/{call.id}",
        params={"stream": "stdout"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["eof"] is True
    assert data["text"] == ""


async def test_transcript_stream_emits_chunk_then_finishes(
    client: TestClient,
    druks_db,
):
    # The seeded run is terminal. It streams its stdout in one tick: a transcript.chunk
    # that carries the log, then agent_call.finished, which ends the SSE.
    call_id = await _seed_call_id(druks_db)

    response = client.get(
        f"/api/field_notes/transcripts/{call_id}/stream",
        params={"stream": "stdout"},
    )

    assert response.status_code == 200
    body = response.text
    assert "event: transcript.chunk" in body
    assert "hello stdout output" in body
    assert "event: agent_call.finished" in body


async def test_transcript_stream_rejects_a_negative_offset(
    client: TestClient,
    druks_db,
):
    call_id = await _seed_call_id(druks_db)

    response = client.get(
        f"/api/field_notes/transcripts/{call_id}/stream",
        params={"stream": "stdout", "offset": -1},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "offset -1 is less than 0. Pass an offset of 0 or more."


def test_transcript_stream_unknown_call_returns_unified_404(client: TestClient, druks_db):
    response = client.get(
        "/api/field_notes/transcripts/no-such-call/stream",
        params={"stream": "stdout"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "AGENT_CALL_NOT_FOUND"


async def test_get_file_serves_inventory_paths(
    client: TestClient,
    druks_db,
):
    call_id = await _seed_call_id(druks_db)

    files = client.get(f"/api/field_notes/transcripts/{call_id}/files").json()
    # Build the download URL as the client does: the route of the file list, then the
    # file's name. The wire carries only names.
    name = files["response"]["name"]

    response = client.get(f"/api/field_notes/transcripts/{call_id}/files/{name}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private"
    assert response.json() == {"ok": True}


async def test_get_file_rejects_path_traversal(
    client: TestClient,
    druks_db,
):
    call_id = await _seed_call_id(druks_db)

    response = client.get(
        f"/api/field_notes/transcripts/{call_id}/files/..%2F..%2Fetc%2Fpasswd",
    )

    assert response.status_code == 404
    assert response.json() == {
        "error": "HTTP_404",
        "detail": "The call has no file of this name. Name a file that its file list shows.",
    }


async def test_get_file_missing_returns_404(
    client: TestClient,
    druks_db,
):
    call_id = await _seed_call_id(druks_db)

    response = client.get(
        f"/api/field_notes/transcripts/{call_id}/files/nope.json",
    )

    assert response.status_code == 404
    assert response.json() == {
        "error": "HTTP_404",
        "detail": "The call has no file of this name. Name a file that its file list shows.",
    }


async def test_agent_call_artifact_layout(druks_db, tmp_path):
    # The subdirectory of the layout is the call id. The sandbox runner streams the
    # stdout of every run to stdout.jsonl. The layout is thus the same for every harness.
    note = await Note.create(body="artifact layout")
    run = await seed_run(druks_db, kind=Summarize.kind, subject=note)
    call = await seed_call(druks_db, run, "summarize", model="claude-opus-4-7", status="running")
    directory = Path(call.artifact_dir) / call.id
    assert call.artifact_layout.transcript == directory / "stdout.jsonl"
    assert call.artifact_layout.stderr == directory / "stderr.log"
    assert call.artifact_layout.output == directory / "output.json"


async def test_a_run_reads_with_its_app_and_work_item(druks_client, druks_db):
    note = await Note.create(body="Summarize this")
    run = await seed_run(
        druks_db, kind=Summarize.kind, subject=note, state="failed", failure="the disk is full"
    )

    response = await druks_client.get(f"/api/runs/{run.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["run"]["app"] == "field_notes"
    assert body["subject"] == {"type": "note", "id": str(note.id)}
    assert body["run"]["id"] == run.id
    assert body["run"]["state"] == "failed"
    assert body["run"]["failure"] == "the disk is full"


async def test_a_person_reads_the_run_of_another_account(druks_client, druks_db):
    # The first account is the default one. The second one is thus another person's.
    await Account.get_or_create(druks_db, "service@example.invalid")
    other = await Account.get_or_create(druks_db, "another@example.invalid")
    run = await seed_run(druks_db, kind=Summarize.kind, account_id=other.id)

    response = await druks_client.get(f"/api/runs/{run.id}")

    assert response.status_code == 200
    assert response.json()["subject"] is None
    assert (await druks_client.get("/api/runs/no-such-run")).status_code == 404


async def test_a_run_lists_the_secrets_that_its_box_fetches(druks_client, druks_db):
    github = await connect_service(
        "github",
        identity={"app_id": "1", "slug": "druks-operator"},
        secrets={"private_key": "operator-pem"},
    )
    subscription = await connect_provider(
        AnthropicProvider,
        {"claudeAiOauth": {"accessToken": "live", "refreshToken": "R0", "expiresAt": 0}},
    )
    run = await seed_run(druks_db, kind=Summarize.kind)
    identity, _ = await SandboxIdentity.create(
        druks_db,
        account_id=run.account_id,
        run_id=run.id,
        scoped_to="workflow",
        secret_refs=[
            SecretRef(name="github", secret_id=github.id, resources=["getdruks/druks"]),
            SecretRef(name="anthropic", secret_id=subscription.id),
        ],
        secrets_hash="",
    )

    before_the_box = await druks_client.get(f"/api/runs/{run.id}/sandboxes")
    await identity.bind("host-1")
    with_the_box = await druks_client.get(f"/api/runs/{run.id}/sandboxes")

    sandbox = {
        "scopedTo": "workflow",
        "secrets": [
            {
                "name": "anthropic",
                "secretId": subscription.id,
                "kind": subscription.kind,
                "resources": [],
                "issues": [],
            },
            {
                "name": "github",
                "secretId": github.id,
                "kind": github.kind,
                "resources": ["getdruks/druks"],
                "issues": [],
            },
        ],
    }
    assert before_the_box.json() == [{**sandbox, "isLive": False}]
    assert with_the_box.json() == [{**sandbox, "isLive": True}]

    await SecretIssue.record(druks_db, identity.id, "github", error="GitHub refused")
    await druks_db.commit()
    with_an_issue = await druks_client.get(f"/api/runs/{run.id}/sandboxes")

    [issue] = with_an_issue.json()[0]["secrets"][1]["issues"]
    assert issue.pop("issuedAt")
    assert issue == {
        "fingerprint": "",
        "expiresAt": None,
        "audience": "",
        "mintedAt": None,
        "error": "GitHub refused",
    }


async def test_a_run_with_no_box_lists_no_secrets(druks_client, druks_db):
    subscription = await connect_provider(
        AnthropicProvider,
        {"claudeAiOauth": {"accessToken": "live", "refreshToken": "R0", "expiresAt": 0}},
    )
    run_with_a_box = await seed_run(druks_db, kind=Summarize.kind)
    await SandboxIdentity.create(
        druks_db,
        account_id=run_with_a_box.account_id,
        run_id=run_with_a_box.id,
        scoped_to="workflow",
        secret_refs=[SecretRef(name="anthropic", secret_id=subscription.id)],
        secrets_hash="",
    )
    run = await seed_run(druks_db, kind=Summarize.kind)

    response = await druks_client.get(f"/api/runs/{run.id}/sandboxes")

    assert response.json() == []
    assert (await druks_client.get("/api/runs/no-such-run/sandboxes")).status_code == 404
