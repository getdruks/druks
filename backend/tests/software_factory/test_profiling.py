from types import SimpleNamespace

import pytest
from conftest import connect_service
from druks.contrib.software_factory.app import SoftwareFactory
from druks.contrib.software_factory.exceptions import SandboxSetupMissing
from druks.contrib.software_factory.models import Project, ProjectRepo
from druks.contrib.software_factory.policy import (
    RepoPolicy,
    VerificationProfile,
    VerificationSandbox,
)
from druks.contrib.software_factory.workflows import Profile
from druks.db import db_session
from druks.durable.engine import configure_engine
from druks.sandbox import Sandbox, TemplateUnavailable
from druks.services.exceptions import ServiceNotConnectedError
from druks.skills.datastructures import InstalledSkill
from druks.skills.models import SkillCollection
from sqlalchemy import update


@pytest.fixture(autouse=True)
async def _passthrough_step(monkeypatch, druks_db):
    # run() is itself a durable step (single-operation workflow) — route it
    # straight through so the test needs no live DBOS runtime.
    configure_engine(await druks_db.connection())

    async def _run_step(_options, fn):
        return await fn()

    monkeypatch.setattr("druks.workflows.DBOS.run_step_async", _run_step)
    yield
    configure_engine(None)


async def _seed_repo() -> ProjectRepo:
    project = await Project.create(name="Acme")
    return await ProjectRepo.create(project_id=project.id, full_name="acme/widget")


async def _seed_skills(*names: str, disabled: tuple[str, ...] = ()) -> None:
    collection = await SkillCollection.create(
        db_session(),
        source="test",
        name="test skills",
        skills=[
            InstalledSkill(name=name, description=f"{name} skill", path=name, content_hash="x")
            for name in names
        ],
    )
    for skill in collection.skills:
        if skill.name in disabled:
            skill.enabled = False


def _profiled(**overrides) -> dict:
    baseline = {
        "languages": ["python"],
        "frameworks": ["django"],
        "package_managers": ["uv"],
        "stack_summary": "A Django backend.",
        "verification": {
            "test_commands": [{"command": "pytest", "ci_check": "Backend / tests"}],
            "lint_commands": [{"command": "ruff check .", "ci_check": "Backend / lint"}],
            "typecheck_commands": [],
        },
        "recommended_skills": ["django-patterns"],
    }
    baseline.update(overrides)
    return {"purpose": "The billing API.", "baseline": baseline}


async def _no_policy(repo):
    return RepoPolicy()


@pytest.mark.parametrize("refresh_only", [False, True])
async def test_dispatch_shapes_the_profile_start(druks_db, monkeypatch, refresh_only):
    await connect_service(
        "github",
        identity={"app_id": "1", "slug": "druks-operator"},
        secrets={"private_key": "operator-pem", "webhook_secret": "hook-secret"},
    )
    repo = await _seed_repo()
    calls: list[dict] = []

    async def _start(cls, **kwargs):
        calls.append(kwargs)
        return "profile-run"

    monkeypatch.setattr(Profile, "start", classmethod(_start))

    run_id = await Profile.dispatch(repo, refresh_only=refresh_only)

    assert run_id == "profile-run"
    assert calls == [{"subject": repo, "repo_id": repo.id, "refresh_only": refresh_only}]


async def test_dispatch_refuses_before_start_without_github(druks_db, monkeypatch):
    repo = await _seed_repo()

    async def _start(cls, **kwargs):
        raise AssertionError("start must not be reached without a GitHub identity")

    monkeypatch.setattr(Profile, "start", classmethod(_start))

    with pytest.raises(ServiceNotConnectedError, match="github is not connected"):
        await Profile.dispatch(repo)


class TestProfileRun:
    async def test_persists_baseline_and_effective(self, druks_db, monkeypatch):
        await _seed_skills("django-patterns")
        repo = await _seed_repo()

        async def _profiler(*, repo: str):
            return _profiled()

        monkeypatch.setattr(SoftwareFactory, "repo_profiler", _profiler)
        monkeypatch.setattr(RepoPolicy, "resolve", staticmethod(_no_policy))

        await Profile().run(repo_id=repo.id)
        # The step commits on its own session; the identity-mapped `repo` is
        # stale across that boundary.
        await db_session().refresh(repo)

        assert repo.profile["baseline"]["languages"] == ["python"]
        assert repo.purpose == "The billing API."
        assert repo.effective_profile["verification"]["lint_commands"] == [
            {"command": "ruff check .", "ci_check": "Backend / lint"}
        ]

    async def test_keeps_a_typed_purpose(self, druks_db, monkeypatch):
        repo = await _seed_repo()

        async def _profiler(*, repo: str):
            # The edit route saves from another session, so the loaded row is not updated.
            await db_session().execute(
                update(ProjectRepo)
                .where(ProjectRepo.full_name == repo)
                .values(purpose="The web app")
                .execution_options(synchronize_session=False)
            )
            return _profiled()

        monkeypatch.setattr(SoftwareFactory, "repo_profiler", _profiler)
        monkeypatch.setattr(RepoPolicy, "resolve", staticmethod(_no_policy))

        await Profile().run(repo_id=repo.id)
        await db_session().refresh(repo)

        assert repo.purpose == "The web app"

    async def test_drops_skills_that_are_not_enabled(self, druks_db, monkeypatch):
        await _seed_skills("django-patterns", "retired-skill", disabled=("retired-skill",))
        repo = await _seed_repo()

        async def _profiler(*, repo: str):
            # The agent picked a disabled skill and one that was never real.
            return _profiled(
                recommended_skills=["django-patterns", "retired-skill", "made-up-skill"]
            )

        monkeypatch.setattr(SoftwareFactory, "repo_profiler", _profiler)
        monkeypatch.setattr(RepoPolicy, "resolve", staticmethod(_no_policy))

        await Profile().run(repo_id=repo.id)
        await db_session().refresh(repo)

        assert repo.profile["baseline"]["recommended_skills"] == ["django-patterns"]

    async def test_a_pinned_list_replaces_only_that_list(self, druks_db, monkeypatch):
        repo = await _seed_repo()

        async def _profiler(*, repo: str):
            return _profiled()

        async def _pinning_policy(repo):
            return RepoPolicy(verification=VerificationProfile(test_commands=("make test",)))

        monkeypatch.setattr(SoftwareFactory, "repo_profiler", _profiler)
        monkeypatch.setattr(RepoPolicy, "resolve", staticmethod(_pinning_policy))

        await Profile().run(repo_id=repo.id)
        await db_session().refresh(repo)

        # The list that the operator wrote replaces its detected list...
        assert repo.effective_profile["verification"]["test_commands"] == [
            {"command": "make test", "ci_check": None}
        ]
        # ...and the lists that the operator left out stay as detected.
        assert repo.effective_profile["verification"]["lint_commands"] == [
            {"command": "ruff check .", "ci_check": "Backend / lint"}
        ]
        # The detected baseline is preserved underneath.
        assert repo.profile["baseline"]["verification"]["test_commands"] == [
            {"command": "pytest", "ci_check": "Backend / tests"}
        ]

    async def test_an_empty_pinned_list_removes_the_detected_commands(self, druks_db, monkeypatch):
        repo = await _seed_repo()

        async def _profiler(*, repo: str):
            return _profiled()

        async def _pinning_policy(repo):
            return RepoPolicy(verification=VerificationProfile(lint_commands=()))

        monkeypatch.setattr(SoftwareFactory, "repo_profiler", _profiler)
        monkeypatch.setattr(RepoPolicy, "resolve", staticmethod(_pinning_policy))

        await Profile().run(repo_id=repo.id)
        await db_session().refresh(repo)

        assert repo.effective_profile["verification"]["lint_commands"] == []
        assert repo.effective_profile["verification"]["test_commands"] == [
            {"command": "pytest", "ci_check": "Backend / tests"}
        ]

    async def test_pinned_command_keeps_the_check_detected_for_it(self, druks_db, monkeypatch):
        repo = await _seed_repo()

        async def _profiler(*, repo: str):
            return _profiled()

        async def _pinning_policy(repo):
            return RepoPolicy(
                verification=VerificationProfile(test_commands=("pytest", "make e2e"))
            )

        monkeypatch.setattr(SoftwareFactory, "repo_profiler", _profiler)
        monkeypatch.setattr(RepoPolicy, "resolve", staticmethod(_pinning_policy))

        await Profile().run(repo_id=repo.id)
        await db_session().refresh(repo)

        assert repo.effective_profile["verification"]["test_commands"] == [
            {"command": "pytest", "ci_check": "Backend / tests"},
            {"command": "make e2e", "ci_check": None},
        ]


class TestRefreshOnly:
    async def test_skips_the_agent_and_reapplies_the_pin(self, druks_db, monkeypatch):
        repo = await _seed_repo()
        baseline = _profiled()["baseline"]
        await repo.set_profile(baseline=baseline, effective=baseline)

        async def _boom(*, repo: str):
            raise AssertionError("refresh_only must not call the repo profiler")

        async def _pinning_policy(repo):
            return RepoPolicy(verification=VerificationProfile(test_commands=("make test",)))

        monkeypatch.setattr(SoftwareFactory, "repo_profiler", _boom)
        monkeypatch.setattr(RepoPolicy, "resolve", staticmethod(_pinning_policy))

        await Profile().run(repo_id=repo.id, refresh_only=True)
        await db_session().refresh(repo)

        # Baseline untouched — only the pin re-applies.
        assert repo.profile["baseline"]["verification"]["test_commands"] == [
            {"command": "pytest", "ci_check": "Backend / tests"}
        ]
        assert repo.effective_profile["verification"]["test_commands"] == [
            {"command": "make test", "ci_check": None}
        ]


SETUP = ".druks/software_factory/setup.sh"


def _sandbox_policy(monkeypatch) -> None:
    async def _policy(repo):
        return RepoPolicy(
            verification=VerificationProfile(sandbox=VerificationSandbox(setup=SETUP))
        )

    monkeypatch.setattr(RepoPolicy, "resolve", staticmethod(_policy))


def _repo_files(
    monkeypatch, *, setup: str | None = "#!/bin/bash\napt-get install -y make\n", lockfiles=None
):
    """The default branch of the repo: its setup script, and the lockfiles at its root."""

    async def _fetch_file(*, repo, path):
        return setup

    async def _get_file_content(repo, path):
        return (lockfiles or {}).get(path)

    async def _client(repo):
        return SimpleNamespace(get_file_content=_get_file_content)

    async def _profiler(*, repo):
        return _profiled()

    monkeypatch.setattr("druks.contrib.software_factory.workflows.fetch_file", _fetch_file)
    monkeypatch.setattr("druks.contrib.software_factory.workflows.Github.get_client", _client)
    monkeypatch.setattr(SoftwareFactory, "repo_profiler", _profiler)


def _templates(monkeypatch, *outcomes):
    """``Sandbox.build`` answers with each of ``outcomes`` in turn: a template id, or an
    exception to raise. Returns the (script, label) of every call."""
    answers = iter(outcomes)
    calls = []

    async def _build(self, *, label):
        calls.append((self.read_setup_script().decode(), label))
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(id=answer)

    monkeypatch.setattr(Sandbox, "build", _build)
    return calls


class TestSandboxTemplate:
    async def test_a_setup_script_builds_the_repos_template(self, druks_db, monkeypatch):
        repo = await _seed_repo()
        _sandbox_policy(monkeypatch)
        _repo_files(monkeypatch)
        calls = _templates(monkeypatch, "template-1")

        await Profile().run(repo_id=repo.id)
        await db_session().refresh(repo)

        assert calls == [("#!/bin/bash\napt-get install -y make\n", f"acme/widget:{SETUP}")]
        assert repo.effective_profile["sandbox_template"] == "template-1"
        assert "sandbox_template" not in repo.profile["baseline"]

    async def test_a_repo_without_a_sandbox_builds_no_template(self, druks_db, monkeypatch):
        repo = await _seed_repo()
        _repo_files(monkeypatch)
        calls = _templates(monkeypatch)

        await Profile().run(repo_id=repo.id)
        await db_session().refresh(repo)

        assert calls == []
        assert "sandbox_template" not in repo.effective_profile

    async def test_a_changed_lockfile_changes_the_script_that_builds_the_template(
        self, druks_db, monkeypatch
    ):
        repo = await _seed_repo()
        _sandbox_policy(monkeypatch)
        calls = _templates(monkeypatch, *["template-1"] * 4)

        for lockfiles in ({"uv.lock": "a"}, {"uv.lock": "a"}, {"uv.lock": "b"}, {}):
            _repo_files(monkeypatch, lockfiles=lockfiles)
            await Profile().run(repo_id=repo.id)

        scripts = [script for script, _ in calls]
        assert scripts[0] == scripts[1]
        assert scripts[0] != scripts[2]
        assert all("# lockfiles " in script for script in scripts[:3])
        assert scripts[3] == "#!/bin/bash\napt-get install -y make\n"

    async def test_a_missing_setup_script_fails_loudly(self, druks_db, monkeypatch):
        repo = await _seed_repo()
        _sandbox_policy(monkeypatch)
        _repo_files(monkeypatch, setup=None)
        _templates(monkeypatch)

        with pytest.raises(SandboxSetupMissing, match=f"acme/widget has no {SETUP}"):
            await Profile().run(repo_id=repo.id)

    async def test_a_script_that_does_not_build_keeps_the_last_good_template(
        self, druks_db, monkeypatch
    ):
        repo = await _seed_repo()
        _sandbox_policy(monkeypatch)
        _repo_files(monkeypatch)
        _templates(monkeypatch, "template-1", TemplateUnavailable("no such package"))

        await Profile().run(repo_id=repo.id)
        await Profile().run(repo_id=repo.id)
        await db_session().refresh(repo)

        assert repo.effective_profile["sandbox_template"] == "template-1"

    async def test_a_first_build_that_fails_leaves_no_template_and_still_profiles(
        self, druks_db, monkeypatch
    ):
        repo = await _seed_repo()
        _sandbox_policy(monkeypatch)
        _repo_files(monkeypatch)
        _templates(monkeypatch, TemplateUnavailable("no such package"))

        await Profile().run(repo_id=repo.id)
        await db_session().refresh(repo)

        assert "sandbox_template" not in repo.effective_profile
        assert repo.effective_profile["languages"] == ["python"]
