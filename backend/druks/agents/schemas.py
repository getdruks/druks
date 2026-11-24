from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from druks.schemas import Schema
from druks.ui import FileSummary

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class AgentRequest(BaseModel):
    name: NonBlank
    description: str
    instructions: str
    skills: list[str]
    harness: str
    model: str
    is_private: bool = Field(alias="isPrivate")


class AgentUpdateRequest(AgentRequest):
    owner_id: str = Field(alias="ownerId")
    channel_id: NonBlank | None = Field(alias="channelId")
    picture: NonBlank | None


class AgentResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str
    instructions: str
    skills: list[str]
    harness: str
    model: str
    owner_id: str
    is_private: bool
    channel_id: str | None
    picture: FileSummary | None


class AgentCostResponse(Schema):
    cost_usd: float
