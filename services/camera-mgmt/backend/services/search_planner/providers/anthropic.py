from .base import MAX_OUTPUT_TOKENS, TRUNCATED, post, schema
from ..schemas import PlannerError


async def extract(client, runtime, system, user, output_schema=None):
    output_config = {'format': {'type': 'json_schema', 'schema': (output_schema if output_schema is not None else schema())}}
    if runtime.effort == 'low':
        # Current Claude models think by default (some cannot turn it off);
        # effort is the lever that keeps it short.
        output_config['effort'] = 'low'
    data = await post(client, 'https://api.anthropic.com/v1/messages', {
        'x-api-key': runtime.key.get_secret_value(), 'anthropic-version': '2023-06-01',
    }, {
        'model': runtime.model, 'max_tokens': MAX_OUTPUT_TOKENS, 'system': system,
        'messages': [{'role': 'user', 'content': user}],
        'output_config': output_config,
    })
    if data.get('stop_reason') == 'refusal':
        raise PlannerError('The provider declined to interpret this query. Rephrase or use explicit filters.', 422)
    if data.get('stop_reason') == 'max_tokens':
        raise PlannerError(TRUNCATED, 502)
    if data.get('stop_reason') != 'end_turn':
        raise PlannerError('The planner response was incomplete. Try a shorter query.', 502)
    return ''.join(c.get('text', '') for c in data.get('content', []) if c.get('type') == 'text')
