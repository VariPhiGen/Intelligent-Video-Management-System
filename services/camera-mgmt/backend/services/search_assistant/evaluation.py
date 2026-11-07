"""Readiness gate uses synthetic catalogs; never searches real evidence."""
from types import SimpleNamespace

from ..search_planner.schemas import PlannerError, PlannerConfig, Clarify
from . import interpret, grounding

CATALOG = {'intrusion': {'label': 'Intrusion', 'color': '#ff0000', 'search_aliases': []},
           'fall': {'label': 'Fall', 'color': '#ffff00', 'search_aliases': []}}


async def evaluate(runtime):
    cases = [
        ('Find a person wearing a red shirt today', [], 'search_people', 'new'),
        ('Find a red vehicle today', [], 'search_vehicles', 'new'),
        ('Count intrusion events per camera yesterday', [], 'count_events', 'new'),
        ('Count intrusion events yesterday', [], 'count_events', 'new'),
        ('Show the latest intrusion events', [], 'search_events', 'new'),
        ('Count intrusion events involving a ladder', [], None, None),
        ('also Gate 2', [{'tool': 'count_events', 'cameras': ['gate-1'], 'activities': ['intrusion']}], 'count_events', 'refine'),
        ('not Gate 2', [{'tool': 'count_events', 'cameras': ['gate-1', 'gate-2'], 'activities': ['intrusion']}], 'count_events', 'refine'),
        ('Show more', [{'tool': 'search_events'}], None, 'more'),
        ('Find plate KA01AB1234 yesterday', [], 'lookup_plate', 'new'),
        ('Which activities are configured on Gate 2?', [], 'describe_setup', 'new'),
        ('Start over', [{'tool': 'count_events', 'cameras': ['gate-1']}], None, 'reset'),
        ('What is the weather?', [{'tool': 'count_events', 'cameras': ['gate-1']}], None, 'unsupported'),
        # Everyday phrasing, clock times and durations: a provider that loses the
        # time (or a backend that masks it as a plate) must not pass.
        ('What happened at Gate 1 yesterday?', [], 'search_events', None),
        ('Count intrusion events after 8.30pm yesterday', [], 'count_events', None),
        ('Give me intrusion events from the last 30mins', [], None, None),
        ('Which camera had the most intrusion events yesterday?', [], 'count_events', None),
        ('Who entered Gate 1 yesterday?', [], None, None),
        # A self-contained question after a visual search must not inherit its
        # appearance filter (grounding would then refuse the event search).
        ('Show intrusion events that happened at Gate 1 yesterday',
         [{'tool': 'search_people', 'appearance': 'red shirt', 'cameras': ['gate-2']}], 'search_events', None),
    ]
    cameras = [SimpleNamespace(slug='gate-1', name='Gate 1', camera_metadata={}, retention_minutes=90*1440),
               SimpleNamespace(slug='gate-2', name='Gate 2', camera_metadata={}, retention_minutes=90*1440)]
    measurements = []
    import time
    for query, state, tool, mode in cases:
        start = time.monotonic()
        intent, metrics = await interpret.extract(runtime, query, state, cameras, CATALOG, 'UTC')
        if tool and (not intent.operations or intent.operations[0].tool != tool):
            raise PlannerError('Assistant evaluation failed tool selection. Keep the Assistant disabled and choose a more capable model.', 422)
        if mode and intent.mode != mode:
            raise PlannerError('Assistant evaluation failed conversation handling. Choose a more capable model.', 422)
        if 'ladder' in query and not (intent.unsupported or intent.mode in ('unsupported', 'clarify') or any(o.subject for o in intent.operations)):
            raise PlannerError('Assistant evaluation dropped an unsupported constraint.', 422)
        if query.startswith('also') and intent.operations[0].cameras.action!='add':
            raise PlannerError('Assistant evaluation failed additive camera refinement.', 422)
        if query.startswith('not') and intent.operations[0].cameras.action!='remove':
            raise PlannerError('Assistant evaluation failed camera removal.', 422)
        if query.startswith('Find plate') and intent.operations[0].plate.value != 'KA01AB1234':
            raise PlannerError('Assistant evaluation failed plate grounding.', 422)
        if '8.30pm' in query:
            t = intent.operations[0].time if intent.operations else None
            if not t or t.action != 'set' or t.value.start_time != '20:30:00' or intent.operations[0].plate.value:
                raise PlannerError('Assistant evaluation lost a clock time.', 422)
        if '30mins' in query:
            t = intent.operations[0].time if intent.operations else None
            if not t or t.value.kind != 'recent' or t.value.recent_minutes != 30:
                raise PlannerError('Assistant evaluation lost a duration.', 422)
        if query.startswith('Who ') and not (intent.unsupported or intent.mode in ('unsupported', 'clarify')):
            raise PlannerError('Assistant evaluation ran an identity question instead of refusing it.', 422)
        if tool:
            try:
                plans = grounding.ground(intent, query, state, cameras, CATALOG, PlannerConfig(), 'UTC')
            except Clarify:
                raise PlannerError('Assistant evaluation failed source grounding or filter preservation.', 422) from None
            if ('per camera' in query or 'Which camera' in query) and plans[0]['grouping'] != 'camera':
                raise PlannerError('Assistant evaluation failed count grouping.', 422)
            if query.startswith('also') and plans[0]['cameras'] != ['gate-1', 'gate-2']:
                raise PlannerError('Assistant evaluation lost the existing camera filter.', 422)
            if query.startswith('not') and plans[0]['cameras'] != ['gate-1']:
                raise PlannerError('Assistant evaluation failed to remove the specified camera.', 422)
            if 'that happened' in query and (plans[0]['cameras'] != ['gate-1'] or plans[0].get('appearance')):
                raise PlannerError('Assistant evaluation carried filters into a new question.', 422)
        measurements.append({'case': len(measurements)+1, 'duration_ms': round((time.monotonic()-start)*1000), **metrics})
    return {'ok': True, 'cases': measurements}
