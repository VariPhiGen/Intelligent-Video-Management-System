"""Opt-in live Assistant evaluation of the saved provider; no real footage is searched.

Run from camera-mgmt with PYTHONPATH=. Pass --run-live to authorize provider
calls. Cloud calls may incur usage charges. Only aggregate metrics are printed.
"""
import argparse
import asyncio
import json
import statistics

from backend.db import AsyncSessionLocal
from backend.services.search_planner import settings
from backend.services.search_planner.schemas import PlannerError
from backend.services.search_assistant.evaluation import evaluate


async def main():
    async with AsyncSessionLocal() as db:
        runtime = settings.effective(await settings.row(db)).runtime
    try:
        report = await evaluate(runtime)
    except PlannerError:
        print(json.dumps({'provider': runtime.provider, 'model': runtime.model, 'passed': False}))
        raise SystemExit(1)
    durations = sorted(c['duration_ms'] for c in report['cases'])
    print(json.dumps({'provider': runtime.provider, 'model': runtime.model, 'passed': True,
        'cases': len(durations), 'p50_ms': statistics.median(durations),
        'p95_ms': durations[min(len(durations)-1, int(len(durations)*.95))],
        'provider_retries': sum(c['provider_retries'] for c in report['cases']),
        'token_usage': [c['token_usage'] for c in report['cases']]}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live', action='store_true')
    args = parser.parse_args()
    if args.run_live:
        asyncio.run(main())
    else:
        print('Pass --run-live to evaluate the saved provider against the Assistant readiness suite.')
