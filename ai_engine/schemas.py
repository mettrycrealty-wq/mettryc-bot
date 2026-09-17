from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


Role = Literal["client", "colleague", "unknown"]
Operation = Literal["sale", "rent", "unknown"]
Intent = Literal[
    "property_search",
    "property_detail",
    "visit_request",
    "human_handoff",
    "general_information",
    "qualification",
    "unknown",
]


class PropertyCriteria(BaseModel):
    """Normalized property preferences extracted from a conversation."""

    model_config = ConfigDict(extra="ignore")

    operation: Operation = "unknown"
    property_type: str | None = None
    country: str | None = None
    state: str | None = None
    city: str | None = None
    zone: str | None = None
    min_budget: float | None = Field(default=None, ge=0)
    max_budget: float | None = Field(default=None, ge=0)
    bedrooms: int | None = Field(default=None, ge=0)
    bathrooms: int | None = Field(default=None, ge=0)
    parking: int | None = Field(default=None, ge=0)
    min_m2: float | None = Field(default=None, ge=0)
    max_m2: float | None = Field(default=None, ge=0)
    features: list[str] = Field(default_factory=list)


class ConversationState(BaseModel):
    """Small, provider-neutral memory for a single conversation."""

    model_config = ConfigDict(extra="ignore")

    role: Role = "unknown"
    intent: Intent = "unknown"
    criteria: PropertyCriteria = Field(default_factory=PropertyCriteria)
    # Recent messages are intentionally bounded so the state remains small enough
    # for fast model calls while retaining actual conversational context.
    history: list[dict[str, str]] = Field(default_factory=list)
    last_properties: list[dict[str, Any]] = Field(default_factory=list)
    selected_property: dict[str, Any] | None = None
    assigned_agent: dict[str, Any] | None = None
    visit: dict[str, Any] | None = None
    summary: str = ""


class UserTurnAnalysis(BaseModel):
    """Structured interpretation of the latest customer message."""

    model_config = ConfigDict(extra="ignore")

    role: Role = "unknown"
    intent: Intent = "unknown"
    criteria: PropertyCriteria = Field(default_factory=PropertyCriteria)
    requested_property_code: str | None = None
    requested_property_reference: str | None = None
    needs_human: bool = False
    reasoning_summary: str = ""


class ToolResult(BaseModel):
    """Standard result returned by deterministic business tools."""

    ok: bool = True
    name: str
    data: Any = None
    message: str = ""


class EngineResult(BaseModel):
    """Public result returned by the AI engine to a channel adapter."""

    reply: str
    state: ConversationState
    tool_results: list[ToolResult] = Field(default_factory=list)
