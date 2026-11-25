import asyncio
import os
from types import SimpleNamespace

import psycopg
import pytest
from conftest import FORM_TEXT, install_service, set_endpoint
from dbos import DBOS
from druks import registry, ui
from druks.accounts.context import current_conversation_id
from druks.accounts.models import Account
from druks.apps import App
from druks.chat.channels.slack import channel as slack_channel
from druks.chat.enums import ConversationSource, MessageRole, MessageState
from druks.chat.forms import parse_fields
from druks.chat.models import Conversation, Message
from druks.core.services import Slack
from druks.core.webhooks import slack as slack_webhooks
from druks.database import configure_session, get_session
from druks.db import db_session
from druks.durable.constants import NOTIFICATIONS_QUEUE
from druks.durable.engine import (
    configure_engine,
    get_dbos_client,
    init_dbos,
    launch,
    shutdown,
)
from druks.durable.enums import RunState
from druks.models import StoredSubject
from druks.notifications import constants, outbox
from druks.notifications.constants import CLOSE_QUESTION_WORKFLOW, CLOSED_MESSAGE
from druks.notifications.exceptions import (
    AlreadyAcknowledgedError,
    AnswerNotAllowedError,
    ChannelUnavailableError,
    DeliveryError,
)
from druks.notifications.models import Notification
from druks.notifications.outbox import send_notification
from druks.notifications.services import respond_to_notification
from druks.secrets.datastructures import Audience
from druks.secrets.models import VaultSecret
from druks.settings import Settings, configure_settings, get_settings
from druks.testing import configure_app_for_test, init_db, make_settings, worker_name
from druks.workflows import Gate, OperatorReply, Run, Workflow
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field
from sqlalchemy import NullPool, create_engine, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Session

PG_BASE = os.environ.get("DRUKS_TEST_PG", "postgresql://druks:druks@localhost:5432")
DB = worker_name("druks_notifications_durable_test")
URL = f"{PG_BASE.replace('postgresql://', 'postgresql+psycopg://')}/{DB}"
_BOT_TOKEN = "xoxb-durablesecret"


try:
    psycopg.connect(f"{PG_BASE}/postgres", connect_timeout=2).close()
except psycopg.Error:
    POSTGRES_AVAILABLE = False
else:
    POSTGRES_AVAILABLE = True


pytestmark = [
    pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="test Postgres not reachable"),
    pytest.mark.asyncio(loop_scope="module"),
]


class _Question(BaseModel):
    id: str
    prompt: str
    options: list[dict] = Field(default_factory=list)


class NotificationsDurable(App):
    name = "notifications_durable"
    package = __name__


class NotificationProbe(StoredSubject):
    __tablename__ = "test_notification_probes"


# What the in-app reviews resumed with. The respond round-trip checks that the payload
# reached the workflow unchanged.
_REVIEW_REPLIES: list[OperatorReply] = []
# The run ids of every FailingFlow execution, so a test sees a replay happen.
_FAILED_EXECUTIONS: list[str] = []


# The topic that a status flow waits on after each round of its reports, until the test sends.
_NEXT = "status.next"


@pytest.fixture(scope="module", autouse=True)
async def runtime():
    original_settings = get_settings()

    admin = psycopg.connect(f"{PG_BASE}/postgres", autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
    admin.execute(f"CREATE DATABASE {DB}")
    admin.close()

    schema_engine = create_engine(URL)
    init_db(schema_engine)
    with Session(schema_engine) as session:
        session.add(Account(username="op@example.com", is_default=True))
        session.add_all(
            NotificationProbe(id=subject_id)
            for subject_id in (
                9001,
                9002,
                9003,
                9004,
                9006,
                9007,
                9008,
                9009,
                9015,
                9016,
                9017,
                9018,
                9019,
                9023,
            )
        )
        session.commit()
    # NullPool: a TestClient of this module runs requests on its own loop, and a pooled
    # async connection must never cross loops.
    engine = create_async_engine(URL, poolclass=NullPool)
    configure_engine(engine)
    configure_session(engine)

    class ParkNote(Gate):
        name = "park_note"
        action: str = ""

        @classmethod
        async def on_wait(cls, workflow: Workflow) -> None:
            # The override allows a park with no subject.
            return

    class InAppFlow(Workflow):
        subject = NotificationProbe

        async def run_multistep(self, approver: str = "") -> None:
            reply = await self.review(
                questions=[
                    _Question(
                        id="q1",
                        prompt="Which database?",
                        options=[{"id": "postgres", "label": "Postgres"}],
                    )
                ],
                approver=approver,
            )
            _REVIEW_REPLIES.append(reply)

    class ExternalFlow(Workflow):
        subject = NotificationProbe

        async def run_multistep(self) -> None:
            await ParkNote.wait(
                input_request={"presentation": "external", "label": "Answer on the ticket"}
            )

    class SubjectlessFlow(Workflow):
        async def run_multistep(self) -> None:
            await ParkNote.wait(
                input_request={"presentation": "external", "label": "Answer on the ticket"}
            )

    class ExternalUrlFlow(Workflow):
        subject = NotificationProbe

        async def run_multistep(self) -> None:
            await ParkNote.wait(
                input_request={
                    "presentation": "external",
                    "label": "Review the PR",
                    "url": "https://github.com/acme/app/pull/7",
                }
            )

    class DoubleParkFlow(Workflow):
        subject = NotificationProbe

        async def run_multistep(self) -> None:
            await ParkNote.wait(input_request={"presentation": "external", "label": "Round one"})
            await ParkNote.wait(input_request={"presentation": "external", "label": "Round two"})

    class FailingFlow(Workflow):
        subject = NotificationProbe

        async def run_multistep(self) -> None:
            _FAILED_EXECUTIONS.append(self.workflow_id)
            raise RuntimeError("the disk is full")

    class StatusFlow(Workflow):
        subject = NotificationProbe

        async def run_multistep(self, rounds: list[list[str]]) -> None:
            # Each body is a report of its own, and the test lets the next round start.
            for bodies in rounds:
                for body in bodies:
                    await self.report([ui.Markdown(body)])
                await self.wait(_NEXT, label="Waiting for the test", ttl_seconds=120)

    class EndingFlow(Workflow):
        subject = NotificationProbe

        async def run_multistep(self) -> None:
            steps = [
                ui.ProgressStep(label="Read", status=ui.StatusValue("read", tone="success")),
                ui.ProgressStep(label="Gist", status=ui.StatusValue("writing", tone="active")),
            ]
            await self.report([ui.Progress("Summary", steps=steps)])
            await self.wait(_NEXT, label="Waiting for the test", ttl_seconds=120)

    class ReviewedFlow(Workflow):
        subject = NotificationProbe

        async def run_multistep(self) -> None:
            await self.report([ui.Markdown("The gist is written.")])
            await self.review()

    configure_settings(Settings(database_url=URL))  # pyright: ignore[reportCallIssue]
    # The import of the outbox module at the top registers its queue and its workflow
    # before launch(). This whole module runs through that wiring. An earlier module can
    # leave a client that it built on its own database, so the cache clears first.
    get_dbos_client.cache_clear()
    init_dbos()
    await launch()
    # A run starts as an account that can use the workflow's app, so the app
    # answers by name. It is not installed: it has no package for the loader.
    installed = registry.iter_apps()
    apps = pytest.MonkeyPatch()
    apps.setattr(registry, "iter_apps", lambda: [*installed, NotificationsDurable])
    try:
        yield SimpleNamespace(
            engine=engine,
            schema_engine=schema_engine,
            InAppFlow=InAppFlow,
            ExternalFlow=ExternalFlow,
            SubjectlessFlow=SubjectlessFlow,
            ExternalUrlFlow=ExternalUrlFlow,
            DoubleParkFlow=DoubleParkFlow,
            FailingFlow=FailingFlow,
            StatusFlow=StatusFlow,
            EndingFlow=EndingFlow,
            ReviewedFlow=ReviewedFlow,
        )
    finally:
        shutdown()
        await engine.dispose()
        schema_engine.dispose()
        apps.undo()
        NotificationsDurable.workflows.clear()
        configure_settings(original_settings)


class _DeliverSpy:
    def __init__(self):
        self.calls: list[dict] = []
        self.failures_remaining = 0
        self.always_fail = False
        self.error: Exception = DeliveryError("is_archived")
        # When it is set, a post waits for it, so a test can act while the post is under way.
        self.hold: asyncio.Event | None = None

    async def __call__(
        self,
        client,
        channel,
        body,
        *,
        session,
        account,
        blocks=None,
        thread_ts="",
        message_ts="",
        user="",
        actions=None,
        fields=None,
        token=None,
        deep_link=None,
        outcome="",
        question_offset=None,
    ):
        self.calls.append(
            {
                "bot_token": client.token,
                "channel": channel,
                "thread_ts": thread_ts,
                "message_ts": message_ts,
                "user": user,
                "body": body,
                "blocks": blocks,
                "actions": actions,
                "fields": fields,
                "token": token,
                "deep_link": deep_link,
                "outcome": outcome,
                "question_offset": question_offset,
            }
        )
        if self.hold and not message_ts:
            await self.hold.wait()
        if self.always_fail:
            raise self.error
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise self.error
        return {"ok": True, "channel": channel, "ts": f"1700000000.{len(self.calls):06d}"}


@pytest.fixture
async def deliver_spy(monkeypatch):
    spy = _DeliverSpy()
    monkeypatch.setattr(slack_channel, "deliver_notification", spy)
    monkeypatch.setattr(slack_webhooks, "deliver", spy)
    return spy


async def _seed(runtime, seeder):
    # Commit for real: the outbox worker reads through its own sessions.
    # Expunge first, so that the returned instance keeps its loaded attributes past
    # the commit's expiry.
    session = get_session(runtime.engine)
    db_session.registry.set(session)
    try:
        result = await seeder()
        await session.flush()
        session.expunge_all()
        await session.commit()
        return result
    finally:
        await db_session.remove()
        await session.close()


async def _install_slack():
    # The module shares one database, and a service holds one live grant.
    if not await Slack.get_grant():
        await install_service(
            "slack",
            access_token=_BOT_TOKEN,
            identity={"client_id": "slack-client"},
            secrets={"client_secret": "slack-secret"},
        )


async def _seed_person(runtime, name):
    # The module shares one database, so each test names its own person.
    async def create():
        await _install_slack()
        account = await Account.get_or_create(db_session(), f"{name}@example.com")
        await account.update_preferences(notification_channel_id="C1")
        return account

    return await _seed(runtime, create)


async def _deliver(runtime, *, to, subject=None, reason="r", body="b", actions=None):
    # The path of a producer, as a gate's park takes it: commit the row, then enqueue the
    # outbox.
    async def create():
        account = await Account.get_or_create(db_session(), f"{to}@example.com")
        notification = await Notification.create(
            db_session(),
            account_id=account.id,
            reason=reason,
            body=body,
            subject=subject or {"type": "notification_probe", "id": 1},
            actions=actions,
        )
        return notification.id

    notification_id = await _seed(runtime, create)
    await DBOS.enqueue_workflow_async(NOTIFICATIONS_QUEUE, send_notification, notification_id)
    return notification_id


async def _snapshot(runtime, notification_id) -> dict:
    session = get_session(runtime.engine)
    try:
        notification = await session.get(Notification, notification_id)
        return {
            "state": notification.state,
            "attempts": notification.attempts,
            "last_error": notification.last_error,
            "delivered_at": notification.delivered_at,
            "token": notification.correlation_token,
            "message_id": notification.message_id,
        }
    finally:
        await session.close()


async def _wait_for(runtime, notification_id, predicate, timeout=30.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        snapshot = await _snapshot(runtime, notification_id)
        if predicate(snapshot):
            return snapshot
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out; last={await _snapshot(runtime, notification_id)}")


async def test_outbox_delivers_with_the_bot_token_actions_and_token(runtime, deliver_spy):
    await _seed_person(runtime, "happy")

    notification_id = await _deliver(
        runtime, to="happy", reason="ops.alert", body="hello", actions=[{"id": "ok", "label": "OK"}]
    )

    done = await _wait_for(
        runtime, notification_id, lambda snapshot: snapshot["state"] == "delivered"
    )
    assert done["attempts"] == 1
    assert done["delivered_at"]
    assert done["message_id"] == "C1:1700000000.000001"
    (call,) = deliver_spy.calls
    assert call["bot_token"] == _BOT_TOKEN
    assert (call["channel"], call["thread_ts"]) == ("C1", "")
    assert call["body"] == "hello"
    assert call["actions"] == [{"id": "ok", "label": "OK"}]
    assert call["token"] == done["token"]


async def test_what_a_click_starts_reaches_slack_with_the_bot_token(runtime, deliver_spy):
    await _seed_person(runtime, "clicked")
    notification_id = await _deliver(runtime, to="clicked", body="review the plan")
    await _wait_for(runtime, notification_id, lambda snapshot: snapshot["state"] == "delivered")

    await slack_webhooks.replace_buttons(
        notification_id,
        channel="C1",
        message_ts="1700000000.000200",
        outcome="Answer: Approve",
    )
    await slack_webhooks.explain_reason(
        "C1",
        user="U7",
        reason="This notification was already acknowledged.",
        thread_ts="",
    )

    _, edited, explained = deliver_spy.calls
    assert (edited["bot_token"], explained["bot_token"]) == (_BOT_TOKEN, _BOT_TOKEN)
    assert (edited["body"], edited["message_ts"], edited["outcome"]) == (
        "review the plan",
        "1700000000.000200",
        "Answer: Approve",
    )
    assert (explained["body"], explained["user"]) == (
        "This notification was already acknowledged.",
        "U7",
    )


async def test_transient_failure_retries_to_delivered_and_touches_no_run(runtime, deliver_spy):
    deliver_spy.failures_remaining = 2
    await _seed_person(runtime, "flaky")

    notification_id = await _deliver(runtime, to="flaky")

    done = await _wait_for(
        runtime, notification_id, lambda snapshot: snapshot["state"] == "delivered"
    )
    assert done["attempts"] == 3
    assert len(deliver_spy.calls) == 3
    # Delivery does not touch the run lifecycle: no Run row exists.
    with runtime.schema_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM durable_runs")).scalar_one() == 0


async def test_terminal_failure_marks_failed_sanitized_and_reads_back(
    runtime, deliver_spy, monkeypatch, tmp_path
):
    deliver_spy.always_fail = True
    deliver_spy.error = DeliveryError("is_archived")
    # Two attempts, not five: the terminal path is the same, and the backoff is shorter.
    monkeypatch.setattr(outbox, "_SEND_RETRIES", {"retries_allowed": True, "max_attempts": 2})
    await _seed_person(runtime, "doomed")

    notification_id = await _deliver(runtime, to="doomed")

    done = await _wait_for(runtime, notification_id, lambda snapshot: snapshot["state"] == "failed")
    assert done["attempts"] == 2
    assert len(deliver_spy.calls) == 2
    assert done["last_error"] == "Delivery failed: is_archived."

    settings = make_settings(tmp_path, identity={"mode": "header", "header": "X-ExeDev-Email"})
    app = configure_app_for_test(settings=settings, engine=runtime.engine, authenticated=False)
    shown = TestClient(app).get(
        f"/api/notifications/{notification_id}", headers={"X-ExeDev-Email": "doomed@example.com"}
    )
    assert shown.status_code == 200
    assert shown.json()["state"] == "failed"
    assert shown.json()["attempts"] == 2
    assert done["token"] not in shown.text


async def test_unexpected_error_reduces_to_class_name(runtime, deliver_spy, monkeypatch):
    # The bot token in the error of a faulty transport must never reach last_error
    # verbatim.
    deliver_spy.always_fail = True
    deliver_spy.error = RuntimeError(f"boom at {_BOT_TOKEN}")
    monkeypatch.setattr(outbox, "_SEND_RETRIES", {"retries_allowed": True, "max_attempts": 2})
    await _seed_person(runtime, "leaky")

    notification_id = await _deliver(runtime, to="leaky")

    done = await _wait_for(runtime, notification_id, lambda snapshot: snapshot["state"] == "failed")
    assert done["last_error"] == "RuntimeError"


async def test_a_send_without_slack_records_that_slack_is_not_connected(
    runtime, deliver_spy, monkeypatch
):
    monkeypatch.setattr(outbox, "_SEND_RETRIES", {"retries_allowed": True, "max_attempts": 2})
    await _seed_person(runtime, "unplugged")

    async def revoke_grant():
        grant = await Slack.get_grant()
        await grant.revoke("user")

    await _seed(runtime, revoke_grant)

    notification_id = await _deliver(runtime, to="unplugged")

    done = await _wait_for(runtime, notification_id, lambda snapshot: snapshot["state"] == "failed")
    assert "slack is not connected" in done["last_error"]
    assert not deliver_spy.calls


async def _notify_person(runtime, email, *, slack_user="", notification_channel=""):
    # The module shares one database, so each test names its own person.
    async def create():
        await _install_slack()
        account = await Account.get_or_create(db_session(), email)
        if slack_user:
            await VaultSecret.connect(
                db_session(),
                Audience.service("slack"),
                account_id=account.id,
                refresh_token="",
                scopes=["users:read"],
                identity={"authority": "https://slack.com/T1", "subject": slack_user},
            )
        if notification_channel:
            await account.update_preferences(notification_channel_id=notification_channel)
        notification = await Notification.create(
            db_session(),
            account_id=account.id,
            reason="subscription.disconnected",
            body="Your Anthropic subscription is disconnected.",
            deep_link="https://druks.test/settings/providers",
        )
        return notification.id

    notification_id = await _seed(runtime, create)
    await DBOS.enqueue_workflow_async(NOTIFICATIONS_QUEUE, send_notification, notification_id)
    return notification_id


async def test_a_person_s_notification_goes_to_their_slack_user(runtime, deliver_spy):
    notification_id = await _notify_person(runtime, "dm@example.com", slack_user="U42")

    done = await _wait_for(
        runtime, notification_id, lambda snapshot: snapshot["state"] == "delivered"
    )
    (call,) = deliver_spy.calls
    assert call["channel"] == "U42"
    assert call["deep_link"] == "https://druks.test/settings/providers"
    assert done["message_id"] == "U42:1700000000.000001"


async def test_a_person_s_notification_goes_to_the_channel_they_chose(runtime, deliver_spy):
    notification_id = await _notify_person(
        runtime, "channel@example.com", slack_user="U43", notification_channel="C7"
    )

    await _wait_for(runtime, notification_id, lambda snapshot: snapshot["state"] == "delivered")
    (call,) = deliver_spy.calls
    assert call["channel"] == "C7"


async def test_a_channel_that_refuses_the_bot_sends_the_notification_by_dm(runtime, deliver_spy):
    deliver_spy.failures_remaining = 1
    deliver_spy.error = ChannelUnavailableError("the bot is not in channel C8")
    notification_id = await _notify_person(
        runtime, "removed@example.com", slack_user="U44", notification_channel="C8"
    )

    done = await _wait_for(
        runtime, notification_id, lambda snapshot: snapshot["state"] == "delivered"
    )
    assert [call["channel"] for call in deliver_spy.calls] == ["C8", "U44"]
    assert done["message_id"].startswith("U44:")


async def test_a_notification_with_no_channel_is_delivered_with_no_message(runtime, deliver_spy):
    notification_id = await _notify_person(runtime, "no-slack@example.com")

    done = await _wait_for(
        runtime, notification_id, lambda snapshot: snapshot["state"] == "delivered"
    )
    assert done["attempts"] == 1
    assert not deliver_spy.calls


async def test_rerun_on_delivered_notification_skips_the_send(runtime, deliver_spy):
    await _seed_person(runtime, "once")
    notification_id = await _deliver(runtime, to="once")
    await _wait_for(runtime, notification_id, lambda snapshot: snapshot["state"] == "delivered")
    assert len(deliver_spy.calls) == 1

    handle = await DBOS.enqueue_workflow_async(
        NOTIFICATIONS_QUEUE, send_notification, notification_id
    )
    await handle.get_result()

    assert len(deliver_spy.calls) == 1


async def test_create_seam_plus_direct_enqueue_delivers(runtime, deliver_spy):
    account = await _seed_person(runtime, "seam")

    async def create():
        return await Notification.create(
            db_session(),
            account_id=account.id,
            reason="r",
            body="b",
            subject={"type": "probe", "id": 1},
        )

    notification = await _seed(runtime, create)
    # The create seam persisted a pending row and enqueued nothing.
    assert (await _snapshot(runtime, notification.id))["state"] == "pending"
    assert not deliver_spy.calls

    handle = await DBOS.enqueue_workflow_async(
        NOTIFICATIONS_QUEUE, send_notification, notification.id
    )
    await handle.get_result()

    assert (await _snapshot(runtime, notification.id))["state"] == "delivered"
    assert len(deliver_spy.calls) == 1


async def _set_notification_channel(runtime, channel_id):
    async def update():
        await _install_slack()
        account = await Account.get_default(db_session())
        await account.update_preferences(notification_channel_id=channel_id)

    await _seed(runtime, update)


async def _run_snapshot(runtime, workflow_id) -> Run:
    session = get_session(runtime.engine)
    try:
        return await session.get(Run, workflow_id)
    finally:
        await session.close()


async def _notifications_for_run(runtime, workflow_id) -> list[Notification]:
    session = get_session(runtime.engine)
    try:
        result = await session.execute(
            select(Notification).where(Notification.run_id == workflow_id).order_by(Notification.id)
        )
        return list(result.scalars().all())
    finally:
        await session.close()


async def _wait_run(runtime, workflow_id, predicate, timeout=30.0) -> Run:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        run = await _run_snapshot(runtime, workflow_id)
        if run and predicate(run):
            return run
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out; last={await _run_snapshot(runtime, workflow_id)}")


async def _wait_notifications(runtime, workflow_id, predicate, timeout=30.0) -> list[Notification]:
    """The notifications of the run, once they satisfy ``predicate``."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        rows = await _notifications_for_run(runtime, workflow_id)
        if predicate(rows):
            return rows
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out; last={await _notifications_for_run(runtime, workflow_id)}")


async def _wait_notification(runtime, workflow_id, state, timeout=30.0) -> Notification:
    rows = await _wait_notifications(
        runtime, workflow_id, lambda rows: rows and rows[0].state == state, timeout
    )
    return rows[0]


async def test_in_app_park_notifies_with_actions(runtime, deliver_spy):
    await _set_notification_channel(runtime, "C-inbox-inapp")
    set_endpoint("https://druks.example")

    workflow_id = await runtime.InAppFlow.start(subject=NotificationProbe(id=9001))
    parked = await _wait_run(runtime, workflow_id, lambda run: run.state == RunState.PARKED)

    notification = await _wait_notification(runtime, workflow_id, "delivered")
    assert len(await _notifications_for_run(runtime, workflow_id)) == 1
    assert notification.reason == "gate.parked"
    assert notification.body.startswith("# Review")
    assert "Which database?\n- Postgres" in notification.body
    assert notification.actions == [
        {"id": "approve", "label": "Approve"},
        {"id": "request_changes", "label": "Request changes"},
    ]
    assert [field["name"] for field in notification.fields] == ["question:q1", "note"]
    assert notification.deep_link == f"https://druks.example/runs/{workflow_id}"
    assert notification.subject == {"type": "notification_probe", "id": 9001}
    assert notification.run_id == workflow_id
    assert notification.run_parked_at == parked.input_requested_at

    (call,) = deliver_spy.calls
    assert call["blocks"] == notification.blocks
    assert call["actions"] == notification.actions
    assert call["fields"] == notification.fields
    assert call["token"] == notification.correlation_token
    assert call["deep_link"] == notification.deep_link


async def test_external_park_notifies_without_actions(runtime, deliver_spy):
    await _set_notification_channel(runtime, "C-inbox-ext")

    workflow_id = await runtime.ExternalFlow.start(subject=NotificationProbe(id=9002))
    await _wait_run(runtime, workflow_id, lambda run: run.state == RunState.PARKED)

    notification = await _wait_notification(runtime, workflow_id, "delivered")
    assert notification.body == "Answer on the ticket"
    assert (notification.actions, notification.fields) == (None, None)
    assert not notification.deep_link
    (call,) = deliver_spy.calls
    assert (call["actions"], call["fields"]) == (None, None)


async def test_external_park_with_declared_url_sets_deep_link(runtime, deliver_spy):
    await _set_notification_channel(runtime, "C-inbox-url")
    set_endpoint("https://druks.example")

    workflow_id = await runtime.ExternalUrlFlow.start(subject=NotificationProbe(id=9003))
    await _wait_run(runtime, workflow_id, lambda run: run.state == RunState.PARKED)

    notification = await _wait_notification(runtime, workflow_id, "delivered")
    assert notification.deep_link == "https://github.com/acme/app/pull/7"
    assert not notification.actions
    (call,) = deliver_spy.calls
    assert call["deep_link"] == notification.deep_link


async def test_a_park_with_no_channel_is_delivered_with_no_message(runtime, deliver_spy):
    await _set_notification_channel(runtime, None)

    workflow_id = await runtime.ExternalFlow.start(subject=NotificationProbe(id=9004))
    await _wait_run(runtime, workflow_id, lambda run: run.state == RunState.PARKED)

    # The unattended account has no channel and no Slack sign-in.
    await _wait_notification(runtime, workflow_id, "delivered")
    assert not deliver_spy.calls


async def test_subjectless_park_notifies_nothing(runtime, deliver_spy):
    await _set_notification_channel(runtime, "C-inbox-nosubj")

    workflow_id = await runtime.SubjectlessFlow.start(subject=None)
    await _wait_run(runtime, workflow_id, lambda run: run.state == RunState.PARKED)

    await asyncio.sleep(1.0)  # room for a wrong notification to land
    assert not await _notifications_for_run(runtime, workflow_id)
    assert not deliver_spy.calls


async def test_failed_delivery_leaves_run_parked_and_resumable(runtime, deliver_spy, monkeypatch):
    deliver_spy.always_fail = True
    monkeypatch.setattr(outbox, "_SEND_RETRIES", {"retries_allowed": True, "max_attempts": 2})
    await _set_notification_channel(runtime, "C-inbox-flaky")

    workflow_id = await runtime.InAppFlow.start(subject=NotificationProbe(id=9006))
    await _wait_run(runtime, workflow_id, lambda run: run.state == RunState.PARKED)
    await _wait_notification(runtime, workflow_id, "failed")

    # The park never noticed the dead endpoint: the run still waits, and it can still resume.
    parked = await _run_snapshot(runtime, workflow_id)
    assert parked.state == RunState.PARKED
    await parked.resume(action="approve")
    await _wait_run(runtime, workflow_id, lambda run: run.state == RunState.FINISHED)


async def test_replayed_park_notifies_once(runtime, deliver_spy):
    await _set_notification_channel(runtime, "C-inbox-replay")

    workflow_id = await runtime.ExternalFlow.start(subject=NotificationProbe(id=9007))
    await _wait_run(runtime, workflow_id, lambda run: run.state == RunState.PARKED)
    await _wait_notification(runtime, workflow_id, "delivered")
    assert len(deliver_spy.calls) == 1

    # Run the parked workflow again from its checkpoints. The notify step returns its
    # recorded row, and the enqueue is a recorded child start, so no outbox run starts.
    await _replay(runtime, workflow_id)
    await _wait_run(runtime, workflow_id, lambda run: run.state == RunState.PARKED)
    await asyncio.sleep(1.0)  # room for a wrong duplicate enqueue to land

    assert len(await _notifications_for_run(runtime, workflow_id)) == 1
    assert len(deliver_spy.calls) == 1

    # Two waiters now share the topic: the first one and the replayed one. The cancel
    # ends the run and both waiters.
    await _cancel(runtime, workflow_id)


async def test_each_park_round_gets_its_own_notification(runtime, deliver_spy):
    await _set_notification_channel(runtime, "C-inbox-rounds")

    workflow_id = await runtime.DoubleParkFlow.start(subject=NotificationProbe(id=9008))
    first_round = await _wait_run(runtime, workflow_id, lambda run: run.state == RunState.PARKED)
    await _wait_notification(runtime, workflow_id, "delivered")
    assert len(deliver_spy.calls) == 1

    await first_round.resume(action="go")
    # The same code parks again in a later step. A new checkpoint, a new row, and a new
    # delivery follow.
    await _wait_run(
        runtime,
        workflow_id,
        lambda run: (
            run.state == RunState.PARKED
            and run.input_requested_at != first_round.input_requested_at
        ),
    )
    rows = await _wait_notifications(
        runtime, workflow_id, lambda rows: len(rows) == 2 and rows[1].state == "delivered"
    )

    assert [row.body for row in rows] == ["Round one", "Round two"]
    assert rows[0].run_parked_at != rows[1].run_parked_at
    assert len(deliver_spy.calls) == 2


async def test_a_failed_run_with_no_chat_tells_its_owner(runtime, deliver_spy):
    await _set_notification_channel(runtime, None)
    set_endpoint("https://druks.example")
    workflow_id = await runtime.FailingFlow.start(subject=NotificationProbe(id=9016))
    run = await _wait_run(runtime, workflow_id, lambda run: run.state == RunState.FAILED)

    (notification,) = await _notifications_for_run(runtime, workflow_id)
    assert notification.account_id == run.account_id
    assert notification.reason == "run.failed"
    assert "the disk is full" in notification.body
    assert notification.deep_link == f"https://druks.example/runs/{workflow_id}"
    # The unattended account has no Slack sign-in, so the bell is where it reads the notice.
    await _wait_for(runtime, notification.id, lambda snapshot: snapshot["state"] == "delivered")


async def test_a_failed_run_a_chat_started_sends_no_notification(runtime, deliver_spy):
    async def create():
        account = await Account.get_default(db_session())
        conversation = Conversation(
            account_id=account.id,
            source=ConversationSource.SLACK,
            user_id="U1",
            thread_id="C9:1700000000.000200",
        )
        db_session().add(conversation)
        await db_session().flush()
        return conversation.id

    reset = current_conversation_id.set(await _seed(runtime, create))
    try:
        workflow_id = await runtime.FailingFlow.start(subject=NotificationProbe(id=9017))
    finally:
        current_conversation_id.reset(reset)
    await _wait_run(runtime, workflow_id, lambda run: run.state == RunState.FAILED)

    assert not await _notifications_for_run(runtime, workflow_id)


async def test_a_replayed_failure_tells_the_owner_once(runtime, deliver_spy):
    workflow_id = await runtime.FailingFlow.start(subject=NotificationProbe(id=9018))
    await _wait_run(runtime, workflow_id, lambda run: run.state == RunState.FAILED)

    # A crash after the notification's checkpoints, before DBOS recorded the ERROR, leaves
    # the workflow PENDING. DBOS never resumes an ERROR workflow, so the test puts it back.
    with runtime.schema_engine.begin() as connection:
        connection.execute(
            text("UPDATE dbos.workflow_status SET status = 'PENDING' WHERE workflow_uuid = :id"),
            {"id": workflow_id},
        )
    await _replay(runtime, workflow_id)
    await _wait_run(runtime, workflow_id, lambda run: run.state == RunState.FAILED)
    await asyncio.sleep(1.0)  # room for a wrong duplicate enqueue to land

    assert len(await _notifications_for_run(runtime, workflow_id)) == 1


async def test_an_answer_from_elsewhere_closes_the_question_s_message(runtime, deliver_spy):
    await _set_notification_channel(runtime, "C-inbox-answered")

    workflow_id = await runtime.InAppFlow.start(subject=NotificationProbe(id=9020))
    parked = await _wait_run(runtime, workflow_id, lambda run: run.state == RunState.PARKED)
    posted = await _wait_notification(runtime, workflow_id, "delivered")

    await parked.resume(action="approve", answers={"q1": "postgres"}, note="")
    await _wait_notification(runtime, workflow_id, "acknowledged")

    (edit,) = [call for call in deliver_spy.calls if call["outcome"]]
    assert f"{edit['channel']}:{edit['message_ts']}" == posted.message_id
    assert edit["outcome"] == CLOSED_MESSAGE
    # The edit draws the blocks again, so the closed message keeps them.
    assert edit["blocks"] == posted.blocks


async def test_a_cancelled_run_closes_the_question_s_message(runtime, deliver_spy):
    await _set_notification_channel(runtime, "C-inbox-cancelled")

    workflow_id = await runtime.InAppFlow.start(subject=NotificationProbe(id=9021))
    await _wait_run(runtime, workflow_id, lambda run: run.state == RunState.PARKED)
    posted = await _wait_notification(runtime, workflow_id, "delivered")

    session = get_session(runtime.engine)
    db_session.registry.set(session)
    try:
        await (await session.get(Run, workflow_id)).cancel()
        await session.commit()
    finally:
        await db_session.remove()
        await session.close()
    await _wait_notification(runtime, workflow_id, "acknowledged")

    (edit,) = [call for call in deliver_spy.calls if call["outcome"]]
    assert f"{edit['channel']}:{edit['message_ts']}" == posted.message_id
    assert edit["outcome"] == CLOSED_MESSAGE


async def _respond_in_own_session(
    runtime, token, choice, *, user_id=None, account_id=None, **click
) -> None:
    # The answer binds a session of its own in the scoped registry, as a request has. A
    # TestClient portal loop poisons the module's shared executor when it drives DBOS's
    # send.
    session = get_session(runtime.engine)
    db_session.registry.set(session)
    try:
        await respond_to_notification(
            session, token, choice, user_id=user_id, account_id=account_id, **click
        )
        await session.commit()
    finally:
        await db_session.remove()
        await session.close()


async def test_respond_round_trip_finishes_the_run(runtime, deliver_spy):
    await _set_notification_channel(runtime, "C-inbox-respond")
    workflow_id = await runtime.InAppFlow.start(subject=NotificationProbe(id=9009))
    await _wait_run(runtime, workflow_id, lambda run: run.state == RunState.PARKED)
    notification = await _wait_notification(runtime, workflow_id, "delivered")
    token = notification.correlation_token
    assert not notification.deep_link

    await _respond_in_own_session(
        runtime, token, {"control": "approve", "answers": {"q1": "postgres"}, "note": ""}
    )

    await _wait_run(runtime, workflow_id, lambda run: run.state == RunState.FINISHED)
    assert OperatorReply(action="approve", answers={"q1": "postgres"}) in _REVIEW_REPLIES
    assert (await _notifications_for_run(runtime, workflow_id))[0].state == "acknowledged"

    # A second answer finds the notification acknowledged, and DBOS still holds one reply
    # for the round.
    with pytest.raises(AlreadyAcknowledgedError):
        await _respond_in_own_session(runtime, token, {"control": "approve"})
    with runtime.schema_engine.connect() as connection:
        replies = connection.execute(
            text(
                "SELECT count(*) FROM dbos.notifications"
                " WHERE destination_uuid = :id AND topic = 'review'"
            ),
            {"id": workflow_id},
        ).scalar_one()
    assert replies == 1


async def test_a_slack_chat_s_review_offers_its_buttons_in_its_thread(runtime, deliver_spy):
    set_endpoint("https://druks.example")

    async def create():
        await _install_slack()
        account = await Account.get_default(db_session())
        conversation = Conversation(
            account_id=account.id,
            source=ConversationSource.SLACK,
            user_id="U1",
            thread_id="C9:1700000000.000100",
        )
        db_session().add(conversation)
        await db_session().flush()
        return conversation.id

    conversation_id = await _seed(runtime, create)
    reset = current_conversation_id.set(conversation_id)
    try:
        workflow_id = await runtime.InAppFlow.start(subject=NotificationProbe(id=9015))
    finally:
        current_conversation_id.reset(reset)
    await _wait_run(runtime, workflow_id, lambda run: run.state == RunState.PARKED)

    # The chat asks, so the run's account gets no second notification.
    (in_thread,) = await _wait_notifications(
        runtime,
        workflow_id,
        lambda rows: any(
            row.conversation_id == conversation_id and row.state == "delivered" for row in rows
        ),
    )
    assert in_thread.actions == [
        {"id": "approve", "label": "Approve"},
        {"id": "request_changes", "label": "Request changes"},
    ]
    assert [field["name"] for field in in_thread.fields] == ["question:q1", "note"]
    assert in_thread.deep_link == f"https://druks.example/runs/{workflow_id}"
    (call,) = [call for call in deliver_spy.calls if call["token"] == in_thread.correlation_token]
    assert (call["channel"], call["thread_ts"]) == ("C9", "1700000000.000100")
    assert (call["actions"], call["fields"]) == (in_thread.actions, in_thread.fields)
    assert call["deep_link"] == in_thread.deep_link

    session = get_session(runtime.engine)
    try:
        (approval,) = await session.scalars(
            select(Message).where(Message.conversation_id == conversation_id)
        )
        assert "Its buttons are in this thread" in approval.body
        assert (approval.is_internal, approval.state) == (True, MessageState.DEFERRED)
    finally:
        await session.close()
    # The ask with its buttons is the person's one message, so the park starts no turn.
    assert not await DBOS.list_workflows_async(name="chat.deliver", parent_workflow_id=workflow_id)

    # The click carries the answer and the note, so the chat's agent is not asked.
    await _respond_in_own_session(
        runtime,
        in_thread.correlation_token,
        {"control": "request_changes"},
        user_id="U1",
        values={"question:q1": "postgres", "note": "Use a queue."},
        has_inputs=True,
    )
    await _wait_run(runtime, workflow_id, lambda run: run.state == RunState.FINISHED)
    # The person of the chat answers as the chat's account.
    reply = OperatorReply(
        action="request_changes",
        answers={"q1": "postgres"},
        note="Use a queue.",
        answered_by="op@example.com",
    )
    assert reply in _REVIEW_REPLIES
    session = get_session(runtime.engine)
    try:
        bodies = await session.scalars(
            select(Message.body).where(
                Message.conversation_id == conversation_id,
                Message.role == MessageRole.USER,
                ~Message.is_internal,
            )
        )
        assert not list(bodies)
    finally:
        await session.close()


async def test_a_review_asks_its_approver_and_only_their_answer_resumes_it(runtime, deliver_spy):
    async def create():
        await _install_slack()
        approver = await Account.get_or_create(db_session(), "paulo@example.com")
        await approver.update_preferences(notification_channel_id="C-paulo")
        requester = await Account.get_default(db_session())
        conversation = Conversation(
            account_id=requester.id, source=ConversationSource.SLACK, user_id="U1"
        )
        db_session().add(conversation)
        await db_session().flush()
        return approver.id, requester.id, conversation.id

    approver_id, requester_id, conversation_id = await _seed(runtime, create)
    reset = current_conversation_id.set(conversation_id)
    try:
        workflow_id = await runtime.InAppFlow.start(
            subject=NotificationProbe(id=9019), approver="Paulo@example.com"
        )
    finally:
        current_conversation_id.reset(reset)

    notification = await _wait_notification(runtime, workflow_id, "delivered")
    # The approver gets the question, and the requester's chat gets no buttons.
    assert [row.id for row in await _notifications_for_run(runtime, workflow_id)] == [
        notification.id
    ]
    assert notification.account_id == approver_id
    (call,) = deliver_spy.calls
    assert (call["channel"], call["actions"]) == ("C-paulo", notification.actions)
    session = get_session(runtime.engine)
    try:
        (told,) = await session.scalars(
            select(Message.body).where(Message.conversation_id == conversation_id)
        )
    finally:
        await session.close()
    # The requester's chat hears the approver, as the account spells the name.
    assert "paulo@example.com" in told

    token = notification.correlation_token
    with pytest.raises(AnswerNotAllowedError):
        await _respond_in_own_session(
            runtime, token, {"control": "approve"}, user_id="U1", account_id=requester_id
        )
    await _respond_in_own_session(
        runtime, token, {"control": "approve"}, user_id="U2", account_id=approver_id
    )

    await _wait_run(runtime, workflow_id, lambda run: run.state == RunState.FINISHED)
    assert OperatorReply(action="approve", answered_by="paulo@example.com") in _REVIEW_REPLIES


async def test_a_chat_of_the_approver_s_own_account_asks_in_the_chat(runtime, deliver_spy):
    async def create():
        await _install_slack()
        account = await Account.get_default(db_session())
        conversation = Conversation(
            account_id=account.id,
            source=ConversationSource.SLACK,
            user_id="U1",
            thread_id="C9:1700000000.000900",
        )
        db_session().add(conversation)
        await db_session().flush()
        return account.username, conversation.id

    username, conversation_id = await _seed(runtime, create)
    reset = current_conversation_id.set(conversation_id)
    try:
        workflow_id = await runtime.InAppFlow.start(
            subject=NotificationProbe(id=9023), approver=username
        )
    finally:
        current_conversation_id.reset(reset)

    notification = await _wait_notification(runtime, workflow_id, "delivered")
    # The thread's buttons answer as the approver, so the approver gets no second question.
    assert [row.id for row in await _notifications_for_run(runtime, workflow_id)] == [
        notification.id
    ]
    assert (notification.account_id, notification.conversation_id) == (None, conversation_id)
    await _respond_in_own_session(
        runtime, notification.correlation_token, {"control": "approve"}, user_id="U1"
    )
    await _wait_run(runtime, workflow_id, lambda run: run.state == RunState.FINISHED)


async def test_a_park_notifies_the_run_s_account_by_its_channel(runtime, deliver_spy):
    async def configure():
        await _install_slack()
        account = await Account.get_or_create(db_session(), "explicit@example.com")
        await account.update_preferences(notification_channel_id="C-explicit")
        subject = NotificationProbe(id=9021)
        db_session().add(subject)
        return account.id, subject

    account_id, subject = await _seed(runtime, configure)
    workflow_id = await runtime.ExternalFlow.start(subject=subject, account_id=account_id)
    notification = await _wait_notification(runtime, workflow_id, "delivered")
    assert notification.account_id == account_id
    (call,) = deliver_spy.calls
    assert call["channel"] == "C-explicit"


CHOICES = {"actions": [{"id": "1", "label": "Postgres"}, {"id": "2", "label": "MySQL"}]}
FORM = {"fields": parse_fields(FORM_TEXT)}


async def _create_question(user_id: str, ask: dict) -> Notification:
    """A question that a Slack chat's agent asked, as its turn left it: with choices, or
    with the fields of a form."""
    await _install_slack()
    account = await Account.get_default(db_session())
    conversation = Conversation(
        account_id=account.id,
        source=ConversationSource.SLACK,
        user_id=user_id,
        thread_id="C9:1700000000.000300",
    )
    db_session().add(conversation)
    await db_session().flush()
    return await Notification.create(
        db_session(),
        conversation_id=conversation.id,
        reason="question.asked",
        body="I read both.\n\nWhich database?",
        question_offset=12,
        **ask,
    )


async def _ask(runtime, user_id: str, ask: dict) -> Notification:
    """The question and its queued send, as the turn that asked commits them."""

    async def create():
        question = await _create_question(user_id, ask)
        await outbox.queue_send(db_session(), question)
        return question

    return await _seed(runtime, create)


async def _answer_in_words(runtime, conversation_id: str) -> None:
    """What the person's typed answer does when their message reaches the agent."""

    async def close():
        await outbox.close_questions(db_session(), conversation_id)

    await _seed(runtime, close)


async def _wait_workflow(runtime, workflow_id: str, statuses=("SUCCESS", "ERROR")) -> str:
    deadline = asyncio.get_event_loop().time() + 30
    while asyncio.get_event_loop().time() < deadline:
        with runtime.schema_engine.connect() as connection:
            status = connection.execute(
                text("SELECT status FROM dbos.workflow_status WHERE workflow_uuid = :id"),
                {"id": workflow_id},
            ).scalar_one_or_none()
        if status in statuses:
            return status
        await asyncio.sleep(0.1)
    raise AssertionError(f"workflow {workflow_id} is still {status}")


@pytest.mark.parametrize("ask", [CHOICES, FORM], ids=["choices", "form"])
async def test_a_question_that_an_answer_closed_in_the_queue_posts_its_reply_closed(
    runtime, deliver_spy, ask
):
    async def create():
        question = await _create_question("U21", ask)
        await outbox.close_questions(db_session(), question.conversation_id)
        await outbox.queue_send(db_session(), question)
        return question

    question = await _seed(runtime, create)

    assert await _wait_workflow(runtime, f"{CLOSE_QUESTION_WORKFLOW}:{question.id}") == "SUCCESS"
    done = await _snapshot(runtime, question.id)
    assert (done["state"], done["message_id"]) == ("acknowledged", "C9:1700000000.000001")
    posted = deliver_spy.calls[0]
    assert (posted["message_ts"], posted["body"], posted["question_offset"]) == (
        "",
        "I read both.\n\nWhich database?",
        12,
    )
    assert (posted["actions"], posted["fields"], posted["outcome"]) == (None, None, CLOSED_MESSAGE)


@pytest.mark.parametrize("ask", [CHOICES, FORM], ids=["choices", "form"])
async def test_an_answer_while_a_question_posts_takes_its_buttons_off_after_the_post(
    runtime, deliver_spy, ask
):
    deliver_spy.hold = asyncio.Event()
    question = await _ask(runtime, "U22", ask)
    await _wait_deliveries(deliver_spy, bool)

    await _answer_in_words(runtime, question.conversation_id)
    close = f"{CLOSE_QUESTION_WORKFLOW}:{question.id}"
    await _wait_workflow(runtime, close, statuses=("PENDING", "SUCCESS", "ERROR"))
    await asyncio.sleep(1.0)  # room for a close that does not wait for the post to end
    deliver_spy.hold.set()

    assert await _wait_workflow(runtime, close) == "SUCCESS"
    done = await _snapshot(runtime, question.id)
    posted, closed = deliver_spy.calls
    assert (done["state"], done["message_id"]) == ("acknowledged", "C9:1700000000.000001")
    assert (posted["actions"], posted["fields"], posted["question_offset"]) == (
        question.actions,
        question.fields,
        12,
    )
    assert (closed["message_ts"], closed["outcome"], closed["actions"], closed["fields"]) == (
        "1700000000.000001",
        CLOSED_MESSAGE,
        None,
        None,
    )
    assert closed["question_offset"] == 12


async def test_a_refused_edit_leaves_the_question_closed_and_retries_nothing(runtime, deliver_spy):
    question = await _ask(runtime, "U24", CHOICES)
    await _wait_for(runtime, question.id, lambda snapshot: snapshot["state"] == "delivered")
    deliver_spy.always_fail = True

    await _answer_in_words(runtime, question.conversation_id)

    assert await _wait_workflow(runtime, f"{CLOSE_QUESTION_WORKFLOW}:{question.id}") == "SUCCESS"
    _, refused = deliver_spy.calls
    assert refused["outcome"] == CLOSED_MESSAGE
    assert (await _snapshot(runtime, question.id))["state"] == "acknowledged"


@pytest.fixture
def status_delay(monkeypatch):
    # A short delay, so a status goes out soon after it changes.
    monkeypatch.setattr(constants, "STATUS_DELAY_SECONDS", 0.2)


async def _get_status(runtime, workflow_id) -> Notification | None:
    session = get_session(runtime.engine)
    try:
        return await Notification.get_status(session, workflow_id)
    finally:
        await session.close()


async def _wait_status(runtime, workflow_id, predicate, timeout=30.0) -> Notification:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        status = await _get_status(runtime, workflow_id)
        if status and predicate(status):
            return status
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out; last={await _get_status(runtime, workflow_id)}")


async def _wait_deliveries(deliver_spy, predicate, timeout=30.0) -> None:
    """Wait until the messages that went to Slack satisfy ``predicate``."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate(deliver_spy.calls):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out; sent {[call['body'] for call in deliver_spy.calls]}")


async def _replay(runtime, workflow_id) -> None:
    """Re-execute the workflow from its checkpoints, and wait until a new execution starts."""

    def _execution_claim():
        with runtime.schema_engine.connect() as connection:
            return connection.execute(
                text(
                    "SELECT started_at_epoch_ms FROM dbos.workflow_status WHERE workflow_uuid = :id"
                ),
                {"id": workflow_id},
            ).scalar_one()

    first_claim = _execution_claim()
    await DBOS.resume_workflow_async(workflow_id)
    deadline = asyncio.get_event_loop().time() + 15
    while asyncio.get_event_loop().time() < deadline:
        claim = _execution_claim()
        if claim and claim != first_claim:
            return
        await asyncio.sleep(0.1)
    raise AssertionError("workflow was never re-executed after resume")


async def _cancel(runtime, workflow_id, **kwargs) -> None:
    session = get_session(runtime.engine)
    db_session.registry.set(session)
    try:
        run = await session.get(Run, workflow_id)
        await run.cancel(**kwargs)
        await session.commit()
    finally:
        await db_session.remove()
        await session.close()


async def test_three_reports_post_one_message_that_changes_twice(
    runtime, deliver_spy, status_delay
):
    await _set_notification_channel(runtime, "C-status")

    workflow_id = await runtime.StatusFlow.start(
        subject=NotificationProbe(id=9101), rounds=[["Report 1."], ["Report 2."], ["Report 3."]]
    )
    for count in (1, 2, 3):
        await _wait_deliveries(deliver_spy, lambda calls, count=count: len(calls) == count)
        await DBOS.send_async(workflow_id, {}, topic=_NEXT)

    # The end of the run edits the message once more.
    await _wait_deliveries(deliver_spy, lambda calls: len(calls) == 4)
    posted, *edits = deliver_spy.calls
    assert (posted["channel"], posted["message_ts"], posted["body"]) == (
        "C-status",
        "",
        "Report 1.",
    )
    assert [(edit["channel"], edit["message_ts"], edit["body"]) for edit in edits] == [
        ("C-status", "1700000000.000001", "Report 2."),
        ("C-status", "1700000000.000001", "Report 3."),
        ("C-status", "1700000000.000001", "Report 3.\n\n> Finished."),
    ]
    assert all(not call["actions"] and not call["outcome"] for call in deliver_spy.calls)
    status = await _wait_status(runtime, workflow_id, lambda status: status.state == "delivered")
    assert status.message_id == "C-status:1700000000.000001"


async def test_reports_that_come_close_together_go_out_in_one_message(runtime, deliver_spy):
    await _set_notification_channel(runtime, "C-status-burst")
    await runtime.StatusFlow.start(
        subject=NotificationProbe(id=9107), rounds=[["R1.", "R2.", "R3."]]
    )

    await _wait_deliveries(deliver_spy, bool)
    await asyncio.sleep(4.0)  # room for a send of R1 or R2 that did not join

    (posted,) = deliver_spy.calls
    assert (posted["body"], posted["message_ts"]) == ("R3.", "")


async def test_a_status_post_that_fails_once_is_tried_again(runtime, deliver_spy, status_delay):
    await _set_notification_channel(runtime, "C-status-down")
    deliver_spy.failures_remaining = 1
    workflow_id = await runtime.StatusFlow.start(
        subject=NotificationProbe(id=9111), rounds=[["R1."]]
    )

    status = await _wait_status(runtime, workflow_id, lambda status: status.state == "delivered")

    refused, posted = deliver_spy.calls
    assert (refused["body"], posted["body"]) == ("R1.", "R1.")
    assert status.message_id == "C-status-down:1700000000.000002"
    # The post that failed never touched the run.
    assert (await _run_snapshot(runtime, workflow_id)).state == RunState.RUNNING


async def test_a_cancel_edits_the_status_once_more(runtime, deliver_spy, status_delay):
    await _set_notification_channel(runtime, "C-status-cancel")
    workflow_id = await runtime.EndingFlow.start(subject=NotificationProbe(id=9114))
    await _wait_deliveries(deliver_spy, lambda calls: len(calls) == 1)

    await _cancel(runtime, workflow_id, failure="Wrong note")

    await _wait_deliveries(deliver_spy, lambda calls: len(calls) == 2)
    posted, ended = deliver_spy.calls
    assert (ended["channel"], ended["message_ts"], ended["body"]) == (
        "C-status-cancel",
        "1700000000.000001",
        "Summary\n\n- Read: read\n- Gist: cancelled\n\n> Cancelled.",
    )


async def test_an_approval_is_a_message_of_its_own_and_its_answer_leaves_the_status_alone(
    runtime, deliver_spy, status_delay
):
    await _set_notification_channel(runtime, "C-status-review")
    workflow_id = await runtime.ReviewedFlow.start(subject=NotificationProbe(id=9116))
    await _wait_deliveries(deliver_spy, lambda calls: any(call["actions"] for call in calls))
    (approval,) = [
        notification
        for notification in await _notifications_for_run(runtime, workflow_id)
        if notification.reason == "gate.parked"
    ]
    await _wait_for(runtime, approval.id, lambda snapshot: snapshot["state"] == "delivered")
    # A send shows the newest status. The status posts before the answer ends the run.
    await _wait_status(runtime, workflow_id, lambda status: status.state == "delivered")

    await _respond_in_own_session(runtime, approval.correlation_token, {"control": "approve"})
    await _wait_deliveries(deliver_spy, lambda calls: calls[-1]["body"].endswith("> Finished."))

    status = await _wait_status(runtime, workflow_id, lambda status: status.state == "delivered")
    assert (await _snapshot(runtime, approval.id))["message_id"] != status.message_id
    # The post of a status carries its token, which draws no button, and its edits carry none.
    posted, *edits = [
        call for call in deliver_spy.calls if call["token"] in (status.correlation_token, None)
    ]
    assert {f"{edit['channel']}:{edit['message_ts']}" for edit in edits} == {status.message_id}
    assert all(not call["actions"] and not call["outcome"] for call in [posted, *edits])


async def test_a_replayed_run_reports_nothing_again(runtime, deliver_spy, status_delay):
    await _set_notification_channel(runtime, "C-status-replay")
    workflow_id = await runtime.StatusFlow.start(
        subject=NotificationProbe(id=9117), rounds=[["R1."]]
    )
    await _wait_deliveries(deliver_spy, lambda calls: len(calls) == 1)

    await _replay(runtime, workflow_id)
    await asyncio.sleep(3.0)  # room for a wrong report or send to land

    assert (await _get_status(runtime, workflow_id)).body == "R1."
    assert len(deliver_spy.calls) == 1
    # Two waiters now share the topic. The cancel ends the run and both waiters.
    await _cancel(runtime, workflow_id)
    await _wait_deliveries(deliver_spy, lambda calls: len(calls) == 2)
