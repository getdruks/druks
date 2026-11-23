import contextlib

from dbos import DBOS
from pydantic_core import to_json
from sqlalchemy.ext.asyncio import AsyncSession

from druks import ui
from druks.accounts.models import Account
from druks.chat.constants import CLICK_ANSWER_MESSAGE
from druks.chat.forms import get_answer
from druks.chat.models import Conversation
from druks.chat.service import deliver
from druks.core.services import Slack
from druks.durable.enums import RunState
from druks.durable.exceptions import ArtifactContentMissing
from druks.durable.models import Artifact, Run
from druks.durable.reads import read_ask
from druks.exceptions import ObjectNotFound
from druks.notifications.constants import (
    APPROVER_ONLY_MESSAGE,
    NOTE_FIELD,
    NOTE_REQUIRED_MESSAGE,
    QUESTION_FIELD_PREFIX,
    TEAM_ONLY_MESSAGE,
)
from druks.notifications.exceptions import (
    AlreadyAcknowledgedError,
    AnswerNotAllowedError,
    InvalidChoiceError,
    StaleRoundError,
    UnknownTokenError,
)
from druks.notifications.models import Notification
from druks.registry import get_app
from druks.teams.models import Team
from druks.ui.blocks import iter_parts
from druks.ui.page import get_route_path, list_route_parameters


async def list_notification_channels() -> list[dict]:
    """The channels that a person can choose for their notifications, by name. The bot
    posts only in a channel that it is a member of."""
    client = await Slack.get_client()
    return sorted(await client.list_member_channels(), key=lambda channel: channel["name"])


async def get_rendered_ask(session: AsyncSession, run: Run) -> dict:
    """The ask that ``run`` waits on, as the content of its notification. The blocks are the
    ask in order. The body is their text form. Each action is a control that a click can
    send. Delivery makes its button. Each field is an input for a reply field that the gate
    takes: one for each question, and one for the note."""
    ask = await read_ask(session, run)
    # An external ask has no questions, controls or artifact. Only this branch thus reads them.
    if ask["presentation"] == "in_app":
        blocks = [ui.Markdown(f"# {ask['label']}")]
        context = ask.get("context", "")
        if stack := ui.Stack.from_context(context):
            app = get_app(run.app)
            pages = app.pages()
            # A link names a page or a subject of the run's app. A channel needs its
            # address. The address replaces the page or the subject. The stored
            # link thus names one destination.
            for link in iter_parts(stack.blocks, ui.Link):
                if address := link.get_address(app.name, pages):
                    link.url, link.page, link.arguments, link.subject = address, "", {}, None
            blocks += stack.blocks
        elif context:
            blocks.append(ui.Markdown(context))
        for question in ask["questions"]:
            lines = [question["prompt"]]
            for option in question["options"]:
                mark = " (recommended)" if option.get("recommended") else ""
                lines.append(f"- {option['label']}{mark}")
            blocks.append(ui.Markdown("\n".join(lines)))
        if ask["artifact_id"]:
            artifact = await session.get(Artifact, ask["artifact_id"])
            with contextlib.suppress(ArtifactContentMissing):
                blocks.append(ui.Markdown(await artifact.read_content()))
        actions = [
            {"id": control["id"], "label": control["id"].replace("_", " ").capitalize()}
            for control in ask["controls"]
        ]
        fields = []
        if "answers" in ask["reply_fields"]:
            for question in ask["questions"]:
                name = f"{QUESTION_FIELD_PREFIX}{question['id']}"
                options = [
                    ui.Option(option["label"], value=option["id"]) for option in question["options"]
                ]
                # A question without options takes the person's own words.
                if options:
                    field = ui.SelectField(name=name, label=question["prompt"], options=options)
                else:
                    field = ui.TextAreaField(name=name, label=question["prompt"])
                fields.append(field.model_dump())
        if "note" in ask["reply_fields"]:
            fields.append(ui.TextAreaField(name=NOTE_FIELD, label="Note").model_dump())
        return {
            "body": ui.Stack(blocks).to_markdown(),
            # Each subject status keeps the status that the ask read, as the body does.
            "blocks": [block.model_dump(mode="json", by_alias=True) for block in blocks],
            "actions": actions,
            "fields": fields,
            "deep_link": None,
        }
    # A person answers an external ask on its source. It thus has no actions and no fields.
    # Its link is the one that the author of its gate declares.
    return {
        "body": ask["label"],
        "blocks": None,
        "actions": None,
        "fields": None,
        "deep_link": ask.get("url"),
    }


async def create_park_notification(
    session: AsyncSession,
    run: Run,
    subject: dict,
    *,
    account_id: str | None = None,
    conversation_id: str | None = None,
    deep_link: str | None = None,
) -> str:
    """Create the notification of the question that ``run`` parked on, for the account
    or in the conversation, and return its id. The caller queues its delivery. The
    notification keeps the run and the time that it parked. Druks thus refuses a click on an
    old button after the run parks again."""
    rendered = await get_rendered_ask(session, run)
    notification = await Notification.create(
        session,
        account_id=account_id,
        conversation_id=conversation_id,
        reason="gate.parked",
        body=rendered["body"],
        blocks=rendered["blocks"],
        subject=subject,
        actions=rendered["actions"],
        fields=rendered["fields"],
        run_id=run.id,
        run_parked_at=run.input_requested_at,
        deep_link=rendered["deep_link"] or deep_link,
    )
    return notification.id


async def get_action_request(
    session: AsyncSession, token: str, position: list[int], *, user_id: str, values: dict
) -> dict:
    """The request that a click by ``user_id`` sends for the action at ``position`` in the
    blocks of the notification of ``token``. It holds the notification, and the method, the
    path and the JSON body of the operation's route, as the dashboard sends them. ``values``
    holds the answers in the inputs of the block of the action, keyed by field name. Raises when
    the action cannot run, or when a field refuses its answer."""
    notification = await Notification.get_for_token(session, token)
    if not notification:
        raise UnknownTokenError()
    action = notification.get_action_at(position)
    if notification.is_acknowledged:
        raise AlreadyAcknowledgedError()
    # The click must be for the round of the notification. The read thus gets the run
    # from the database, not from the identity map.
    run = await session.get(Run, notification.run_id, populate_existing=True)
    if run.state != RunState.PARKED.value or run.input_requested_at != notification.run_parked_at:
        raise StaleRoundError()
    if notification.conversation_id:
        conversation = await session.get(Conversation, notification.conversation_id)
        if user_id != conversation.user_id:
            raise AnswerNotAllowedError(
                "Only the person of this conversation can run its actions. Ask that person "
                "to run this action."
            )
    if not run.workflow:
        raise ObjectNotFound("run", {"id": run.id})
    app = get_app(run.app)
    # The app can lose the route after the run parks.
    operation = app.operations().get(action["operation"])
    if not operation:
        raise InvalidChoiceError(
            f"The app {app.name} has no route for this action at this time. Ask an admin to "
            "add the route to the app again."
        )
    match position:
        case [index, _]:
            # A form keeps the fields that each of its actions sends.
            declared = ui.Form.model_validate(notification.blocks[index]).fields
        case _:
            # An action keeps its own, alone or in a cell of a table.
            declared = ui.Action.model_validate(action).fields
    fields = [field.model_dump() for field in declared]
    # Each field refuses an answer as it does in a form of the ask, before anything runs.
    get_answer(fields, values)
    # The arguments come first, then the answers, as the dashboard sends them.
    payload = dict(action["arguments"])
    for field in fields:
        value = values.get(field["name"])
        if field["field"] == "number":
            # The dashboard sends the number, whole when it is whole, or null for no number.
            text = (value or "").strip()
            value = None
            if text:
                number = float(text)
                value = int(number) if number.is_integer() else number
        payload[field["name"]] = value
    parameters = list_route_parameters(operation.path)
    # A number is a value even when it is 0. A client removes a segment of one or two dots
    # from a path. Another route then answers. Neither is thus a value.
    if missing_names := [name for name in parameters if payload.get(name) in (None, "", ".", "..")]:
        raise InvalidChoiceError(
            f"This action carries no path value for {', '.join(missing_names)}. Give the "
            "action an argument or a required field with each of these names."
        )
    return {
        "notification": notification,
        "method": operation.method,
        "path": get_route_path(operation.path, payload),
        "body": {name: value for name, value in payload.items() if name not in parameters},
    }


async def get_offered_control(ask: dict, control: str) -> dict:
    """The control that the ask offers under the id ``control``."""
    for offered in ask.get("controls", []):
        if offered["id"] == control:
            return offered
    raise InvalidChoiceError(
        f"The ask offers no control {control!r}. Answer with a control that it offers."
    )


async def validate_in_app_answer(
    session: AsyncSession,
    run: Run,
    *,
    account_id: str | None,
    control: str,
    answers: dict[str, str],
    note: str,
    has_note_input: bool = False,
) -> dict:
    # The in-app answer contract, shared by the runs resume route, the answer_gate
    # tool, and the respond path of notifications. Only the approver answers a review
    # that names one. Otherwise, only the admin of an app's channel answers a question
    # that a chat on that channel asked. Only the team and the admins answer a run of an
    # app that a team lists.
    #
    # The control must be one that the ask offered. The workflow owns the vocabulary.
    # Druks never reads it from the client. A spoofed control thus cannot change the
    # control flow. The answering account comes from the identity of the caller, never
    # from the client.
    account = await session.get(Account, account_id) if account_id else None
    if approver_id := run.input_request.get("approver_id"):
        if account_id != approver_id:
            approver = await session.get(Account, approver_id)
            raise AnswerNotAllowedError(
                APPROVER_ONLY_MESSAGE.format(approver=approver.username), link=run.get_link()
            )
    elif run.conversation_id:
        conversation = await session.get(Conversation, run.conversation_id)
        if not conversation.is_answerable_by(account_id):
            raise AnswerNotAllowedError(
                "Only the admin of the number that started this run can answer it. Ask that "
                "admin to answer it."
            )
    workflow = run.workflow
    if not workflow:
        raise ObjectNotFound("run", {"id": run.id})
    app = get_app(workflow.app)
    # A person with no account is on no team.
    if await Team.exists_for_app(session, app) and not (account and await account.can_use_app(app)):
        raise AnswerNotAllowedError(TEAM_ONLY_MESSAGE.format(app=app.name), link=run.get_link())
    ask = await run.get_ask()
    offered = await get_offered_control(ask, control)
    if offered["note_required"] and not note.strip():
        # A person who sees the note input writes the note. Elsewhere, an answer replaces it.
        if has_note_input:
            raise InvalidChoiceError(NOTE_REQUIRED_MESSAGE)
        if not answers:
            raise InvalidChoiceError(
                f"{control} needs an answer or a note to guide the next plan. Answer a "
                "question, or write a note."
            )
    # Answers can be an offered option id or the operator's own words. Free text is
    # content for the next agent prompt. Druks thus checks only that each question id is
    # in the ask.
    #
    # A blank answer is a client bug. The check strips the answer. The HTTP models
    # normalize whitespace. This validator is also the boundary of a direct call, such
    # as the Slack path.
    asked = {question["id"] for question in ask.get("questions", [])}
    for question_id, answer in answers.items():
        if question_id not in asked:
            raise InvalidChoiceError(
                f"The answer to {question_id!r} matches no open question. Answer only the "
                "questions of the ask."
            )
        if not answer.strip():
            raise InvalidChoiceError(
                f"The answer to {question_id!r} is blank. Write an answer, or remove the "
                "question from the answers."
            )
    return {
        "action": control,
        "answers": answers,
        "note": note,
        "answered_by_id": account_id or "",
    }


async def respond_to_notification(
    session: AsyncSession,
    token: str,
    choice: dict,
    *,
    user_id: str | None,
    account_id: str | None,
    values: dict | None = None,
    has_inputs: bool = False,
) -> Notification:
    """Answer the notification with a click by ``user_id``, whose account is
    ``account_id``, or with a link, which names nobody. In a chat, only its person answers.
    A click on the question of the chat's agent goes to the agent as the person's message.
    So does a click on a run's control that needs more, when its message shows no inputs.
    The chat's channel then has none, or the notification holds none.

    A click carries ``values``, the answers in the inputs of its message keyed by field name.
    A form takes them only when its fields take them. With ``has_inputs``, the click
    comes from a channel that shows a run's inputs. The answers and the note are then
    those of the inputs that the notification holds. When it holds a note input, a
    control that needs a note takes no empty note."""
    notification = await Notification.get_for_token(session, token)
    if not notification:
        raise UnknownTokenError()
    if notification.is_acknowledged:
        raise AlreadyAcknowledgedError()
    run, ask = None, {}
    if notification.run_id:
        # The notification keeps the round that Druks sent it for. The answer must be for
        # the live round of the run. The read thus gets the run from the database, not from
        # the identity map.
        run = await session.get(Run, notification.run_id, populate_existing=True)
        if (
            run.state != RunState.PARKED.value
            or run.input_requested_at != notification.run_parked_at
        ):
            raise StaleRoundError()
        ask = await run.get_ask()
        if ask.get("presentation") != "in_app":
            # A person answers an external gate on its source (a PR review, a ticket
            # comment), through the webhook paths, never through this path. An
            # ask that does not declare in_app takes no answer here either. get_ask
            # dispatches on the same declared key.
            raise InvalidChoiceError("This notification is informational. Answer it on its source.")
    elif not notification.conversation_id or not (notification.actions or notification.fields):
        # Without a run, only the question of a chat's agent takes an answer.
        raise InvalidChoiceError(
            "This notification takes no answer. Answer only a notification that offers a "
            "choice or a field."
        )
    control = choice["control"]
    if notification.conversation_id:
        conversation = await session.get(Conversation, notification.conversation_id)
        # A link names nobody. It thus answers no chat.
        if not user_id or user_id != conversation.user_id:
            raise AnswerNotAllowedError(
                "Only the person of this conversation can answer it. Ask that person to answer it."
            )
        # Druks checks the answers before the claim. A wrong answer thus leaves the form open.
        if not run and notification.fields:
            answer = get_answer(notification.fields, values or {})
        else:
            answer = notification.get_action(control)["label"]
        if not run or (
            not (conversation.channel.has_inputs and notification.fields)
            and ((await get_offered_control(ask, control))["note_required"] or ask["questions"])
        ):
            # A click on the agent's question goes to the chat's agent as the person's
            # message. So does a click on a control that needs more, from a message with no
            # inputs.
            if not await notification.mark_acknowledged():
                raise AlreadyAcknowledgedError()
            await conversation.create_message(session, answer)
            await session.commit()
            await DBOS.start_workflow_async(deliver, conversation.id)
            return notification
        # The person of the chat answers its run as the chat's account.
        account_id = conversation.account_id
    answers, note = choice.get("answers", {}), choice.get("note", "")
    has_note_input = False
    if has_inputs:
        names = [field["name"] for field in notification.fields or []]
        answers = {
            name.removeprefix(QUESTION_FIELD_PREFIX): value
            for name in names
            if name.startswith(QUESTION_FIELD_PREFIX) and (value := values.get(name, "").strip())
        }
        has_note_input = NOTE_FIELD in names
        if has_note_input:
            note = values.get(NOTE_FIELD, "").strip()
    resume_payload = await validate_in_app_answer(
        session,
        run,
        account_id=account_id,
        control=control,
        answers=answers,
        note=note,
        has_note_input=has_note_input,
    )
    # Claim first. After the run resumes, close_notifications closes each message of the
    # question that is still open. This one must show the click instead.
    if not await notification.mark_acknowledged():
        raise AlreadyAcknowledgedError()
    await run.resume(**resume_payload)
    if notification.conversation_id:
        # Only the chat's person answers in its thread, and the message says so.
        answer = {key: value for key, value in resume_payload.items() if key != "answered_by_id"}
        await conversation.create_message(
            session,
            CLICK_ANSWER_MESSAGE.format(run=run.id, answer=to_json(answer).decode()),
            is_internal=True,
        )
    return notification
