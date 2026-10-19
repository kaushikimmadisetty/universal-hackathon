from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator


AgentId = Literal["coordinator", "branding", "copy", "image", "landing_page", "app"]
AgentStatus = Literal["waiting", "working", "done", "failed"]
JobStatus = Literal["queued", "running", "succeeded", "failed"]
EventType = Literal[
    "job.queued",
    "job.started",
    "job.completed",
    "job.failed",
    "agent.started",
    "agent.message",
    "agent.handoff",
    "agent.completed",
    "agent.failed",
    "landing_page.deployed",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BrandColor(StrictModel):
    name: str = Field(min_length=1, max_length=40)
    hex: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")


class BrandFonts(StrictModel):
    heading: str = Field(min_length=1, max_length=80)
    body: str = Field(min_length=1, max_length=80)


class BrandKit(StrictModel):
    business_name: str = Field(min_length=1, max_length=100)
    logo_concept: str = Field(min_length=1, max_length=1000)
    colors: list[BrandColor] = Field(min_length=2, max_length=6)
    fonts: BrandFonts
    tone_of_voice: str = Field(min_length=1, max_length=500)


class StoreCopy(StrictModel):
    headline: str = Field(min_length=1, max_length=240)
    product_description: str = Field(min_length=1, max_length=4000)
    about: str = Field(min_length=1, max_length=4000)
    cta_label: str = Field(min_length=1, max_length=80)


class ImageResult(StrictModel):
    url: AnyHttpUrl
    alt: str = Field(min_length=1, max_length=300)
    role: str = Field(default="product", min_length=1, max_length=40)

    @field_validator("url")
    @classmethod
    def require_https_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.scheme != "https":
            raise ValueError("Image URLs must use HTTPS.")
        return value


class WorkflowEvent(StrictModel):
    schema_version: Literal[1] = 1
    event_id: UUID
    type: EventType
    agent: AgentId | None = None
    to_agent: AgentId | None = None
    message: str | None = Field(default=None, max_length=2000)
    data: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime | None = None

    @field_validator("occurred_at")
    @classmethod
    def normalize_occurred_at(cls, value: datetime | None) -> datetime | None:
        if value is None or value.tzinfo is not None:
            return value
        return value.replace(tzinfo=timezone.utc)


class LaunchAccepted(StrictModel):
    job_id: UUID
    status: JobStatus
    status_url: str
    events_url: str


class AgentSnapshot(StrictModel):
    id: AgentId
    name: str
    status: AgentStatus
    message: str
    updated_at: datetime


class JobResults(StrictModel):
    brand: dict[str, Any] | None = None
    copy: dict[str, Any] | None = None
    images: list[dict[str, Any]] | None = None
    landing_url: AnyHttpUrl | None = None
    app: dict[str, Any] | None = None
    summary: str | None = None


class JobStatusResponse(StrictModel):
    job_id: UUID
    status: JobStatus
    current_stage: str | None = None
    agents: list[AgentSnapshot]
    results: JobResults
    error: str | None = None
    updated_at: datetime


class EventResponse(StrictModel):
    event_id: UUID
    sequence: int
    type: EventType
    agent: AgentId | None = None
    to_agent: AgentId | None = None
    message: str | None = None
    data: dict[str, Any]
    occurred_at: datetime


class CallbackAck(StrictModel):
    accepted: bool = True
    duplicate: bool = False
    sequence: int
