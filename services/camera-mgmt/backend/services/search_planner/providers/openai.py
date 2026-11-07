from .base import MAX_OUTPUT_TOKENS, TRUNCATED, post, schema
from ..schemas import PlannerError


async def extract(client, runtime, system, user, output_schema=None):
    payload = {
        'model': runtime.model, 'store': False, 'max_output_tokens': MAX_OUTPUT_TOKENS,
        'input': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
        'text': {'format': {'type': 'json_schema', 'name': 'search_intent',
                            'strict': True, 'schema': (output_schema if output_schema is not None else schema())}},
    }
    if runtime.effort == 'low':
        # Reasoning models only; others reject it, hence the 'default' setting.
        payload['reasoning'] = {'effort': 'low'}
    data = await post(client, 'https://api.openai.com/v1/responses',
                      {'Authorization': 'Bearer ' + runtime.key.get_secret_value()}, payload)
    if (data.get('incomplete_details') or {}).get('reason') == 'max_output_tokens':
        raise PlannerError(TRUNCATED, 502)
    if data.get('status') != 'completed':
        raise PlannerError('The planner response was incomplete. Try a shorter query.', 502)
    text = []
    for output in data.get('output', []):
        for content in output.get('content', []):
            if content.get('type') == 'refusal':
                raise PlannerError('The provider declined to interpret this query. Rephrase or use explicit filters.', 422)
            if content.get('type') == 'output_text':
                text.append(content.get('text', ''))
    return ''.join(text)
