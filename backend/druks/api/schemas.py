from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from druks.durable.enums import RunState
from druks.durable.schemas import RunResponse
from druks.schemas import Schema

if TYPE_CHECKING:
    from druks.sandbox.models import SandboxIdentity


class ResumeRequest(BaseModel):
    # The operator's decision on a parked run: a control id the ask offered, an answer
    # per question (an offered option id or the operator's own words), and an optional
    # free-text note. The control is checked against the stored ask; answers and note
    # are content for the next agent prompt, never control flow.
    model_config = ConfigDict(str_strip_whitespace=True)
    control: str
    answers: dict[str, str] = Field(default_factory=dict)
    note: str = ""


class CancelRunResponse(Schema):
    run: str
    result: Literal["cancelled", "already_cancelled"]


class RetryRunResponse(Schema):
    run: str


class RunDetailResponse(Schema):
    # The run's work item; None for a run about no work item.
    subject: dict[str, str] | None
    run: RunResponse


class SecretIssueResponse(Schema):
    # One answer of Druks to a box for a secret. It holds no secret value.
    issued_at: datetime
    # The first 8 hex characters of the SHA-256 of the token. Empty for a value with
    # no expiry.
    fingerprint: str
    expires_at: datetime | None
    # The vault row that keeps the token, and when Druks wrote it. Empty for a token
    # that the vault does not keep.
    audience: str
    minted_at: datetime | None
    # Why the box got no token.
    error: str


class SandboxSecretResponse(Schema):
    name: str
    # The vault row that answers for the secret, and its kind.
    secret_id: str
    kind: str
    # What the token covers, such as repositories.
    resources: list[str]
    # The answers that changed, oldest first.
    issues: list[SecretIssueResponse]


class SandboxResponse(Schema):
    # One box of a run and the secrets that it fetches from Druks. A pasted provider
    # key is not one of them: Druks gives it to the box at creation.

    # "workflow" for the box that a run keeps for all its steps. A step name otherwise.
    scoped_to: str
    is_live: bool
    secrets: list[SandboxSecretResponse]

    @classmethod
    def from_identity(cls, identity: "SandboxIdentity") -> "SandboxResponse":
        return cls(
            scoped_to=identity.scoped_to,
            # Druks stores an identity before its box exists, so a live box also has a host.
            is_live=bool(identity.host_id) and identity.is_live,
            secrets=[
                SandboxSecretResponse(
                    name=ref.name,
                    secret_id=ref.secret_id,
                    kind=ref.secret.kind,
                    resources=ref.resources,
                    issues=[
                        SecretIssueResponse.model_validate(issue, from_attributes=True)
                        for issue in ref.issues
                    ],
                )
                for ref in sorted(identity.secret_refs, key=lambda ref: ref.name)
            ],
        )


class OpenWorkflowResponse(Schema):
    app: str
    state: RunState
    run: str = Field(description="What get_gate, cancel_run, and retry_run take.")
    latest_agent_call: str | None = Field(
        description="What get_agent_call takes; null before the first call."
    )
    failure: str | None
    created_at: datetime


class OpenSubjectResponse(Schema):
    subject_type: str
    subject_id: str
    subject_key: str
    workflows: list[OpenWorkflowResponse]


class OpenSubjectsResponse(Schema):
    subjects: list[OpenSubjectResponse]


class DashboardRun(Schema):
    app: str
    run: str
    kind: str
    state: RunState
    subject_type: str | None
    subject_id: str | None
    subject_key: str | None
    updated_at: datetime
    parked_at: datetime | None
    request_label: str | None
    artifact_title: str | None
    presentation: str | None
    request_url: str | None
    failure: str | None


class DashboardSection(Schema):
    total: int
    rows: list[DashboardRun]


class DashboardOverview(Schema):
    needs_you: DashboardSection
    running: DashboardSection
    failed: DashboardSection
    last_finished_at: datetime | None
    last_failed_at: datetime | None


class ScheduledRun(Schema):
    model_config = ConfigDict(from_attributes=True)

    run: str
    status: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class DashboardSchedule(Schema):
    app: str
    kind: str
    cron: str | None
    default_cron: str
    enabled: bool
    timezone: str
    next_run_at: datetime | None
    runs: list[ScheduledRun]


class DashboardSchedules(Schema):
    rows: list[DashboardSchedule]


class ArtifactContent(Schema):
    # A call's renderable output, served to the in-app review so it can show the
    # plan (or other markdown) beside its controls.
    kind: str
    title: str
    content: str
