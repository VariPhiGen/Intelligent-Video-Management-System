"""Opt-in live planner evaluation; run inside the API container from
camera-mgmt with PYTHONPATH=. (it needs DATABASE_URL and DISCOVERY_SECRET_KEY).

Evaluates the provider, model and key saved in the admin UI — exactly what
operators' queries use. No provider requests happen without --run-live.
Outputs aggregate metrics only.
"""
import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

from backend.db import AsyncSessionLocal
from backend.services.search_planner import service, settings


async def saved_runtime():
    async with AsyncSessionLocal() as db:
        return settings.effective(await settings.row(db)).runtime


async def evaluate(cases):
    runtime = await saved_runtime()
    results = []
    for case in cases:
        start = time.monotonic()
        try:
            intent = await service.extract(runtime, case['query'], case['domain'], case['timezone'])
            expected = case['expected']
            passed = (
                intent.domain == case['domain'] and
                intent.camera_refs == expected['camera_refs'] and
                not intent.clarification and not intent.unsupported and
                all(getattr(intent.time, key) == value for key, value in expected['time'].items()) and
                all(term in intent.visual_query.casefold() for term in expected['visual_terms'])
            )
            results.append((passed, time.monotonic() - start, False))
        except Exception:
            # Do not print exception bodies or credentials from external runtimes.
            results.append((False, time.monotonic() - start, True))
    latencies = sorted(r[1] for r in results)
    print(json.dumps({
        'provider': runtime.provider, 'model': runtime.model, 'cases': len(results),
        'accuracy': sum(r[0] for r in results) / len(results),
        'error_rate': sum(r[2] for r in results) / len(results),
        'p50_seconds': statistics.median(latencies),
        'p95_seconds': latencies[min(len(latencies)-1, int(len(latencies)*.95))],
        'note': 'Extraction baseline only; separately assess ambiguity, visual relevance and model token usage.',
    }, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live', action='store_true', help='Authorize provider requests; cloud usage may incur charges')
    args = parser.parse_args()
    cases = json.loads(Path(__file__).with_name('planner_queries.json').read_text())
    if args.run_live:
        asyncio.run(evaluate(cases))
    else:
        print(f'{len(cases)} cases loaded. Pass --run-live to evaluate the configured provider.')
