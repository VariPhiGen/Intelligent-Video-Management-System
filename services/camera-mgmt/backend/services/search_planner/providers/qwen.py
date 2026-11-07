from .base import local_endpoint, post, schema
from ..schemas import PlannerError


async def extract(client, runtime, system, user, output_schema=None):
    headers = {'Authorization': 'Bearer ' + runtime.key.get_secret_value()} if runtime.key.get_secret_value() else {}
    data = await post(client, local_endpoint(runtime.base_url) + '/chat/completions', headers, {
        'model': runtime.model, 'max_tokens': 8192 if output_schema is not None else 2048, 'temperature': 0,
        'chat_template_kwargs': {'enable_thinking': False},
        'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
        'response_format': {'type': 'json_schema', 'json_schema': {
            'name': 'search_intent', 'strict': True, 'schema': (output_schema if output_schema is not None else schema())}},
    })
    choices = data.get('choices') or []
    if not choices or choices[0].get('finish_reason') != 'stop':
        raise PlannerError('The local planner response was incomplete. Check the model configuration.', 502)
    message = choices[0].get('message') or {}
    if message.get('refusal'):
        raise PlannerError('The local planner declined this query. Rephrase or use explicit filters.', 422)
    return message.get('content', '')
