from __future__ import annotations

from typing import Any, Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator, model_validator

Decision = Literal["PASS", "FAIL", "ABSTAIN"]


class ReferenceAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1, max_length=200)
    asset_version: str = Field(min_length=1, max_length=200)
    url: AnyHttpUrl
    view: Literal[
        "FRONT",
        "THREE_QUARTER_LEFT",
        "THREE_QUARTER_RIGHT",
        "LEFT_PROFILE",
        "RIGHT_PROFILE",
    ] = "FRONT"

    @field_validator("url")
    @classmethod
    def https_reference_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.scheme != "https" or value.username or value.password:
            raise ValueError("reference URL must be credential-free HTTPS")
        return value


class CharacterInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    character_id: str = Field(min_length=1, max_length=200)
    reference_assets: list[ReferenceAsset] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def unique_reference_versions(self) -> CharacterInput:
        identities = [(item.asset_id, item.asset_version) for item in self.reference_assets]
        if len(identities) != len(set(identities)):
            raise ValueError("reference asset id/version pairs must be unique")
        return self


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    shot_id: str = Field(min_length=1, max_length=200)
    video_url: AnyHttpUrl
    characters: list[CharacterInput] = Field(min_length=1, max_length=20)
    threshold_version: str = Field(min_length=1, max_length=160)
    shot_type: str = Field(default="DIALOGUE", min_length=1, max_length=80)
    sample_positions: list[float] | None = Field(default=None, max_length=120)

    @field_validator("video_url")
    @classmethod
    def https_video_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.scheme != "https" or value.username or value.password:
            raise ValueError("video URL must be credential-free HTTPS")
        return value

    @field_validator("sample_positions")
    @classmethod
    def valid_sample_positions(cls, value: list[float] | None) -> list[float] | None:
        if value is not None and any(not 0 <= item <= 1 for item in value):
            raise ValueError("sample positions must be between zero and one")
        return value


class AnalyzeAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: Literal["ACCEPTED"] = "ACCEPTED"
    # True when this job_id was already accepted earlier: the request is
    # acknowledged without starting a second GPU job for the same candidate.
    duplicate: bool = False


class CallbackEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["character-evidence-callback-v1"] = "character-evidence-callback-v1"
    job_id: str
    project_id: str
    shot_id: str
    status: Literal["SUCCEEDED", "FAILED"]
    reports: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    error_code: str | None = Field(default=None, max_length=120)
    error_message: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def success_has_reports_and_failure_does_not(self) -> CallbackEnvelope:
        if self.status == "SUCCEEDED" and not self.reports:
            raise ValueError("successful callback requires at least one report")
        if self.status == "FAILED" and self.reports:
            raise ValueError("failed callback cannot carry evidence reports")
        return self
