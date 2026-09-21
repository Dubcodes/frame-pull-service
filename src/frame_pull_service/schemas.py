from __future__ import annotations

from pydantic import BaseModel, Field


class ReviewPatch(BaseModel):
    final_name: str | None = Field(default=None, max_length=255)
    final_role: str | None = Field(default=None, max_length=255)
    final_track: str | None = Field(default=None, max_length=255)


class SelectCandidateRequest(BaseModel):
    candidate_id: int


class CaptureFrameRequest(BaseModel):
    clip_time: float = Field(ge=0)


class RejectRequest(BaseModel):
    reason: str = Field(default="false_positive", max_length=500)
