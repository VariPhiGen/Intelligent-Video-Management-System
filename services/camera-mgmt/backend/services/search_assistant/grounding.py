"""Pure, deterministic filter changes and catalog grounding."""
import copy
import difflib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from ..search_planner.resolver import _match_camera, time_bounds, zone
from ..search_planner.schemas import Clarify
from .interpret import ANAPHORIC_TIME, every_word_match, uncovered, words as interpret_words

COLLECTIVE = {'all cameras', 'each camera', 'every camera', 'cameras', 'camera',
              'all activities', 'every activity', 'each zone', 'per zone', 'all zones'}


class ChoiceNeeded(Clarify):
    def __init__(self, message, choices, operation, field, phrase):
        super().__init__(message)
        self.options = [{'id': str(uuid4()), 'label': label, 'values': values} for label, values in choices]
        self.operation, self.field, self.phrase = operation, field, phrase


def apply_values(old, action, values, universe):
    if action == 'keep':
        if values:
            raise Clarify('A new filter was mentioned without a clear change. Specify add, remove or only.')
        return old
    if action == 'set':
        return sorted(set(values))
    if not values:
        if action == 'remove':
            return None
        raise Clarify('Specify which camera or activity to add.')
    base = set(old if old is not None else universe)
    result = base | set(values) if action == 'add' else base - set(values)
    if not result:
        raise Clarify('That change leaves no cameras or activities. Choose a new scope.')
    return sorted(result)


def activity_matches(source, catalog):
    # Shared aliases explicitly describe a set, whereas a label collision is ambiguous.
    aliases = [k for k, t in catalog.items() if source.casefold() in [a.casefold() for a in t.get('search_aliases', [])]]
    if aliases:
        return aliases, True
    exact = [k for k, t in catalog.items() if source.casefold() in (k.casefold(), t['label'].casefold())]
    if exact:
        return exact, len(exact) == 1
    matches = [k for k, t in catalog.items() if any(every_word_match(source, n) for n in [k, t['label'], *t.get('search_aliases', [])])]
    return matches, len(matches) == 1


def ground(intent, message, previous, cameras, catalog, config, tz_name, *, overrides=None, now=None):
    now = now or datetime.now(timezone.utc)
    tz = zone(tz_name)
    if intent.unsupported or intent.mode == 'unsupported':
        raise Clarify('This request contains unsupported constraints: ' + ', '.join(intent.unsupported or ['the requested feature']))
    leftovers = uncovered(intent, message)
    if leftovers:
        raise Clarify('Please clarify these constraints before searching: ' + ', '.join(leftovers))
    slugs = [c.slug for c in cameras if c.slug]
    overrides = overrides or {}
    # A self-contained question replaces context even if a provider labels it
    # refine. This is state handling, never query/tool routing.
    # Bare "that" is not one: "intrusions that happened at Gate 1 yesterday" is a
    # new question. "that day" / "same time" are, via ANAPHORIC_TIME.
    refinement = {'also', 'only', 'same', 'those', 'not', 'except', 'exclude'}
    refines = bool(set(interpret_words(message)) & refinement) or bool(ANAPHORIC_TIME.search(message))
    fresh = any(o.time.action == 'set' and (o.cameras.values or o.activities.source) for o in intent.operations) and not refines
    plans = []
    for i, operation in enumerate(intent.operations):
        old = previous[min(i, len(previous)-1)] if previous and intent.mode in ('refine', 'compare', 'more') and (not fresh or intent.mode == 'compare') else {}
        plan = copy.deepcopy(old)
        plan.update(tool=operation.tool, grouping=old.get('grouping', 'none') if operation.grouping == 'keep' else operation.grouping,
                    sort=old.get('sort', 'latest') if operation.sort == 'keep' else operation.sort, cursor=None, offset=0)
        plan['interpretations'] = []
        if operation.subject:
            raise Clarify('This constraint is not supported by the selected search: ' + operation.subject)
        refs = [r for r in operation.cameras.values if r.casefold() not in COLLECTIVE]
        resolved = []
        for ref in refs:
            if f'{i}:cameras:{ref}' in overrides:
                resolved.extend(overrides[f'{i}:cameras:{ref}'])
                continue
            matches = _match_camera(ref, cameras)
            if len(matches) != 1:
                options = matches or sorted(cameras, key=lambda c: difflib.SequenceMatcher(None, ref.casefold(), (c.name or c.slug).casefold()).ratio(), reverse=True)[:8]
                raise ChoiceNeeded(f'Choose the camera intended by “{ref}”.', [(c.name or c.slug, [c.slug]) for c in options], i, 'cameras', ref)
            resolved.append(matches[0].slug)
        zones = [r for r in operation.zones.values if r.casefold() not in COLLECTIVE]
        if refs and zones:
            raise Clarify('Choose camera names or a camera zone for this operation so the scope is unambiguous.')
        for ref in zones:
            matched = [c.slug for c in cameras if str((c.camera_metadata or {}).get('zone', '')).casefold() == ref.casefold()]
            if not matched:
                raise Clarify(f'No configured camera zone named “{ref}”.')
            resolved.extend(matched)
        action = operation.cameras.action if refs or not zones else operation.zones.action
        plan['cameras'] = apply_values(old.get('cameras'), action, resolved, slugs)
        if action == 'set' and not resolved:
            plan['cameras'] = None
        if plan['cameras'] is not None and not set(plan['cameras']).issubset(slugs):
            raise Clarify('A camera in this investigation is no longer available. Start a new investigation.')
        activity = operation.activities
        selected = []
        if activity.source and activity.source.casefold() not in COLLECTIVE:
            override = overrides.get(f'{i}:activities:{activity.source}')
            matches, declared = activity_matches(activity.source, catalog)
            if override is not None:
                selected = override
            elif declared:
                selected = matches
                if not all(activity.source.casefold() in (k.casefold(), catalog[k]['label'].casefold()) for k in selected):
                    plan['interpretations'].append(f'Interpreted “{activity.source}” as ' + ', '.join(catalog[k]['label'] for k in selected))
            elif matches or not activity.keys:
                options = matches or list(catalog)
                raise ChoiceNeeded(f'Choose the activity intended by “{activity.source}”.', [(catalog[k]['label'], [k]) for k in options[:10]], i, 'activities', activity.source)
            else:
                selected = activity.keys
                plan['interpretations'].append(f'Interpreted “{activity.source}” as ' + ', '.join(catalog[k]['label'] for k in selected))
        plan['activities'] = apply_values(old.get('activities'), activity.action, selected, list(catalog))
        if activity.action == 'set' and not selected:
            plan['activities'] = None
        if plan['activities'] is not None and not set(plan['activities']).issubset(catalog):
            raise Clarify('An activity in this investigation is no longer available. Start a new investigation.')
        for field in ('appearance', 'plate'):
            change = getattr(operation, field)
            if change.action == 'keep' and change.value:
                raise Clarify(f'Clarify how to change the {field} filter.')
            plan[field] = old.get(field, '') if change.action == 'keep' else (change.value if change.action == 'set' else '')
        if plan['activities'] and (plan['appearance'] or plan['plate']):
            raise Clarify('Choose an activity search or an appearance/plate search. These records cannot establish that both describe the same object.')
        if operation.appearance.action != 'keep' or operation.tool != 'search_people':
            plan.pop('example', None)
        if operation.tool in ('search_people', 'search_vehicles') and not plan['appearance'] and not plan.get('example'):
            raise Clarify('Describe the appearance to search for.')
        if len(plan['plate']) > 32:
            raise Clarify('Plate values must contain at most 32 characters.')
        if operation.tool == 'lookup_plate' and not plan['plate']:
            raise Clarify('Enter a plate to look up.')
        if operation.tool in ('count_events', 'search_events', 'describe_setup') and (plan['appearance'] or plan['plate']):
            raise Clarify('Event records cannot answer appearance or plate constraints. Clear those filters or use visual search.')
        if operation.tool in ('search_people', 'search_vehicles', 'lookup_plate') and plan['activities']:
            raise Clarify('Clear the activity filter before searching visual detections.')
        t = operation.time
        if t.action == 'keep' and t.value.source:
            raise Clarify('A new time was mentioned without a clear change. Specify the new time window.')
        if t.action == 'keep' and old.get('time_from'):
            start, end = datetime.fromisoformat(old['time_from']), datetime.fromisoformat(old['time_to'])
        elif t.action != 'set' and operation.tool == 'search_events' and plan['sort'] in ('latest', 'earliest'):
            from ...config import settings
            selected_cameras = [c for c in cameras if plan['cameras'] is None or c.slug in plan['cameras']]
            minutes = max([c.retention_minutes or (settings.nvr_retention_minutes or settings.nvr_default_retention_days * 1440) for c in selected_cameras] or [config.max_window_days * 1440])
            start, end = now - timedelta(minutes=minutes), now
        else:
            start, end = time_bounds(t.value, tz, now, config)
        if end <= start or start >= now:
            raise Clarify('Choose a past window whose end is after its start.')
        # Latest/earliest uses the retention range. Explicit windows retain policy limits.
        if t.action == 'set' and end-start > timedelta(days=config.max_window_days):
            raise Clarify(f'Choose at most {config.max_window_days} days.')
        plan.update(time_from=start.isoformat(), time_to=end.isoformat(), timezone=tz_name)
        plans.append(plan)
    return plans
