"""Shared data models for OCR and MCP responses."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ResponseFormat(str, Enum):
    """Supported tool response formats."""

    MARKDOWN = "markdown"
    JSON = "json"


class StrictInput(BaseModel):
    """Base input model that rejects unknown parameters."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class OcrBlock(BaseModel):
    """One line recognized by a local operating-system OCR engine."""

    text: str
    confidence: float = Field(ge=0.0, le=1.0)
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    width: float = Field(ge=0.0, le=1.0)
    height: float = Field(ge=0.0, le=1.0)

    @property
    def center_x(self) -> float:
        return self.x + self.width / 2

    @property
    def center_y(self) -> float:
        return self.y + self.height / 2


class WindowInfo(BaseModel):
    """Logical screen bounds for the active WeChat main window."""

    window_id: int
    pid: int
    title: str
    x: float
    y: float
    width: float
    height: float


class ChatSummary(BaseModel):
    """A chat row recognized from WeChat's recent-session sidebar."""

    name: str
    preview: str = ""
    time_label: str = ""
    click_x: float
    click_y: float


class ChatMessage(BaseModel):
    """A visible chat message reconstructed from local OCR."""

    sender: Literal["ME", "OTHER", "UNKNOWN"]
    text: str
    confidence: float = Field(ge=0.0, le=1.0)
    page: int = Field(ge=0)


class TodoCandidate(BaseModel):
    """A message that likely contains a task, request, or commitment."""

    chat_name: str
    sender: Literal["ME", "OTHER", "UNKNOWN"]
    text: str
    score: float
    signals: list[str]


class ToolResult(BaseModel):
    """Consistent structured response returned by every MCP tool."""

    response_format: ResponseFormat
    text: str
    data: dict[str, Any]
    truncated: bool = False
