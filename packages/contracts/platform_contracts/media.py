from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ProviderMediaReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["SET_KNOWN_MEDIA_ID", "CONFIRM_REMOTE_NOT_CREATED"]
    provider_media_id: str | None = Field(default=None, max_length=500)
    reason: str = Field(min_length=1, max_length=2000)
    explicit_confirmation: bool = Field(default=False, strict=True)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return normalized

    @field_validator("provider_media_id")
    @classmethod
    def normalize_provider_media_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_action_fields(self) -> ProviderMediaReconcileRequest:
        if self.action == "SET_KNOWN_MEDIA_ID" and not self.provider_media_id:
            raise ValueError("provider_media_id is required for SET_KNOWN_MEDIA_ID")
        if self.action == "CONFIRM_REMOTE_NOT_CREATED":
            if not self.explicit_confirmation:
                raise ValueError("explicit_confirmation must be true")
            if self.provider_media_id is not None:
                raise ValueError("provider_media_id is not allowed for CONFIRM_REMOTE_NOT_CREATED")
        return self


class ProviderMediaReconcileView(BaseModel):
    binding_id: str
    asset_id: str
    project_id: str
    provider: str
    account_id: str
    status: str
    provider_media_id: str | None = None
    action: str
    replayed: bool = False
