"""PTZ phase 1 — presets keep their identity, the camera's view is confirmed by its picture, patrols dwell from
arrival.

  * a patrol step points at the preset's VMS id; a token a camera hands out again after a delete never re-binds it
  * "at preset" from GetStatus is not trusted alone: the picture must stop changing and match the preset's picture
  * the view (what per-preset AI follows) is "none" while moving / off preset / not yet confirmed
  * the patrol's dwell starts when the stop is confirmed; an unconfirmed stop is "missed" and dwelt anyway

No camera, no Valkey, no Postgres: fakes throughout, real frames from tests/fixtures/ptz.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytest.importorskip("backend.extensions.ptz_patrols")

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_engine_push(monkeypatch):
    """A view change also switches per-preset AI (services/ptz_views) — a DB read + an HTTP push; not here."""
    from backend.services import ptz_views

    async def nothing(*a, **k):
        return None
    monkeypatch.setattr(ptz_views, "on_view_change", nothing)

from backend.extensions.ptz_patrols.bind import bind_steps  # noqa: E402
from backend.extensions.ptz_patrols.runner import ARRIVE_TIMEOUT_S, PresetGone, Runner  # noqa: E402
from backend.services import ptz_track  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "ptz"
GATE_ID, YARD_ID, NEW_ID = ("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222",
                            "33333333-3333-3333-3333-333333333333")


def _plan(steps):
    return {"patrols": [{"key": "day", "name": "Day", "steps": steps}]}


# ── binding steps to preset ids ──────────────────────────────────────────────

def test_a_token_only_step_is_bound_to_that_presets_id():
    presets = [{"id": GATE_ID, "token": "1"}, {"id": YARD_ID, "token": "2"}]
    plan, problems, changed = bind_steps(_plan([{"preset_token": "2", "dwell_s": 20}]), presets)
    assert not problems and changed
    assert plan["patrols"][0]["steps"][0] == {"preset_token": "2", "preset_id": YARD_ID, "dwell_s": 20}


def test_the_id_wins_and_follows_a_new_token():
    # Restore gave the Gate preset token "9"; the step still means Gate.
    presets = [{"id": GATE_ID, "token": "9"}]
    plan, problems, _ = bind_steps(_plan([{"preset_token": "1", "preset_id": GATE_ID, "dwell_s": 20}]), presets)
    assert not problems and plan["patrols"][0]["steps"][0]["preset_token"] == "9"


def test_a_deleted_preset_is_refused_on_save_not_rebound_to_the_reused_token():
    # Gate (token 1) was deleted; the camera gave "1" to a new preset. The step must not quietly patrol it.
    presets = [{"id": NEW_ID, "token": "1"}]
    plan, problems, _ = bind_steps(_plan([{"preset_token": "1", "preset_id": GATE_ID, "dwell_s": 20}]), presets)
    assert problems and "deleted" in problems[0]
    assert plan["patrols"][0]["steps"][0]["preset_id"] == GATE_ID
    _, problems, _ = bind_steps(_plan([{"preset_token": "5", "dwell_s": 20}]), presets)
    assert problems and "no preset '5'" in problems[0]


def test_the_runners_one_time_upgrade_only_fills_missing_ids():
    presets = [{"id": NEW_ID, "token": "1"}, {"id": YARD_ID, "token": "2"}]
    steps = [{"preset_token": "1", "preset_id": GATE_ID, "dwell_s": 20}, {"preset_token": "2", "dwell_s": 20},
             {"preset_token": "8", "dwell_s": 20}, {"home": True, "dwell_s": 20}]
    plan, problems, changed = bind_steps(_plan(steps), presets, only_missing=True)
    got = plan["patrols"][0]["steps"]
    assert changed and not problems
    assert got[0]["preset_id"] == GATE_ID and got[1]["preset_id"] == YARD_ID and "preset_id" not in got[2]


# ── confirming the view by its picture ───────────────────────────────────────

def _frames(*names):
    seq = [(FIX / n).read_bytes() for n in names]
    state = {"i": 0}

    async def frame():
        jpeg = seq[min(state["i"], len(seq) - 1)]
        state["i"] += 1
        return jpeg
    return frame


async def _nosleep(_s):
    return None


async def test_a_still_picture_that_matches_the_presets_own_is_confirmed_by_picture():
    ok, how = await ptz_track.verify_arrival(_frames("cpplus_pan_before.jpg"), (FIX / "cpplus_pan_before.jpg").read_bytes(),
                                             sleep=_nosleep)
    assert (ok, how) == (True, "picture")


async def test_a_picture_that_does_not_match_the_preset_is_not_confirmed():
    ok, how = await ptz_track.verify_arrival(_frames("cpplus_pan_after.jpg"), (FIX / "cpplus_pan_before.jpg").read_bytes(),
                                             sleep=_nosleep)
    assert not ok and "doesn't match" in how


async def test_a_head_still_turning_is_not_confirmed():
    # alpha's i-PRO says "arrived" while the picture still changes from grab to grab.
    turning = ["ipro_pan_before.jpg", "ipro_pan_after.jpg"] * 4
    ok, how = await ptz_track.verify_arrival(_frames(*turning), None, sleep=_nosleep)
    assert (ok, how) == (False, ptz_track.SETTLE_FAIL)


async def test_no_preset_picture_falls_back_to_position_and_a_still_frame():
    ok, how = await ptz_track.verify_arrival(_frames("ipro_tilt_before.jpg"), None, sleep=_nosleep)
    assert (ok, how) == (True, "position")


# ── the published view ───────────────────────────────────────────────────────

class FakeRedis:
    def __init__(self):
        self.store, self.published = {}, []

    async def get(self, k):
        return self.store.get(k)

    async def set(self, k, v, ex=None):
        self.store[k] = v

    async def expire(self, k, s):
        return 1

    async def publish(self, ch, msg):
        self.published.append((ch, json.loads(msg)))


async def test_the_view_is_none_until_confirmed_then_names_the_preset_and_clears_on_moving(monkeypatch):
    r = FakeRedis()
    confirmed = []

    async def fake_confirm(camera_id, slug, cur, attempts=1):
        confirmed.append(cur["preset_id"])
        r.store[ptz_track.current_key(camera_id)] = json.dumps(cur)
        await ptz_track._set_view(r, camera_id, slug, ptz_track.view_for(cur, "picture", ptz_track.datetime.now(
            ptz_track.timezone.utc)))
    monkeypatch.setattr(ptz_track, "_confirm", fake_confirm)
    ptz_track._verify_tasks.clear()
    at_gate = {"state": "at_preset", "preset_id": GATE_ID, "preset_token": "1", "preset_name": "Gate", "since": "t1"}

    await ptz_track._track_view(r, "cam", "post-a", at_gate)
    assert json.loads(r.store[ptz_track.view_key("cam")])["reason"] == "arriving"
    await asyncio.gather(*ptz_track._verify_tasks.values())
    view = json.loads(r.store[ptz_track.view_key("cam")])
    assert view["state"] == "preset" and view["preset_id"] == GATE_ID and view["verified_by"] == "picture"

    await ptz_track._track_view(r, "cam", "post-a", at_gate)          # same arrival, next poll: no second check
    assert confirmed == [GATE_ID]

    await ptz_track._track_view(r, "cam", "post-a", {"state": "moving", "since": "t2"})
    assert json.loads(r.store[ptz_track.view_key("cam")]) == {"state": "none", "reason": "moving"}
    states = [m["state"] for ch, m in r.published if ch == "vms.ptz.view.post-a"]
    assert states == ["none", "preset", "none"]                        # published on change only


async def test_an_unsettled_picture_is_checked_again_a_few_times_then_left(monkeypatch):
    r = FakeRedis()
    tries = []

    async def failing_confirm(camera_id, slug, cur, attempts=1):
        tries.append(attempts)
        await ptz_track._set_view(r, camera_id, slug,
                                  {**ptz_track.no_view(ptz_track.SETTLE_FAIL, cur), "arrival": cur["since"],
                                   "attempts": attempts})
    monkeypatch.setattr(ptz_track, "_confirm", failing_confirm)
    ptz_track._verify_tasks.clear()
    cur = {"state": "at_preset", "preset_id": GATE_ID, "preset_token": "1", "preset_name": "Gate", "since": "t1"}
    for _ in range(6):
        await ptz_track._track_view(r, "cam", "post-a", cur)
        await asyncio.gather(*ptz_track._verify_tasks.values())
    assert tries == [1, 2, 3]


# ── the patrol dwells from arrival ───────────────────────────────────────────

class Clocked:
    """A runner with a fake camera, a controllable "arrived" and a fake clock."""

    def __init__(self, steps):
        from datetime import datetime, timezone
        self.moves, self.events, self.there = [], [], False
        self.gone: set = set()

        async def move(slug, step):
            if step.get("preset_id") in self.gone:
                raise PresetGone("preset 'Gate' was deleted — step skipped")
            self.moves.append(step["label"])

        async def holder(slug):
            return None

        async def on_event(run, event, step_index, step, detail):
            self.events.append((event, step.get("label") if step else None, detail))

        async def arrived(run, step, moved_at):
            return self.there

        self.runner = Runner(mover=move, holder=holder, on_event=on_event, arrived=arrived)
        self.plan = {"plan_version": "v1", "timezone": "UTC", "patrols": [{"key": "day", "steps": steps}],
                     "schedule": [{"patrol_key": "day", "days": [0, 1, 2, 3, 4, 5, 6], "start": "00:00", "end": "00:00"}]}
        self.runner.sync_plans([{"camera_id": "c1", "slug": "post-a", "plan_version": "v1"}])
        self.run = self.runner.runs["c1"]
        self.mono, self.now = 1000.0, datetime(2026, 10, 28, 6, 0, tzinfo=timezone.utc)

    async def tick(self, advance=0.0):
        self.mono += advance
        await self.runner.tick_camera(self.run, self.plan, True, self.now, self.mono)
        for _ in range(3):
            await asyncio.sleep(0)
        await self.runner.tick_camera(self.run, self.plan, True, self.now, self.mono)


STEPS = [{"label": "Gate", "dwell_s": 20, "preset_token": "1", "preset_id": GATE_ID},
         {"label": "Yard", "dwell_s": 20, "preset_token": "2", "preset_id": YARD_ID}]


async def test_the_dwell_counts_from_confirmed_arrival_not_from_the_move_command():
    c = Clocked(STEPS)
    await c.tick()
    assert c.moves == ["Gate"] and c.run.extra.get("arriving")
    await c.tick(25)                                   # 25 s after the move: still not confirmed → no next step
    assert c.moves == ["Gate"]
    c.there = True
    await c.tick(1)                                    # confirmed at 26 s → the 20 s dwell starts now
    assert ("arrived", "Gate", "") in c.events
    c.there = False
    await c.tick(19)
    assert c.moves == ["Gate"]
    await c.tick(1)
    assert c.moves == ["Gate", "Yard"]


async def test_a_stop_never_confirmed_is_missed_and_dwelt_anyway():
    c = Clocked(STEPS)
    await c.tick()
    await c.tick(ARRIVE_TIMEOUT_S + 1)
    assert c.events[-1][0] == "missed" and "dwelling anyway" in c.events[-1][2]
    await c.tick(20)
    assert c.moves == ["Gate", "Yard"]


async def test_a_step_whose_preset_was_deleted_is_skipped_and_logged():
    c = Clocked(STEPS)
    c.gone.add(GATE_ID)
    await c.tick()
    assert c.events[0][0] == "missed" and "deleted" in c.events[0][2]
    await c.tick(1)
    assert c.moves == ["Yard"]                         # straight on, no backoff, no re-bind
    assert c.run.state == "running" and c.run.err_count == 0
