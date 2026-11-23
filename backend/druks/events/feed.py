from collections.abc import Iterable
from datetime import datetime
from typing import Any

from pydantic import ConfigDict, Field, ValidationInfo, computed_field, field_validator

from druks.schemas import Schema


class FeedItem(Schema):
    model_config = ConfigDict(from_attributes=True)

    # The event's monotonic log position (its pk) — the feed's ordering and
    # pagination key. ``at`` is whole-second and ties constantly; this never does.
    seq: int = Field(validation_alias="id")
    at: datetime = Field(validation_alias="created_at")
    # The event type verbatim: a lifecycle topic ("workflow.finished") or the
    # milestone an app recorded ("merged"). The words are the client's.
    topic: str = Field(validation_alias="type")
    app: str | None = None
    subject_type: str | None = None
    subject_id: str | None = None
    subject_key: str | None = None
    payload: dict[str, Any]

    @field_validator("payload")
    @classmethod
    def show_usernames(cls, payload: dict, info: ValidationInfo) -> dict:
        """A review's ask stores its approver by account id, and a gate's receipt stores
        the account that answered. The feed shows each one's username, from the
        ``usernames`` that the route reads for ``list_account_ids``."""
        usernames = info.context["usernames"]
        match payload:
            case {"input_request": {"approver_id": account_id, **ask}}:
                return {**payload, "input_request": {**ask, "approver": usernames[account_id]}}
            case {"result": {"answered_by_id": account_id, **reply}}:
                answered_by = usernames.get(account_id, "")
                return {**payload, "result": {**reply, "answered_by": answered_by}}
        return payload

    @computed_field
    @property
    def id(self) -> str:
        return f"event:{self.seq}"


def list_account_ids(payloads: Iterable[dict]) -> set[str]:
    """The accounts that the asks and receipts in ``payloads`` name by id, for
    ``show_usernames``."""
    account_ids = set()
    for payload in payloads:
        match payload:
            case {"input_request": {"approver_id": account_id}} | {
                "result": {"answered_by_id": account_id}
            } if account_id:
                account_ids.add(account_id)
    return account_ids


class FeedResponse(Schema):
    items: list[FeedItem]
    # The snapshot the live stream continues from.
    cursor: str
    # Event sequence cursor for the next (older) page; None at the tail.
    next_cursor: str | None = None


class FeedDestinations(Schema):
    is_subject_available: bool
    is_run_available: bool
    is_artifact_available: bool
