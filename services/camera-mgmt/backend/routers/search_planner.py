"""Natural-language interpretation layered over existing person/vehicle search."""
import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import SearchPlannerSettings
from ..security import get_principal, require_role
from ..services import audit, policy, search_scope
from ..services.search_planner import service, settings as planner_settings
from ..services.search_planner.resolver import resolve, zone
from ..services.search_planner.schemas import Clarify, NaturalQuery, PlannerError, SettingsUpdate
from . import search
from ..services.search_assistant import service as assistant_service, evaluation as assistant_evaluation

router = APIRouter(tags=['search-planner'])
_admin = require_role('admin')


def failure(exc):
    return HTTPException(exc.status, detail={'error': exc.message, 'code': 'planner_unavailable'})


@router.get('/planner/status')
async def status(request: Request, db: AsyncSession = Depends(get_db)):
    principal = await get_principal(request)
    await policy.require(principal, search.CAPABILITY)
    try:
        eff = planner_settings.effective(await planner_settings.row(db))
        return {'enabled': eff.config.enabled, 'provider': eff.config.provider,
                'ready': eff.config.enabled and eff.public['tested'],
                'assistant_enabled': eff.config.assistant_enabled,
                'assistant_ready': eff.config.assistant_enabled and eff.public.get('assistant_tested', False),
                'available_tools': await assistant_service.available(principal)}
    except PlannerError as exc:
        raise failure(exc) from None


@router.get('/planner/settings')
async def get_settings(db: AsyncSession = Depends(get_db), principal=Depends(_admin)):
    try:
        return planner_settings.effective(await planner_settings.row(db)).public
    except PlannerError as exc:
        raise failure(exc) from None


async def settings_body(request: Request) -> SettingsUpdate:
    """Parsed here rather than by FastAPI, whose 422 echoes the rejected input
    — for this body, a plaintext API key — back to the client and into logs."""
    try:
        return SettingsUpdate.model_validate_json(await request.body())
    except ValidationError as exc:
        raise HTTPException(422, detail=exc.errors(
            include_input=False, include_context=False, include_url=False)) from None


async def update_settings(body, request, db, principal, test_only=False):
    try:
        saved = await planner_settings.row(db)
        eff = planner_settings.effective(saved, body)
        planner_settings.validate_config(eff.config)
        proposed = planner_settings.fingerprint(eff.runtime)
        needs_test = test_only or (eff.config.enabled and
                                   (saved.tested_fingerprint if saved else None) != proposed)
        # The provider call can take the whole configured timeout. End the read
        # transaction first so no lock or pooled connection is held across it.
        assistant_proposed = planner_settings.assistant_fingerprint(eff.runtime) if eff.config.assistant_enabled else None
        needs_assistant_test = eff.config.assistant_enabled and (getattr(saved, 'assistant_tested_fingerprint', None) if saved else None) != assistant_proposed
        await db.rollback()
        if needs_assistant_test and not test_only:
            await assistant_evaluation.evaluate(eff.runtime)
        if needs_test:
            await service.test_connection(eff.runtime)
        if test_only:
            # Test proposed values without storing keys or activating changes.
            return {'ok': True}
        # Serialize singleton creation and concurrent updates without a lost key update.
        await db.execute(text("SELECT pg_advisory_xact_lock(637063)"))
        saved = await planner_settings.row(db, lock=True)
        eff = planner_settings.effective(saved, body)
        if needs_test and planner_settings.fingerprint(eff.runtime) != proposed:
            # A retained key or deployment override changed during the test;
            # what passed is not what would be saved.
            raise PlannerError('Planner settings changed while the connection was tested. Review them and save again.', 409)
        fingerprint = proposed if needs_test else (saved.tested_fingerprint if saved else None)
        credentials = planner_settings.encrypted_updates(saved, body)
        if saved is None:
            saved = SearchPlannerSettings(id=1)
            db.add(saved)
        saved.config = body.config.model_dump()
        saved.credentials = credentials
        saved.tested_fingerprint = fingerprint
        if needs_assistant_test and planner_settings.assistant_fingerprint(eff.runtime) != assistant_proposed:
            raise PlannerError("Assistant settings changed during testing. Save again.", 409)
        if needs_assistant_test:
            saved.assistant_tested_fingerprint = assistant_proposed
        await db.commit()
        await audit.record(request, principal, 'smartsearch.planner.settings', target='planner',
                           detail={'provider': eff.config.provider, 'enabled': eff.config.enabled})
        return planner_settings.effective(saved).public
    except PlannerError as exc:
        raise failure(exc) from None


# The admin check is declared first so a non-admin's body is never parsed.
@router.put('/planner/settings')
async def save_settings(request: Request, principal=Depends(_admin),
                        body: SettingsUpdate = Depends(settings_body),
                        db: AsyncSession = Depends(get_db)):
    return await update_settings(body, request, db, principal)


@router.post('/planner/test')
async def test_settings(request: Request, principal=Depends(_admin),
                        body: SettingsUpdate = Depends(settings_body),
                        db: AsyncSession = Depends(get_db)):
    return await update_settings(body, request, db, principal, test_only=True)


@router.post('/natural')
async def natural(body: NaturalQuery, request: Request, db: AsyncSession = Depends(get_db)):
    now = datetime.now(timezone.utc)
    principal = await get_principal(request)
    await policy.require(principal, search.CAPABILITY)
    inventory = None
    try:
        zone(body.timezone)  # Invalid timezone must never incur a provider request.
        saved = await planner_settings.row(db)
        eff = planner_settings.effective(saved)
        if not eff.config.enabled:
            raise PlannerError('Query interpretation is disabled. Use explicit filters or ask an administrator to configure it.')
        if not eff.public['tested']:
            raise PlannerError('The selected planner configuration needs a successful connection test. Save it in planner settings.')
        # Names resolve against the set the domain's own search covers: people
        # need recordings, vehicles only registration. The recorder inventory
        # is network-only, so it is fetched while the provider interprets.
        registered = await search._registered(db)
        if body.domain == 'people':
            inventory = asyncio.create_task(search_scope.recorder_cameras())
        intent = await service.extract(eff.runtime, body.query, body.domain, body.timezone)
        if intent.unsupported:
            return {'status': 'unsupported', 'message': 'This query asks for features the visual index cannot search: ' + ', '.join(intent.unsupported)[:500]}
        recorder = await inventory if inventory else None
        cameras = [c for c in registered if c.slug and (recorder is None or c.slug in recorder)]
        plan = resolve(intent, body, cameras, eff.config, now,
                       unsearchable=[c for c in registered if c.slug and c not in cameras])
        scope = plan.camera_slugs if plan.camera_scoped else None
        args = {'query': plan.visual_query, 'camera': None,
                'time_from': plan.time_from, 'time_to': plan.time_to,
                'top_k': body.top_k, 'score_threshold': body.score_threshold,
                'sort_by': body.sort_by, 'sort_dir': body.sort_dir}
        if plan.domain == 'people':
            result = await search.execute_people(search.PeopleQuery(**args), request, db,
                                                  camera_slugs=scope, searchable=(registered, recorder))
        else:
            result = await search.execute_vehicles(search.VehiclesQuery(**args, plate=body.plate,
                vehicle_type=body.vehicle_type, color=body.color), request, db,
                camera_slugs=scope, registered=registered)
        await audit.record(request, principal, 'smartsearch.planner.query', target=plan.domain,
                           detail={'provider': eff.config.provider, 'model': eff.runtime.model,
                                   'cameras': scope, 'from': plan.time_from,
                                   'to': plan.time_to, 'schema_version': '1'})
        return {'status': 'results', 'domain': plan.domain, 'summary': plan.summary,
                'outcome': result}
    except Clarify as exc:
        return {'status': 'clarification', 'message': exc.message, 'choices': exc.choices}
    except PlannerError as exc:
        raise failure(exc) from None
    finally:
        if inventory and not inventory.done():
            inventory.cancel()


@router.post('/planner/assistant/test')
async def test_assistant(request: Request, principal=Depends(_admin),
                         body: SettingsUpdate = Depends(settings_body),
                         db: AsyncSession = Depends(get_db)):
    try:
        eff = planner_settings.effective(await planner_settings.row(db), body)
        planner_settings.validate_config(eff.config)
        await db.rollback()
        return await assistant_evaluation.evaluate(eff.runtime)
    except PlannerError as exc:
        raise failure(exc) from None
