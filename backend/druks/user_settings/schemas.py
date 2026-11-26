from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, PositiveInt
from pydantic.fields import FieldInfo

from druks.accounts.enums import Theme
from druks.apps.settings import (
    field_choices,
    field_kind,
    field_multiline,
    field_section,
    field_visibility,
    validate_field_choice_details,
)
from druks.chat.enums import BotAccess
from druks.core.utils.time import validate_timezone
from druks.harnesses.datastructures import Billing
from druks.harnesses.schemas import SortedNames
from druks.schemas import Schema

from .datastructures import Effort


class HarnessResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    name: str
    provider: str | None
    billing_options: SortedNames
    # Chat runs on a harness that has an ACP adapter.
    has_chat: Annotated[bool, BeforeValidator(bool)] = Field(validation_alias="adapter_command")


class PersonalSettingsResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    timezone: str
    theme: Theme
    notification_channel_id: str | None


class SettingsResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    updated_at: datetime
    default_harness: str
    default_model: str
    default_billing: str
    default_effort: str
    fast_mode: bool
    include_apps: bool
    default_timeout: int


class UpdatePersonalSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: Annotated[str, AfterValidator(validate_timezone)] | None = None
    theme: Theme | None = None
    # Absent leaves the channel unchanged. Null sends notifications by direct message.
    notification_channel_id: str | None = Field(
        default=None, validation_alias="notificationChannelId"
    )


class UpdateSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_harness: str | None = Field(default=None, validation_alias="defaultHarness")
    default_model: str | None = Field(default=None, validation_alias="defaultModel")
    default_billing: Billing | None = Field(default=None, validation_alias="defaultBilling")
    default_effort: Effort | None = Field(default=None, validation_alias="defaultEffort")
    fast_mode: bool | None = Field(default=None, validation_alias="fastMode")
    include_apps: bool | None = Field(default=None, validation_alias="includeApps")
    default_timeout: PositiveInt | None = Field(default=None, validation_alias="defaultTimeout")


Source = Literal["override", "default"]


class InstructionSettingResponse(Schema):
    name: str
    label: str
    description: str
    harness: str
    harness_source: Source
    model: str
    source: Source
    billing: str
    billing_source: Source
    effort: str
    effort_source: Source
    timeout: int
    timeout_source: Literal["override", "declared", "default"]


class SettingsFieldResponse(Schema):
    name: str
    label: str
    help: str
    type: str
    # A secret field sends neither its value nor its default. secret_set says whether it is set.
    value: Any
    default: Any
    # The allowed values of an enum field.
    choices: list[str] | None
    choice_details: dict[str, dict[str, str]] = {}
    section: str
    visible_when_field: str
    visible_when_values: list[Any]
    secret_set: bool | None
    multiline: bool = False
    overridden: bool
    # A secret file sets the field, so the settings form cannot change or clear it.
    managed: bool = False

    @classmethod
    def from_field(
        cls, name: str, field: FieldInfo, *, value: Any, overridden: bool, managed: bool
    ) -> "SettingsFieldResponse":
        kind = field_kind(field)
        secret = kind == "secret"
        controller, targets = field_visibility(field)
        return cls(
            name=name,
            label=field.title or name,
            help=field.description or "",
            type=kind,
            value=None if secret else value,
            default=None if secret else field.default,
            choices=field_choices(field),
            choice_details=validate_field_choice_details(field),
            section=field_section(field),
            visible_when_field=controller,
            visible_when_values=targets,
            secret_set=bool(value) if secret else None,
            multiline=field_multiline(field),
            overridden=overridden,
            managed=managed,
        )


class WorkflowSettingsResponse(Schema):
    kind: str
    fields: list[SettingsFieldResponse]


class AppSettingsResponse(Schema):
    name: str
    description: str
    # A Lucide icon name.
    icon: str
    # The instructions of a built-in app show under the Druks tab.
    builtin: bool
    # The id of the app's Bot, which answers people on its channels.
    bot: str | None
    bot_access: BotAccess | None
    instructions: list[InstructionSettingResponse]
    workflows: list[WorkflowSettingsResponse]
    settings: list[SettingsFieldResponse]


class AppsSettingsResponse(Schema):
    allowed_efforts: list[str]
    apps: list[AppSettingsResponse]
    # The chat channels a Bot answers on now: the registered ones whose service is connected.
    channels: list[str]


class AppsSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Each map is instruction or bot id to value. Null inherits the operator default.
    instruction_harnesses: dict[str, str | None] = Field(
        default_factory=dict,
        validation_alias="instructionHarnesses",
    )
    instruction_models: dict[str, str | None] = Field(
        default_factory=dict,
        validation_alias="instructionModels",
    )
    instruction_billings: dict[str, Billing | None] = Field(
        default_factory=dict,
        validation_alias="instructionBillings",
    )
    instruction_efforts: dict[str, Effort | None] = Field(
        default_factory=dict,
        validation_alias="instructionEfforts",
    )
    instruction_timeouts: dict[str, PositiveInt | None] = Field(
        default_factory=dict,
        validation_alias="instructionTimeouts",
    )
    # workflow kind -> {field -> value} (null clears).
    workflow_settings: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        validation_alias="workflowSettings",
    )
    # app name -> {field -> value} (null clears).
    app_settings: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        validation_alias="appSettings",
    )
