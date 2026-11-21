import pytest
from druks.accounts.models import Account
from druks.apps import App
from druks.core.tasks import refresh_tokens
from druks.durable.enums import RunState
from druks.durable.exceptions import WorkflowError
from druks.durable.models import Run
from druks.durable.schemas import get_display_label
from druks.events.models import Event
from druks.registry import get_app_name, get_workflow
from druks.workflows import Gate, Workflow, _log_run_event, step, task
from druks_field_notes.models import Note
from sqlalchemy import select

# Every test here defines app classes; the suite keeps seeing only the installed apps.
pytestmark = pytest.mark.usefixtures("declared_apps")


def _app(name: str, package: str) -> type[App]:
    return type(name.title(), (App,), {"name": name, "package": package})


def _workflow(name: str, module: str, **attrs):
    async def run(self) -> None: ...

    return type(name, (Workflow,), {"__module__": module, "run": run, **attrs})


def test_a_gate_without_a_pinned_name_raises():
    # The name is the gate's durable identity (recv channel, the parked run's
    # gate) — it must survive a class rename, so deriving it is refused.
    with pytest.raises(WorkflowError, match="must pin"):

        class Anonymous(Gate):
            action: str = ""


def test_a_package_belongs_to_one_app():
    _app("alpha", "alpha_pkg")
    _app("alpha", "alpha_pkg")
    with pytest.raises(ValueError, match="already belongs"):
        _app("beta", "alpha_pkg")


def test_overlapping_ownership_across_owners_is_rejected():
    _app("alpha", "alpha_pkg")
    with pytest.raises(ValueError, match="overlaps"):
        _app("beta", "alpha_pkg.nested")


def test_resolution_matches_package_boundaries():
    _app("alpha", "alpha_pkg")
    assert get_app_name("alpha_pkg.workflows") == "alpha"
    with pytest.raises(LookupError):
        get_app_name("alpha_pkg_sibling.workflows")


def _task_in(module: str, body, name: str | None = None):
    body.__module__ = module
    if name:
        body.__name__ = name
    return task(body)


def test_duplicate_task_name_is_rejected():
    # DBOS only warns on a duplicate durable name and lets the last registration
    # win — enqueues of one task would silently run the other's body.
    _app("collide", "collide_pkg")

    async def clash() -> None: ...

    _task_in("collide_pkg.tasks", clash)

    async def other() -> None: ...

    with pytest.raises(WorkflowError, match="durable identity"):
        _task_in("collide_pkg.nested.tasks", other, name="clash")


def test_task_input_is_typed():
    # The signature is the wire contract — enqueue validates against it and
    # dumps to JSON, so an unannotated parameter has no wire shape.
    _app("untyped", "untyped_pkg")

    async def send(recipient) -> None: ...  # noqa: ANN001

    with pytest.raises(WorkflowError, match="needs a type"):
        _task_in("untyped_pkg.tasks", send)


def test_a_workflow_outside_every_app_fails_at_class_definition():
    # A workflow belongs to the app that owns its module, so one with no app has no kind.
    with pytest.raises(WorkflowError, match="outside every app package"):
        _workflow("Orphan", "nowhere.workflows")


def test_declaring_app_namespaces_the_kind():
    _app("alpha", "alpha_pkg")
    _app("beta", "beta_pkg")

    alpha = _workflow("Summarize", "alpha_pkg.workflows")
    beta = _workflow("Summarize", "beta_pkg.workflows")

    # Two apps can share a local workflow name without colliding on kind.
    assert (alpha.app, alpha.kind) == ("alpha", "alpha.summarize")
    assert (beta.app, beta.kind) == ("beta", "beta.summarize")


def test_explicit_kind_is_a_local_suffix():
    _app("alpha", "alpha_pkg")
    digest = _workflow("Summarize", "alpha_pkg.workflows", kind="digest")
    assert digest.kind == "alpha.digest"


def test_dotted_explicit_kind_is_rejected():
    _app("alpha", "alpha_pkg")
    with pytest.raises(WorkflowError, match="local name"):
        _workflow("Summarize", "alpha_pkg.workflows", kind="alpha.digest")


def test_in_tree_identities_are_stable():
    # These are durable DBOS names — byte-for-byte pins.
    assert get_workflow("software_factory.build") is not None
    assert get_workflow("software_factory.profile") is not None
    assert refresh_tokens.name == "core.refresh_tokens"


def test_steps_capture_the_namespaced_kind():
    # _wrap_steps closes over cls.kind after the namespace lands, so durable
    # step names carry the final kind — the replay identity.
    _app("alpha", "alpha_pkg")

    async def run_multistep(self) -> None:
        await self.ping()

    @step
    async def ping(self) -> None: ...

    flow = type(
        "Pinger",
        (Workflow,),
        {"__module__": "alpha_pkg.workflows", "run_multistep": run_multistep, "ping": ping},
    )

    captured = [cell.cell_contents for cell in flow.ping.__closure__]
    assert "alpha.pinger" in captured


async def test_lifecycle_event_stamps_the_declaring_app(druks_db):
    # The event's app derives from the run's kind through the registry —
    # never an argument, never a stored copy on the run.
    _app("alpha", "alpha_pkg")
    flow = _workflow("Beacon", "alpha_pkg.workflows")
    run = Run(
        id="wf-identity-1",
        kind=flow.kind,
        account_id=(await Account.get_or_create(druks_db, "op@example.com")).id,
    )
    druks_db.add(run)
    await druks_db.flush()

    payload = await _log_run_event(
        druks_db, run, RunState.FINISHED, {"type": "note", "id": 1}, None, None
    )

    event = (await druks_db.scalars(select(Event).filter_by(type="workflow.finished"))).one()
    assert payload["run"] == run.id
    assert event.app == "alpha"


def test_display_label_reads_the_local_kind():
    assert get_display_label("field_notes.summarize") == "Summarize"


async def test_a_declared_subject_answers_off_the_workflow_and_off_a_run():
    # One word, two answers: the workflow says what kind of thing its runs are about,
    # a run of it says which one.
    _app("alpha", "alpha_pkg")
    flow = _workflow("Sweep", "alpha_pkg.workflows", subject=Note)

    assert flow.subject is Note
    assert await flow().subject is None  # a run with no subject has none to resolve


def test_a_workflow_about_nothing_says_so_by_silence():
    _app("alpha", "alpha_pkg")
    assert _workflow("Sweep", "alpha_pkg.workflows").subject is None


@pytest.mark.parametrize("declared", ["note", None])
def test_a_subject_that_is_not_a_subject_class_is_rejected(declared):
    # ``subject = None`` included: written out it would shadow the declaration with a
    # permanent None, so a workflow about nothing writes nothing.
    _app("alpha", "alpha_pkg")
    with pytest.raises(WorkflowError, match="must be a Subject or StoredSubject"):
        _workflow("Sweep", "alpha_pkg.workflows", subject=declared)
