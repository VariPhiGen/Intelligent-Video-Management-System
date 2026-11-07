from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

Provider = Literal['qwen', 'openai', 'anthropic']
Domain = Literal['people', 'vehicles']
# 'low' asks reasoning-capable cloud models for a short think: extraction needs
# little, and default effort can spend the output budget and the deadline before
# the JSON is written. 'default' sends no effort option, for models that reject it.
Effort = Literal['low', 'default']
MAX_KEY_LENGTH = 4096


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class TimeIntent(StrictModel):
    kind: Literal['none', 'day', 'point', 'range', 'recent']
    source: str
    # All fields required (nullable) for the common strict JSON-schema subset.
    date: str | None
    day_offset: int | None
    start_time: str | None
    end_date: str | None
    end_day_offset: int | None
    end_time: str | None
    recent_minutes: int | None


class SearchIntent(StrictModel):
    domain: Domain | None
    visual_query: str
    camera_refs: list[str]
    time: TimeIntent
    clarification: str | None
    unsupported: list[str]


class Profile(StrictModel):
    model: str = Field(default='', max_length=200)
    base_url: str = Field(default='', max_length=512)
    reasoning_effort: Effort = 'low'


class PlannerConfig(StrictModel):
    enabled: bool = False
    assistant_enabled: bool = False
    provider: Provider = 'qwen'
    profiles: dict[Provider, Profile] = Field(default_factory=lambda: {
        p: Profile() for p in ('qwen', 'openai', 'anthropic')})
    point_minutes: int = Field(default=5, ge=1, le=60)
    lookback_hours: int = Field(default=24, ge=1, le=720)
    max_window_days: int = Field(default=31, ge=1, le=366)
    timeout_seconds: int = Field(default=20, ge=3, le=60)

    @model_validator(mode='after')
    def complete_profiles(self):
        for p in ('qwen', 'openai', 'anthropic'):
            self.profiles.setdefault(p, Profile())
        if self.lookback_hours > self.max_window_days * 24:
            raise ValueError('Default lookback exceeds maximum search window')
        return self


class SettingsUpdate(StrictModel):
    config: PlannerConfig
    # Omitted key = retain; empty string = remove. Never serialize these to logs.
    keys: dict[Provider, SecretStr] = Field(default_factory=dict, exclude=True)

    @field_validator('keys')
    @classmethod
    def bounded_keys(cls, keys):
        if any(len(k.get_secret_value()) > MAX_KEY_LENGTH for k in keys.values()):
            raise ValueError('API key is too long')
        return keys


class NaturalQuery(StrictModel):
    query: str = Field(min_length=1, max_length=500)
    timezone: str = Field(min_length=1, max_length=100)
    domain: Domain
    camera: str | None = Field(default=None, max_length=255)
    time_from: str | None = Field(default=None, max_length=64)
    time_to: str | None = Field(default=None, max_length=64)
    top_k: int = Field(default=24, ge=1, le=96)
    score_threshold: float = Field(default=0.15, ge=0, le=1)
    sort_by: Literal['time', 'confidence'] = 'confidence'
    sort_dir: Literal['asc', 'desc'] = 'desc'
    plate: str | None = Field(default=None, max_length=32)
    vehicle_type: str | None = Field(default=None, max_length=32)
    color: str | None = Field(default=None, max_length=32)


class SearchPlan(StrictModel):
    domain: Domain
    visual_query: str
    camera_slugs: list[str]
    # False when nothing narrowed the cameras: the search runs over the whole
    # domain set exactly as the explicit search would, not a copied list of it.
    camera_scoped: bool
    time_from: str
    time_to: str
    summary: str


class Clarify(Exception):
    def __init__(self, message: str, choices: list[str] | None = None):
        self.message = message
        self.choices = choices or []
        super().__init__(message)


class PlannerError(Exception):
    def __init__(self, message: str, status: int = 503):
        self.message, self.status = message, status
        super().__init__(message)
