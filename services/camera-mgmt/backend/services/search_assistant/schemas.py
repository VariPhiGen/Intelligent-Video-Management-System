from typing import Literal
from uuid import UUID

from pydantic import Field
from ..search_planner.schemas import StrictModel, TimeIntent

Tool = Literal['search_people', 'search_vehicles', 'lookup_plate', 'search_events', 'count_events', 'describe_setup']
Change = Literal['keep', 'set', 'add', 'remove']


class References(StrictModel):
    action: Change
    values: list[str] = Field(max_length=64)


class Activities(StrictModel):
    action: Change
    source: str
    keys: list[str] = Field(max_length=64)


class TextChange(StrictModel):
    action: Literal['keep', 'set', 'remove']
    value: str


class TimeChange(StrictModel):
    action: Literal['keep', 'set', 'remove']
    value: TimeIntent


class Operation(StrictModel):
    tool: Tool
    cameras: References
    zones: References
    activities: Activities
    appearance: TextChange
    plate: TextChange
    time: TimeChange
    grouping: Literal['keep', 'none', 'camera', 'zone', 'activity', 'hour', 'day']
    sort: Literal['keep', 'latest', 'earliest', 'relevance']
    subject: str


class Interpretation(StrictModel):
    mode: Literal['new', 'refine', 'more', 'compare', 'reset', 'unsupported', 'clarify', 'play', 'similar']
    operations: list[Operation] = Field(max_length=3)
    result_position: int | None
    unsupported: list[str]
    clarification: str | None


class CreateConversation(StrictModel):
    timezone: str = Field(min_length=1, max_length=100)


class Turn(StrictModel):
    message: str = Field(default='', max_length=1000)
    request_id: UUID
    expected_version: int = Field(ge=0)
    choice_id: str | None = Field(default=None, max_length=80)
    timezone: str | None = Field(default=None, max_length=100)
    result_id: str | None = Field(default=None, max_length=255)
    result_action: Literal['play', 'similar'] | None = None
    # Chips are explicit operator changes, applied without another model call.
    remove_filter: Literal['cameras', 'activities', 'appearance', 'plate', 'time'] | None = None


def output_schema(keys):
    schema = Interpretation.model_json_schema()
    schema['$defs']['Activities']['properties']['keys']['items'] = {'type': 'string', 'enum': list(keys) or ['__none__']}
    return schema
