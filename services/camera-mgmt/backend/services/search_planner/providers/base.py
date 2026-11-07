import json
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from ..schemas import PlannerError, SearchIntent


# Room for a reasoning model's thinking plus the small JSON object; only the
# tokens actually generated are billed.
MAX_OUTPUT_TOKENS = 8192
last_usage = ContextVar("planner_usage", default=None)


@dataclass(frozen=True)
class Runtime:
    provider: str
    model: str
    base_url: str
    key: SecretStr
    timeout: int
    effort: str = 'low'


def local_endpoint(base_url: str) -> str:
    try:
        u = urlsplit(base_url)
        u.port  # Also validate malformed/out-of-range port numbers.
    except ValueError:
        raise PlannerError('Configure a valid local HTTP(S) endpoint.', 422) from None
    if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise PlannerError('Configure a valid local HTTP(S) endpoint without embedded credentials.', 422)
    return base_url.rstrip('/')


async def post(client: httpx.AsyncClient, url: str, headers: dict, payload: dict) -> dict:
    # Bounded response reads; never surface provider bodies/headers to users or logs.
    async with client.stream('POST', url, headers=headers, json=payload) as response:
        if response.status_code in (401, 403):
            raise PlannerError('The planner provider rejected its credentials. Ask an administrator to check them.')
        if response.status_code == 429:
            raise PlannerError('The planner provider is rate limited. Retry shortly.', 429)
        if response.status_code >= 400:
            raise PlannerError('The planner request failed. Check the model ID, its structured-output support '
                               'and the reasoning-effort setting.', 502)
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            raw.extend(chunk)
            if len(raw) > 65536:
                raise PlannerError('The planner response exceeded its limit.', 502)
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError()
        usage = data.get('usage') or {}
        last_usage.set({k: usage[k] for k in ('input_tokens', 'output_tokens', 'prompt_tokens', 'completion_tokens', 'total_tokens')
                        if isinstance(usage.get(k), int)})
        return data
    except (ValueError, UnicodeError):
        raise PlannerError('The planner returned an invalid response.', 502) from None


TRUNCATED = 'The planner ran out of output budget. Set reasoning effort to low or choose a non-reasoning model.'


def schema() -> dict:
    return SearchIntent.model_json_schema()
