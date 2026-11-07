from . import anthropic, openai, qwen

ADAPTERS = {'qwen': qwen.extract, 'openai': openai.extract, 'anthropic': anthropic.extract}
