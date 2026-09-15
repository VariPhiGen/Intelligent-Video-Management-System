"""search_scope.py — the five rules that stop another site's hits reaching this VMS.

WHY THIS IS IN THE HIGH-RISK BATCH. The CLIP index is shared with the analytics
appliance and is NOT scoped to this VMS. It holds crops from cameras configured
by hand on the appliance, rows scanned out of object storage with no camera
identity at all, and crops from cameras since removed from the registry. The
only thing keeping those out of an operator's search results is this module —
274 lines with no test.

A failure here has two shapes and they fail in opposite directions:

  too permissive   a hit from a camera this VMS does not record is returned,
                   the operator clicks it, and Playback has no footage. At
                   worst it is another deployment's data on this screen.
  too strict       a legitimate hit is dropped and the operator concludes the
                   person was never there. Silent, and unfalsifiable from the
                   UI — which is why the module counts drops BY REASON.

Both are tested. The freshness rules exist entirely because of the second, and
they are the subtlest thing in the file: the index runs ahead of the segment
indexer, so the newest and most useful footage legitimately reads as "not
covered yet".

Hermetic: the recorder is reached through two async functions and both are
stubbed. No HTTP, no database.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services import search_scope as ss  # noqa: E402
from backend.services.search_scope import (  # noqa: E402
    CHUNK_SECONDS,
    FRESH_MARGIN_SECONDS,
    _to_ms,
    build_index,
    resolve,
    retained_window,
)

NOW = int(datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
MIN = 60_000


def cam(slug="gate-a1b2", id="11111111-1111-1111-1111-111111111111",
        sensor_id=None, name="Front Gate"):
    return SimpleNamespace(slug=slug, id=id, sensor_id=sensor_id, name=name)


_DEFAULT = object()


def hit(camera="gate-a1b2", ts=_DEFAULT):
    """A hit. `ts=None` means the hit genuinely carries no timestamp — the
    sentinel keeps that distinguishable from "the test did not care"."""
    return {"camera_id": camera,
            "timestamp": (NOW - 10 * MIN) if ts is _DEFAULT else ts}


async def run_scope(hits, cameras, recorder, spans=None, monkeypatch=None,
                    check_coverage=True):
    """Drive `scope` with the recorder stubbed at its two seams."""
    async def _recorder():
        return recorder

    async def _covered(slug, frm, to):
        return spans

    monkeypatch.setattr(ss, "recorder_cameras", _recorder)
    monkeypatch.setattr(ss, "_covered_spans", _covered)
    return await ss.scope(
        hits,
        candidates_of=lambda h: (h.get("camera_id"),),
        timestamp_of=lambda h: h.get("timestamp"),
        cameras=cameras,
        check_coverage=check_coverage,
    )


def recording(earliest_ms, latest_ms, is_recording=True):
    return {"earliest": earliest_ms / 1000, "latest": latest_ms / 1000,
            "recording": is_recording}


# ── Rule 1: it must resolve to a camera in THIS registry ───────────────────

class TestIdentityResolution:
    def test_a_camera_resolves_by_every_identifier_it_might_carry(self):
        c = cam(slug="gate-a1b2", id="abc-123", sensor_id="sensor-9", name="Front Gate")
        index = build_index([c])
        for key in ("gate-a1b2", "abc-123", "sensor-9", "Front Gate"):
            assert resolve(index, key) is c, f"{key} did not resolve"

    def test_resolution_is_case_and_whitespace_insensitive(self):
        index = build_index([cam(name="Front Gate")])
        assert resolve(index, "  FRONT GATE  ") is not None

    def test_an_unknown_identifier_does_not_resolve(self):
        # An appliance-authored camera label like `cmajet`, which this VMS has
        # never heard of.
        assert resolve(build_index([cam()]), "cmajet") is None

    @pytest.mark.parametrize("junk", [None, "", "   "])
    def test_an_empty_identifier_never_resolves_to_anything(self, junk):
        # A row scanned out of object storage carries no camera identity at
        # all. If an empty key were indexed it would resolve to whichever
        # camera happened to have a null field, and that camera's footage would
        # be offered for someone else's crop.
        index = build_index([cam(sensor_id=None, name=None)])
        assert resolve(index, junk) is None

    def test_a_camera_with_null_fields_does_not_poison_the_index(self):
        index = build_index([cam(sensor_id=None, name=None)])
        assert None not in index and "" not in index
        assert "none" not in index, "the string 'none' was indexed from a null"

    def test_the_first_writer_wins_so_a_display_name_cannot_steal_a_slug(self):
        # A camera whose NAME is another camera's SLUG must not capture it.
        first = cam(slug="gate-a1b2", id="id-1", name="Front Gate")
        second = cam(slug="lobby-c3d4", id="id-2", name="gate-a1b2")
        index = build_index([first, second])
        assert resolve(index, "gate-a1b2") is first

    def test_the_first_matching_candidate_wins(self):
        c1, c2 = cam(slug="a", id="id-a", name="A"), cam(slug="b", id="id-b", name="B")
        index = build_index([c1, c2])
        assert resolve(index, "b", "a") is c2

    @pytest.mark.asyncio
    async def test_an_unresolvable_hit_is_dropped_and_counted(self, monkeypatch):
        out = await run_scope([hit(camera="cmajet")], [cam()],
                              {"gate-a1b2": recording(NOW - 60 * MIN, NOW)},
                              monkeypatch=monkeypatch)
        assert out.kept == []
        assert out.dropped == {"unmapped": 1}


# ── Rule 2/3: the recorder must know it, and it must carry a time ──────────

class TestRecorderAndTimestamp:
    @pytest.mark.asyncio
    async def test_a_camera_the_recorder_does_not_know_is_dropped(self, monkeypatch):
        # In the registry but not recorded: there is no footage to open.
        out = await run_scope([hit()], [cam()], {}, monkeypatch=monkeypatch)
        assert out.kept == [] and out.dropped == {"not_recorded": 1}

    @pytest.mark.asyncio
    async def test_a_recorder_row_with_no_span_is_not_recorded(self, monkeypatch):
        out = await run_scope([hit()], [cam()],
                              {"gate-a1b2": {"earliest": None, "latest": None}},
                              monkeypatch=monkeypatch)
        assert out.dropped == {"not_recorded": 1}

    @pytest.mark.parametrize("ts", [None, "", "not-a-time", "2026-13-45"])
    @pytest.mark.asyncio
    async def test_a_hit_with_no_usable_timestamp_is_dropped(self, monkeypatch, ts):
        out = await run_scope([hit(ts=ts)], [cam()],
                              {"gate-a1b2": recording(NOW - 60 * MIN, NOW)},
                              monkeypatch=monkeypatch)
        assert out.kept == [] and out.dropped == {"no_timestamp": 1}


class TestTimestampCoercion:
    """People hits carry ISO strings, vehicles carry epoch milliseconds, and the
    recorder answers in epoch seconds. Reading one as another puts a hit in 1970,
    where it falls outside every retention window and vanishes silently."""

    def test_epoch_milliseconds_are_taken_as_they_are(self):
        assert _to_ms(1_757_500_000_000) == 1_757_500_000_000

    def test_epoch_seconds_are_scaled_up(self):
        assert _to_ms(1_757_500_000) == 1_757_500_000_000

    def test_a_float_epoch_second_is_scaled(self):
        assert _to_ms(1_757_500_000.5) == 1_757_500_000_500

    def test_an_iso_string_becomes_milliseconds(self):
        assert _to_ms("2026-09-10T12:00:00Z") == NOW

    def test_a_z_suffix_and_an_explicit_offset_agree(self):
        assert _to_ms("2026-09-10T12:00:00Z") == _to_ms("2026-09-10T17:30:00+05:30")

    @pytest.mark.parametrize("junk", [None, "", "yesterday", {}, []])
    def test_unusable_input_is_none_rather_than_zero(self, junk):
        # Zero would be 1970 — a real timestamp that silently fails retention
        # instead of being reported as `no_timestamp`.
        assert _to_ms(junk) is None


# ── Rule 4: inside the retained window ─────────────────────────────────────

class TestRetentionWindow:
    def test_the_window_is_padded_at_the_front_by_one_chunk(self):
        # Grooming trims there and `earliest` moves while a page is open.
        lo, hi = retained_window(recording(NOW - 60 * MIN, NOW, False), NOW)
        assert lo == NOW - 60 * MIN - CHUNK_SECONDS * 1000
        assert hi == NOW

    def test_a_recording_camera_is_trusted_up_to_now(self):
        # The index runs ahead of the segment indexer, so a merely stale
        # `latest` would hide the freshest and most useful hits.
        stale = NOW - 30 * MIN
        _, hi = retained_window(recording(NOW - 60 * MIN, stale, True), NOW)
        assert hi == NOW

    def test_a_stopped_camera_is_not_trusted_past_its_last_segment(self):
        stale = NOW - 30 * MIN
        _, hi = retained_window(recording(NOW - 60 * MIN, stale, False), NOW)
        assert hi == stale

    def test_a_row_missing_a_bound_has_no_window(self):
        assert retained_window({"earliest": None, "latest": 1}, NOW) is None
        assert retained_window({"earliest": 1, "latest": None}, NOW) is None

    @pytest.mark.asyncio
    async def test_a_hit_before_the_window_is_dropped(self, monkeypatch):
        out = await run_scope([hit(ts=NOW - 10 * 60 * MIN)], [cam()],
                              {"gate-a1b2": recording(NOW - 60 * MIN, NOW)},
                              monkeypatch=monkeypatch)
        assert out.kept == [] and out.dropped == {"outside_retention": 1}

    @pytest.mark.asyncio
    async def test_a_hit_after_the_window_is_dropped(self, monkeypatch):
        out = await run_scope([hit(ts=NOW + 10 * 60 * MIN)], [cam()],
                              {"gate-a1b2": recording(NOW - 60 * MIN, NOW, False)},
                              monkeypatch=monkeypatch)
        assert out.dropped == {"outside_retention": 1}

    @pytest.mark.asyncio
    async def test_the_window_bounds_are_inclusive(self, monkeypatch):
        row = recording(NOW - 60 * MIN, NOW, False)
        lo, hi = retained_window(row, NOW)
        for edge in (lo, hi):
            out = await run_scope([hit(ts=edge)], [cam()], {"gate-a1b2": row},
                                  spans=[(lo, hi)], monkeypatch=monkeypatch)
            assert len(out.kept) == 1, f"the hit exactly on {edge} was dropped"


# ── Rule 5: a segment actually covers it ───────────────────────────────────

class TestCoverage:
    @pytest.mark.asyncio
    async def test_a_hit_in_a_recording_gap_is_dropped(self, monkeypatch):
        old = NOW - 50 * MIN                       # older than the fresh margin
        out = await run_scope([hit(ts=old)], [cam()],
                              {"gate-a1b2": recording(NOW - 60 * MIN, NOW - 40 * MIN, False)},
                              spans=[], monkeypatch=monkeypatch)
        assert out.kept == [] and out.dropped.get("in_gap") == 1

    @pytest.mark.asyncio
    async def test_a_covered_hit_survives(self, monkeypatch):
        old = NOW - 50 * MIN
        out = await run_scope([hit(ts=old)], [cam()],
                              {"gate-a1b2": recording(NOW - 60 * MIN, NOW - 40 * MIN, False)},
                              spans=[(old - MIN, old + MIN)], monkeypatch=monkeypatch)
        assert len(out.kept) == 1

    @pytest.mark.asyncio
    async def test_a_fresh_hit_skips_the_coverage_check(self, monkeypatch):
        # THE SUBTLE ONE. The recorder publishes a segment only once it closes
        # and indexes it later still, so the newest footage reads as uncovered.
        # Dropping it would hide live hits that become playable a minute later.
        #
        # THREE MINUTES, WRITTEN OUT. Deriving this from FRESH_MARGIN_SECONDS
        # made the test move with the constant: setting the margin to zero —
        # which would drop every live hit — left this passing, because the
        # "fresh" input collapsed onto `latest` along with the threshold. The
        # margin is 6 minutes (3 x CHUNK_SECONDS); 3 is inside it under the
        # real value and outside it under a broken one.
        latest = NOW
        out = await run_scope([hit(ts=latest - 3 * MIN)], [cam()],
                              {"gate-a1b2": recording(NOW - 60 * MIN, latest)},
                              spans=[], monkeypatch=monkeypatch)
        assert len(out.kept) == 1, "a hit inside the fresh margin was dropped as a gap"

    def test_the_fresh_margin_is_wide_enough_to_span_a_closing_segment(self):
        # The margin has to cover a segment that has not closed yet plus the
        # indexer's lag behind it. One chunk would be the bare minimum; the
        # value is three. Pinned so it cannot be trimmed to nothing silently.
        assert FRESH_MARGIN_SECONDS >= 2 * CHUNK_SECONDS

    @pytest.mark.asyncio
    async def test_a_hit_well_outside_the_fresh_margin_is_checked(self, monkeypatch):
        latest = NOW
        out = await run_scope([hit(ts=latest - 30 * MIN)], [cam()],
                              {"gate-a1b2": recording(NOW - 600 * MIN, latest)},
                              spans=[], monkeypatch=monkeypatch)
        assert out.kept == [] and out.dropped.get("in_gap") == 1

    @pytest.mark.asyncio
    async def test_coverage_can_be_turned_off(self, monkeypatch):
        old = NOW - 50 * MIN
        out = await run_scope([hit(ts=old)], [cam()],
                              {"gate-a1b2": recording(NOW - 60 * MIN, NOW - 40 * MIN, False)},
                              spans=[], monkeypatch=monkeypatch, check_coverage=False)
        assert len(out.kept) == 1

    @pytest.mark.asyncio
    async def test_an_unanswerable_coverage_call_keeps_the_coarse_verdict(self, monkeypatch):
        # `None` spans means the recorder could not answer. Dropping everything
        # then would empty the page because of an unrelated outage.
        old = NOW - 50 * MIN
        out = await run_scope([hit(ts=old)], [cam()],
                              {"gate-a1b2": recording(NOW - 60 * MIN, NOW - 40 * MIN, False)},
                              spans=None, monkeypatch=monkeypatch)
        assert len(out.kept) == 1
        assert "in_gap" not in out.dropped

    @pytest.mark.asyncio
    async def test_only_the_hits_in_a_gap_are_removed(self, monkeypatch):
        old_a, old_b = NOW - 50 * MIN, NOW - 45 * MIN
        row = {"gate-a1b2": recording(NOW - 60 * MIN, NOW - 40 * MIN, False)}
        out = await run_scope([hit(ts=old_a), hit(ts=old_b)], [cam()], row,
                              spans=[(old_b - MIN, old_b + MIN)],
                              monkeypatch=monkeypatch)
        assert [k.when_ms for k in out.kept] == [old_b]
        assert out.dropped["in_gap"] == 1


# ── Degradation when the recorder is unreachable ───────────────────────────

class TestRecorderOutage:
    @pytest.mark.asyncio
    async def test_hits_are_kept_when_the_recorder_cannot_be_reached(self, monkeypatch):
        # A deliberate trade: rules 2-4 need the recorder, and emptying every
        # search result because the NVR is restarting would read as "nothing
        # was ever recorded". Resolution (rule 1) still applies, so this is
        # never a route for a foreign camera's hits.
        out = await run_scope([hit(ts=NOW - 10 * MIN)], [cam()], None,
                              monkeypatch=monkeypatch)
        assert len(out.kept) == 1
        assert out.dropped == {}

    @pytest.mark.asyncio
    async def test_an_unmapped_hit_is_still_dropped_during_an_outage(self, monkeypatch):
        # The rule that keeps another appliance's crops out does not depend on
        # the recorder, and must not be relaxed with the others.
        out = await run_scope([hit(camera="cmajet")], [cam()], None,
                              monkeypatch=monkeypatch)
        assert out.kept == [] and out.dropped == {"unmapped": 1}


# ── The drop tally is what makes a short page explainable ──────────────────

class TestDropAccounting:
    @pytest.mark.asyncio
    async def test_each_reason_is_counted_separately(self, monkeypatch):
        out = await run_scope(
            [hit(camera="cmajet"),                       # unmapped
             hit(camera="lobby-c3d4"),                   # not recorded
             hit(ts="rubbish"),                          # no timestamp
             hit(ts=NOW - 10 * 60 * MIN)],               # outside retention
            [cam(), cam(slug="lobby-c3d4", id="id-2", name="Lobby")],
            {"gate-a1b2": recording(NOW - 60 * MIN, NOW)},
            monkeypatch=monkeypatch,
        )
        assert out.kept == []
        assert out.dropped == {"unmapped": 1, "not_recorded": 1,
                               "no_timestamp": 1, "outside_retention": 1}

    @pytest.mark.asyncio
    async def test_a_fully_kept_page_reports_no_drops(self, monkeypatch):
        out = await run_scope([hit(ts=NOW - 10 * MIN)], [cam()],
                              {"gate-a1b2": recording(NOW - 60 * MIN, NOW)},
                              monkeypatch=monkeypatch)
        assert len(out.kept) == 1 and out.dropped == {}

    def test_every_reason_the_code_uses_is_declared(self):
        # DROP_REASONS is what the API reports to the UI. A reason the code
        # emits but the tuple omits renders as an unexplained shortfall.
        source = (Path(ss.__file__)).read_text()
        emitted = set()
        for line in source.splitlines():
            if 'result.drop("' in line:
                emitted.add(line.split('result.drop("')[1].split('"')[0])
            if 'result.dropped["' in line and "=" in line:
                emitted.add(line.split('result.dropped["')[1].split('"')[0])
        assert emitted <= set(ss.DROP_REASONS), (
            f"reasons emitted but not declared in DROP_REASONS: "
            f"{emitted - set(ss.DROP_REASONS)}"
        )

    @pytest.mark.asyncio
    async def test_a_kept_hit_carries_the_camera_it_resolved_to(self, monkeypatch):
        # The caller uses this to build the playback deep link; resolving to
        # the wrong camera opens the wrong footage.
        c = cam()
        out = await run_scope([hit(camera="Front Gate", ts=NOW - 10 * MIN)], [c],
                              {"gate-a1b2": recording(NOW - 60 * MIN, NOW)},
                              monkeypatch=monkeypatch)
        assert out.kept[0].camera is c
