import asyncio
import json

import httpx
from pydantic import ValidationError

from .prompts import SYSTEM, VERSION
from .providers import ADAPTERS
from .providers.base import Runtime
from .schemas import PlannerError, SearchIntent

_slots = asyncio.Semaphore(4)


async def extract(runtime: Runtime, query: str, domain: str, timezone: str) -> SearchIntent:
    if not runtime.model.strip():
        raise PlannerError('Configure a planner model first.', 422)
    if runtime.provider != 'qwen' and not runtime.key.get_secret_value():
        raise PlannerError('Configure an API key for the selected provider first.', 422)
    user = json.dumps({'query': query, 'ui_domain': domain, 'timezone': timezone,
                       'schema_version': VERSION}, ensure_ascii=False)
    try:
        # Deadline includes queueing, both possible attempts, connect and reads.
        async with asyncio.timeout(runtime.timeout), _slots:
            async with httpx.AsyncClient(timeout=runtime.timeout, follow_redirects=False) as client:
                for attempt in range(2):
                    raw = await ADAPTERS[runtime.provider](client, runtime, SYSTEM, user)
                    try:
                        intent = SearchIntent.model_validate_json(raw)
                        if len(intent.camera_refs) > 64 or len(intent.visual_query) > 500:
                            raise ValueError()
                        return intent
                    except (ValidationError, ValueError, TypeError):
                        if attempt:
                            raise PlannerError('The planner could not interpret this query reliably. Rephrase or use explicit filters.', 502) from None
                        user += '\nReturn a complete object matching every field of the schema; no markdown.'
    except (TimeoutError, httpx.TimeoutException):
        raise PlannerError('Query interpretation timed out. Retry or use explicit filters.', 504) from None
    except httpx.HTTPError:
        raise PlannerError('The planner provider is unreachable. Retry or use explicit filters.', 502) from None
    except (KeyError, TypeError, AttributeError, IndexError):
        raise PlannerError('The planner returned an invalid response.', 502) from None
    raise PlannerError('Query interpretation failed.', 502)


async def test_connection(runtime):
    intent = await extract(runtime, 'person wearing a red shirt', 'people', 'UTC')
    if intent.domain != 'people' or intent.camera_refs or intent.time.kind != 'none' or intent.clarification or intent.unsupported or not intent.visual_query.strip():
        raise PlannerError('The provider failed the sample interpretation test. Check the model.', 422)
