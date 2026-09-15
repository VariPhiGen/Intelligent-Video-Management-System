"""The CPU activities: the registry, their definitions, how camera settings are
read, and what each implemented activity decides.

Activities are driven here directly with FrameContexts, so every rule is
exercised on exact timestamps and positions. The path that feeds them — every
frame, its own detector, the real tracker — is test_activity_pipeline.py.

Run: python -m pytest tests -q   (from services/analytics)
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics.activities import (ACTIVITY_CLASSES, FULL_FRAME, ActivitySpec,  # noqa: E402
                                  FrameContext, Schedule, TrackedObject, resolve_zones)
from analytics.activities.base import (Activity, ActivityDefinition, ActivityEvent,  # noqa: E402
                                       AVAILABLE, ZONE_OPTIONAL, ZONE_REQUIRED, ZONE_TRIPWIRE)
from analytics.activities.engine import ActivityEngine, config_fingerprint  # noqa: E402
from analytics.activities.people_gathering import COOLDOWN_S, find_clusters_by_zone  # noqa: E402

CAM = "cam3-2qyj"
W, H = 1920, 1080
ALL_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
IMPLEMENTED = {"restricted_zone_entry", "people_gathering", "stray_parking", "entry_exit_WLE_logs"}
ON_HOLD = {"no_person_area", "car_detection", "idle_worker"}

LEFT_HALF = {"kind": "zone", "name": "Left half", "color": "#4fd1c5",
             "points": [[0.0, 0.0], [0.5, 0.0], [0.5, 1.0], [0.0, 1.0]], "direction": None}
RIGHT_HALF = {"kind": "zone", "name": "Right half", "color": "#52c77e",
              "points": [[0.5, 0.0], [1.0, 0.0], [1.0, 1.0], [0.5, 1.0]], "direction": None}
WHOLE = {"kind": "zone", "name": "Everything", "color": "#ffb020",
         "points": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]], "direction": None}
DOOR_LINE = {"kind": "tripwire", "name": "Door line", "color": "#ffb020",
             "points": [[0.2, 0.5], [0.8, 0.5]], "direction": "a2b"}
DOOR_B2A = {**DOOR_LINE, "name": "Door line (b2a)", "direction": "b2a"}
SHORT_LINE = {"kind": "tripwire", "name": "Short line", "color": "#ffb020",
              "points": [[0.45, 0.5], [0.55, 0.5]], "direction": "both"}
DIAGONAL = {"kind": "tripwire", "name": "Diagonal", "color": "#ffb020",
            "points": [[0.2, 0.2], [0.8, 0.8]], "direction": "both"}


def config(activity, regions=("left",), region_map=None, **params):
    region_map = {"left": LEFT_HALF, "right": RIGHT_HALF} if region_map is None else region_map
    p = {"active_hours": None, "active_days": list(ALL_DAYS)}
    p.update(params)
    return {"regions": region_map,
            "activities": [{"type": activity, "regions": list(regions), "params": p}]}


def build(cfg, camera=CAM) -> Activity:
    entry = cfg["activities"][0]
    cls = ACTIVITY_CLASSES[entry["type"]]
    return cls(camera, ActivitySpec.build(cls.definition, entry, cfg["regions"]))


def obj(track="t1", x=0.25, y=0.8, label="person", domain="person", conf=0.9,
        w=0.03, h=0.2, confirmed=True) -> TrackedObject:
    """A tracked object whose BOTTOM CENTRE is at normalised (x, y)."""
    return TrackedObject(track_id=track, domain=domain, label=label, confidence=conf,
                         bbox=(x - w / 2, y - h, x + w / 2, y), confirmed=confirmed,
                         is_new=False)


def car(track="c1", x=0.25, y=0.8, label="car", conf=0.8) -> TrackedObject:
    return obj(track, x, y, label=label, domain="vehicles", conf=conf, w=0.1, h=0.1)


def frame(*objects, ts=1_789_000_000.0, camera=CAM) -> FrameContext:
    return FrameContext(camera=camera, ts=ts, width=W, height=H, objects=tuple(objects))


# ── the registry ─────────────────────────────────────────────────────────────

class TestRegistry:
    def test_the_registry_holds_the_vms_activities(self):
        assert set(ACTIVITY_CLASSES) == IMPLEMENTED | ON_HOLD

    def test_the_four_vms_activities_run(self):
        assert {k for k, c in ACTIVITY_CLASSES.items() if c.implemented} == IMPLEMENTED

    def test_person_detection_is_not_an_activity(self):
        assert "person_detection" not in ACTIVITY_CLASSES
        assert all(d["key"] != "person_detection" for d in ActivityEngine().definitions())

    def test_each_lives_in_its_own_module_named_after_its_key(self):
        for key, cls in ACTIVITY_CLASSES.items():
            assert cls.__module__ == f"analytics.activities.{key}", key
            assert cls.definition.key == cls.key == key

    @pytest.mark.parametrize("key", sorted(ON_HOLD))
    def test_an_activity_on_hold_offers_no_settings_and_never_runs(self, key, monkeypatch):
        cls = ACTIVITY_CLASSES[key]
        assert cls.definition.status == "hold" and cls.definition.params == ()
        monkeypatch.setattr(cls, "evaluate", lambda self, ctx: pytest.fail(f"{key} was run"))
        engine = ActivityEngine()
        summary = engine.configure(CAM, config(key, regions=("left",)))
        assert summary["running"] == [] and summary["not_implemented"] == [key]
        assert engine.observe(frame(obj())) == [] and not engine.has_work(CAM)

    def test_entry_exit_runs_on_tripwires_and_offers_no_settings_yet(self):
        d = ACTIVITY_CLASSES["entry_exit_WLE_logs"].definition
        assert (d.status, d.zone_rule, d.params, d.domains) == \
            ("available", ZONE_TRIPWIRE, (), frozenset({"person"}))

    def test_published_definitions_carry_exactly_the_settings_the_code_reads(self):
        wire = {d["key"]: d for d in ActivityEngine().definitions()}
        keys = lambda k: [f["key"] for f in wire[k]["params_schema"]]  # noqa: E731
        assert keys("restricted_zone_entry") == ["cooldown_s"]
        assert keys("people_gathering") == ["required_duration_s", "last_time", "min_group_size",
                                            "proximity_factor", "cooldown_s"]
        assert keys("stray_parking") == ["vehicle_classes", "required_duration_s"]
        assert keys("entry_exit_WLE_logs") == []
        # The DeepStream-era frame counts and unread fields are not offered.
        for k in ("frame_accuracy", "required_frames", "person_limit", "min_overlap"):
            assert all(k not in keys(a) for a in wire), k
        assert wire["stray_parking"]["zone_rule"] == ZONE_REQUIRED
        assert wire["people_gathering"]["zone_rule"] == ZONE_OPTIONAL
        assert wire["restricted_zone_entry"]["zone_rule"] == ZONE_OPTIONAL
        assert wire["entry_exit_WLE_logs"]["zone_rule"] == ZONE_TRIPWIRE

    def test_a_published_setting_says_what_the_ui_needs(self):
        stray = {d["key"]: d for d in ActivityEngine().definitions()}["stray_parking"]
        duration, = [f for f in stray["params_schema"] if f["key"] == "required_duration_s"]
        assert duration == {"key": "required_duration_s", "label": "Required parking duration",
                            "kind": "number", "default": 600, "unit": "s", "min": 1,
                            "max": 86400, "integer": False, "options": None,
                            "min_items": None, "configurable": True,
                            "help": duration["help"]}
        classes, = [f for f in stray["params_schema"] if f["key"] == "vehicle_classes"]
        assert classes["options"] == ["car", "motorcycle", "bus", "truck"]

    def test_people_gathering_publishes_its_three_behaviour_settings(self):
        wire = {d["key"]: d for d in ActivityEngine().definitions()}
        fields = {f["key"]: f for f in wire["people_gathering"]["params_schema"]}
        pick = lambda f: {k: f[k] for k in ("kind", "default", "unit", "min", "max", "integer")}  # noqa: E731
        assert pick(fields["min_group_size"]) == {"kind": "number", "default": 2, "unit": None,
                                                  "min": 2, "max": 50, "integer": True}
        assert pick(fields["proximity_factor"]) == {"kind": "number", "default": 1.35,
                                                    "unit": "× box width", "min": 0.5, "max": 10,
                                                    "integer": False}
        assert pick(fields["cooldown_s"]) == {"kind": "number", "default": 100, "unit": "s",
                                              "min": 1, "max": 86400, "integer": False}
        assert all(f["label"] and f["help"] and f["configurable"] for f in fields.values())
        # The two it already had are unchanged.
        assert pick(fields["required_duration_s"]) == {"kind": "number", "default": 60, "unit": "s",
                                                       "min": 1, "max": 86400, "integer": False}
        assert pick(fields["last_time"]) == {"kind": "number", "default": 30, "unit": "s",
                                             "min": 1, "max": 3600, "integer": False}
        assert wire["people_gathering"]["zone_rule"] == ZONE_OPTIONAL

    def test_the_other_activities_publish_what_they_did_before(self):
        wire = {d["key"]: d for d in ActivityEngine().definitions()}
        [cooldown] = wire["restricted_zone_entry"]["params_schema"]
        assert {k: cooldown[k] for k in ("key", "kind", "default", "unit", "min", "max")} == \
            {"key": "cooldown_s", "kind": "number", "default": 60, "unit": "s", "min": 1, "max": 86400}
        assert wire["restricted_zone_entry"]["zone_rule"] == ZONE_OPTIONAL
        assert [f["key"] for f in wire["stray_parking"]["params_schema"]] == \
            ["vehicle_classes", "required_duration_s"]
        assert (wire["stray_parking"]["zone_rule"], wire["stray_parking"]["version"]) == (ZONE_REQUIRED, 1)
        entry = wire["entry_exit_WLE_logs"]
        assert (entry["params_schema"], entry["zone_rule"], entry["version"]) == ([], ZONE_TRIPWIRE, 2)

    def test_no_activity_decodes_detects_or_touches_deepstream(self):
        forbidden = ("cv2", "ultralytics", "openvino", "torch", "onnx", "redis", "shapely",
                     "scipy", "analytics.detector", "analytics.sampler",
                     "analytics.frame_source", "analytics.pipeline")
        # DeepStream-era names that must not survive as CODE (docstrings may
        # still explain what was ported from where).
        dead = {"video_clip_recorder", "create_result_events", "user_data",
                "parameters_data", "violation_id_data", "is_activity_active"}
        # OpenCV is allowed in exactly one module, and only for line/box geometry.
        geometry = {"entry_exit_WLE_logs.py": "cv2"}
        root = pathlib.Path(__file__).resolve().parents[1] / "analytics" / "activities"
        offenders = []
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            allowed = geometry.get(path.name)
            if allowed:
                used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                        and isinstance(n.value, ast.Name) and n.value.id == allowed}
                assert used == {"clipLine"}, f"{path.name} uses {allowed}.{sorted(used)}"
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [(node.module or "") if node.level == 0 else f"analytics.{node.module}"]
                elif isinstance(node, ast.Name) and node.id in dead:
                    offenders.append(f"{path.name}: uses {node.id}")
                elif isinstance(node, ast.Attribute) and node.attr in dead:
                    offenders.append(f"{path.name}: uses .{node.attr}")
                offenders += [f"{path.name}: imports {n}" for n in names
                              if n != allowed and ("deepstream" in n.lower()
                              or any(n == f or n.startswith(f + ".") for f in forbidden))]
        assert not offenders, offenders


# ── settings: defaults, overrides, invalid values ────────────────────────────

class TestSettings:
    def test_a_camera_that_stored_nothing_runs_on_the_defaults(self):
        cfg = {"regions": {"left": LEFT_HALF},
               "activities": [{"type": "stray_parking", "regions": ["left"], "params": {}}]}
        act = build(cfg)
        assert act.required_duration_s == 600
        assert act.vehicle_classes == {"car", "truck", "bus", "motorcycle"}
        assert set(act.spec.defaulted) == {"vehicle_classes", "required_duration_s"}

    def test_a_camera_value_reaches_the_runtime(self):
        act = build(config("stray_parking", required_duration_s=45, vehicle_classes=["truck"]))
        assert act.required_duration_s == 45 and act.vehicle_classes == {"truck"}
        assert act.spec.defaulted == () and act.spec.problems == {}

    @pytest.mark.parametrize("bad", ["600", True, -5, 0, 90000, float("nan"), [600]])
    def test_an_unusable_number_falls_back_to_the_default_and_is_reported(self, bad):
        act = build(config("stray_parking", required_duration_s=bad))
        assert act.required_duration_s == 600
        assert "required_duration_s" in act.spec.problems

    def test_a_frame_count_is_never_reinterpreted_as_seconds(self):
        act = build(config("stray_parking", required_frames=2400))
        assert act.required_duration_s == 600 and "required_duration_s" in act.spec.defaulted
        gathering = build(config("people_gathering", frame_accuracy=1800))
        assert gathering.required_duration_s == 60

    def test_the_deepstream_class_name_bike_is_mapped_explicitly(self):
        act = build(config("stray_parking", vehicle_classes=["car", "bike", "other_moving_machinary"]))
        assert act.vehicle_classes == {"car", "motorcycle"}
        assert "other_moving_machinary" in act.spec.problems["vehicle_classes"]

    def test_no_supported_vehicle_class_left_is_invalid(self):
        act = build(config("stray_parking", vehicle_classes=["other_moving_machinary"]))
        assert act.vehicle_classes == {"car", "truck", "bus", "motorcycle"}
        assert "vehicle_classes" in act.spec.problems

    def test_the_engine_reports_defaulted_and_unusable_settings(self):
        summary = ActivityEngine().configure(CAM, config("restricted_zone_entry", cooldown_s="soon"))
        s = summary["settings"]["restricted_zone_entry"]
        assert s["values"] == {"cooldown_s": 60.0} and "cooldown_s" in s["problems"]


# ── zones ────────────────────────────────────────────────────────────────────

class TestZones:
    def test_optional_no_zone_is_the_whole_frame(self):
        assert resolve_zones({"regions": []}, {"left": LEFT_HALF}, ZONE_OPTIONAL) == (FULL_FRAME,)
        assert resolve_zones({"regions": ["gone"]}, {}, ZONE_OPTIONAL) == (FULL_FRAME,)

    def test_optional_tripwire_only_is_not_widened(self):
        assert resolve_zones({"regions": ["w"]}, {"w": DOOR_LINE}, ZONE_OPTIONAL) == ()

    def test_required_no_zone_means_it_does_not_run(self):
        assert resolve_zones({"regions": []}, {}, ZONE_REQUIRED) == ()
        engine = ActivityEngine()
        summary = engine.configure(CAM, config("stray_parking", regions=()))
        assert summary["not_running_no_zone"] == ["stray_parking"] and not engine.has_work(CAM)

    def test_tripwire_rule_takes_only_lines(self):
        zones = resolve_zones({"regions": ["left", "w"]}, {"left": LEFT_HALF, "w": DOOR_LINE},
                              ZONE_TRIPWIRE)
        assert [z.key for z in zones] == ["w"]

    def test_a_configured_zone_is_honoured_not_widened(self):
        [zone] = resolve_zones({"regions": ["left"]}, {"left": LEFT_HALF}, ZONE_REQUIRED)
        assert zone.contains(0.25, 0.5) and not zone.contains(0.75, 0.5)


# ── the schedule ─────────────────────────────────────────────────────────────

def local_ts(hour: int) -> float:
    return time.mktime((2026, 9, 14, hour, 0, 0, 0, 0, -1))


class TestSchedule:
    def test_days_and_hours(self):
        ts = local_ts(12)
        today = ALL_DAYS[time.localtime(ts).tm_wday]
        assert Schedule.from_params({"active_days": [today]}).is_active(ts)
        assert not Schedule.from_params({"active_days": []}).is_active(ts)
        night = Schedule.from_params({"active_hours": {"start": "22:00", "end": "06:00"}})
        assert night.is_active(local_ts(23)) and not night.is_active(local_ts(12))

    @pytest.mark.parametrize("key", ["restricted_zone_entry", "people_gathering"])
    def test_every_implemented_activity_respects_the_schedule(self, key):
        act = build(config(key, active_days=[], required_duration_s=1))
        assert act.process(frame(obj("a"), obj("b", x=0.26))) == []


# ── restricted_zone_entry ───────────────────────────────────────────────────

class TestRestrictedZoneEntry:
    def test_a_person_outside_the_zone_raises_nothing(self):
        assert build(config("restricted_zone_entry")).process(frame(obj(x=0.75))) == []

    def test_a_person_inside_raises_an_event(self):
        [ev] = build(config("restricted_zone_entry")).process(frame(obj("p7", conf=0.77), ts=100.0))
        assert (ev.activity, ev.zone.key, ev.track_id, ev.object_class) == \
            ("restricted_zone_entry", "left", "p7", "person")
        assert ev.started_at == 100.0 and ev.confidence == 0.77
        assert ev.attributes["zone_name"] == "Left half" and ev.attributes["people_in_zone"] == 1

    def test_the_box_centre_decides_as_in_the_supplied_logic(self):
        act = build(config("restricted_zone_entry"))
        # Feet inside the left half, centre outside it.
        tall = TrackedObject("p1", "person", "person", 0.9, (0.45, 0.2, 0.60, 0.9), True, False)
        assert act.process(frame(tall)) == []

    def test_other_classes_are_ignored(self):
        assert build(config("restricted_zone_entry")).process(frame(car())) == []

    def test_one_event_per_stay_however_long_it_lasts(self):
        act = build(config("restricted_zone_entry", cooldown_s=5))
        events = [e for i in range(3000) for e in act.process(frame(obj("p1"), ts=i * 0.2))]  # 10 min
        assert len(events) == 1

    def test_leaving_rearms_and_entering_again_raises_a_new_event(self):
        act = build(config("restricted_zone_entry", cooldown_s=60))
        assert len(act.process(frame(obj("p1"), ts=0.0))) == 1
        assert act.process(frame(obj("p1", x=0.75), ts=30.0)) == []     # left the zone
        assert act.process(frame(obj("p1", x=0.75), ts=90.0)) == []
        [ev] = act.process(frame(obj("p1"), ts=100.0))                  # back inside
        assert ev.started_at == 100.0 and ev.track_id == "p1"
        assert act.process(frame(obj("p1"), ts=500.0)) == []            # the same stay

    def test_re_entering_within_the_cooldown_raises_nothing(self):
        act = build(config("restricted_zone_entry", cooldown_s=60))
        assert len(act.process(frame(obj("p1"), ts=0.0))) == 1
        act.process(frame(obj("p1", x=0.75), ts=1.0))                    # wavering on the edge: out…
        assert act.process(frame(obj("p1"), ts=2.0)) == []               # …and in again
        assert act.process(frame(obj("p1"), ts=120.0)) == []             # still that stay
        act.process(frame(obj("p1", x=0.75), ts=121.0))
        assert len(act.process(frame(obj("p1"), ts=122.0))) == 1         # past the cooldown

    def test_the_configured_cooldown_reaches_the_runtime(self):
        act = build(config("restricted_zone_entry", cooldown_s=5))
        assert act.cooldown_s == 5
        act.process(frame(obj(), ts=0.0))
        act.process(frame(obj(x=0.75), ts=1.0))
        assert act.process(frame(obj(), ts=4.9)) == []
        act.process(frame(obj(x=0.75), ts=5.1))
        assert len(act.process(frame(obj(), ts=5.2))) == 1

    def test_a_missed_detection_does_not_end_the_stay(self):
        act = build(config("restricted_zone_entry"))
        act.process(frame(obj("p1"), ts=0.0))
        act.process(frame(ts=0.2))                                       # the detector missed them
        assert act.process(frame(obj("p1"), ts=0.4)) == []

    def test_a_person_the_tracker_retires_is_armed_again(self):
        act = build(config("restricted_zone_entry", cooldown_s=5))
        assert len(act.process(frame(obj("p1"), ts=0.0))) == 1
        act.forget_tracks(frozenset({"p1"}))                             # walked out of view
        assert len(act.process(frame(obj("p1"), ts=10.0))) == 1

    def test_each_person_has_their_own_stay(self):
        act = build(config("restricted_zone_entry"))
        [first] = act.process(frame(obj("a"), ts=0.0))
        [second] = act.process(frame(obj("a"), obj("b", x=0.3), ts=1.0))  # b walks in; a stays
        assert (first.track_id, second.track_id) == ("a", "b")
        assert second.attributes["people_in_zone"] == 2
        assert act.process(frame(obj("a"), obj("b", x=0.3), ts=2.0)) == []

    def test_zones_keep_separate_stays(self):
        act = build(config("restricted_zone_entry", regions=("left", "right")))
        assert [e.zone.key for e in act.process(frame(obj("p", x=0.25), ts=1.0))] == ["left"]
        assert [e.zone.key for e in act.process(frame(obj("p", x=0.75), ts=2.0))] == ["right"]
        assert act.process(frame(obj("p", x=0.25), ts=3.0)) == []       # back left, within its cooldown

    def test_untracked_detections_raise_nothing(self):
        assert build(config("restricted_zone_entry")).process(frame(obj(None))) == []

    def test_no_zone_watches_the_whole_frame(self):
        [ev] = build(config("restricted_zone_entry", regions=())).process(frame(obj(x=0.95)))
        assert ev.zone.implicit and ev.to_ingest()["zone"] is None

    def test_people_entering_together_raise_one_event_each(self):
        events = build(config("restricted_zone_entry")).process(
            frame(obj("a", conf=0.5), obj("b", x=0.3, conf=0.95)))
        assert sorted((e.track_id, e.confidence) for e in events) == [("a", 0.5), ("b", 0.95)]
        assert all(e.attributes["people_in_zone"] == 2 for e in events)


# ── people_gathering ─────────────────────────────────────────────────────────

def pair(ts, a="a", b="b", x=0.25, gap=0.02):
    """Two people close together: feet `gap` apart, boxes 0.03 wide."""
    return frame(obj(a, x=x), obj(b, x=x + gap), ts=ts)


class TestPeopleGathering:
    def gathering(self, **params):
        params.setdefault("required_duration_s", 60)
        params.setdefault("last_time", 30)
        return build(config("people_gathering", **params))

    def test_one_person_is_not_a_gathering(self):
        act = self.gathering()
        assert act.process(frame(obj(), ts=0.0)) == [] and act.process(frame(obj(), ts=100.0)) == []

    def test_people_far_apart_are_not_a_gathering(self):
        act = self.gathering()
        for ts in (0.0, 70.0):
            assert act.process(frame(obj("a", x=0.1), obj("b", x=0.4), ts=ts)) == []

    def test_people_close_but_in_different_zones_are_not(self):
        act = self.gathering(regions=("left", "right"))
        for ts in (0.0, 70.0):
            assert act.process(pair(ts, x=0.495, gap=0.01)) == []

    def test_a_group_below_the_duration_raises_nothing(self):
        act = self.gathering()
        assert all(act.process(pair(ts)) == [] for ts in (0.0, 20.0, 59.0))

    def test_a_group_sustained_for_the_duration_raises_one_event(self):
        act = self.gathering()
        act.process(pair(0.0))
        act.process(pair(30.0))
        [ev] = act.process(pair(60.0))
        assert ev.activity == "people_gathering" and ev.zone.key == "left"
        assert ev.attributes["person_count"] == 2 and ev.attributes["track_ids"] == ["a", "b"]
        assert ev.attributes["sustained_s"] == 60.0

    def test_duration_is_time_not_frames(self):
        many_frames = self.gathering()
        assert all(many_frames.process(pair(i * 0.01)) == [] for i in range(3000))   # 30 s
        two_frames = self.gathering()
        two_frames.process(pair(0.0))
        assert len(two_frames.process(pair(61.0))) == 1

    def test_a_short_gap_keeps_the_timer_a_long_one_resets_it(self):
        short = self.gathering(last_time=30)
        short.process(pair(0.0))
        short.process(frame(ts=20.0))            # nobody for 20 s < 30 s
        assert len(short.process(pair(61.0))) == 1
        long = self.gathering(last_time=10)
        long.process(pair(0.0))
        long.process(frame(ts=20.0))             # 20 s > 10 s: reset
        assert long.process(pair(61.0)) == []

    def test_the_configured_duration_reaches_the_runtime(self):
        act = self.gathering(required_duration_s=5)
        act.process(pair(0.0))
        assert len(act.process(pair(5.0))) == 1

    def test_the_behaviour_settings_default_to_the_supplied_values(self):
        act = build({"regions": {"left": LEFT_HALF}, "activities": [
            {"type": "people_gathering", "regions": ["left"], "params": {}}]})
        assert (act.min_group_size, act.proximity_factor, act.cooldown_s) == (2, 1.35, 100.0)
        assert {"min_group_size", "proximity_factor", "cooldown_s"} <= set(act.spec.defaulted)

    def test_min_group_size_reaches_the_runtime(self):
        act = self.gathering(required_duration_s=5, min_group_size=3)
        assert act.min_group_size == 3
        act.process(pair(0.0))
        assert act.process(pair(5.0)) == []                              # two are not enough
        trio = lambda ts: frame(obj("a"), obj("b", x=0.27), obj("c", x=0.29), ts=ts)  # noqa: E731
        act.process(trio(10.0))
        [ev] = act.process(trio(15.0))
        assert ev.attributes["person_count"] == 3

    def test_proximity_factor_reaches_the_runtime(self):
        apart = lambda ts: pair(ts, gap=0.05)     # feet 96 px apart; boxes 57.6 px wide  # noqa: E731
        default = self.gathering(required_duration_s=5)                  # 1.35 × 57.6 = 77.8 px
        default.process(apart(0.0))
        assert default.process(apart(5.0)) == []
        wide = self.gathering(required_duration_s=5, proximity_factor=2.0)   # 115.2 px
        assert wide.proximity_factor == 2.0
        wide.process(apart(0.0))
        assert len(wide.process(apart(5.0))) == 1

    def test_cooldown_reaches_the_runtime(self):
        def run(act):
            act.process(pair(0.0))
            assert len(act.process(pair(10.0))) == 1
            act.process(frame(ts=50.0))                                  # the group left: timer resets
            act.process(pair(60.0, a="e", b="f"))
            return act.process(pair(70.0, a="e", b="f"))
        short = self.gathering(required_duration_s=10, last_time=30, cooldown_s=20)
        assert short.cooldown_s == 20
        assert len(run(short)) == 1                                      # cooled down at 30
        assert run(self.gathering(required_duration_s=10, last_time=30)) == []   # 100 s: cooling until 110

    @pytest.mark.parametrize("key, bad", [
        ("min_group_size", 2.5), ("min_group_size", 1), ("min_group_size", 51),
        ("proximity_factor", 0), ("proximity_factor", "wide"), ("cooldown_s", -1),
    ])
    def test_an_unusable_behaviour_setting_falls_back_and_is_reported(self, key, bad):
        act = build(config("people_gathering", **{key: bad}))
        assert (act.min_group_size, act.proximity_factor, act.cooldown_s) == (2, 1.35, 100.0)
        assert key in act.spec.problems

    def test_the_same_tracks_do_not_raise_again(self):
        act = self.gathering(required_duration_s=10)
        act.process(pair(0.0))
        assert len(act.process(pair(10.0))) == 1
        for ts in (20.0, 60.0, 200.0, 400.0):       # the SAME two, already reported
            assert act.process(pair(ts)) == []

    def test_a_new_group_after_the_cooldown_raises(self):
        act = self.gathering(required_duration_s=10, last_time=30)
        act.process(pair(0.0))
        assert len(act.process(pair(10.0))) == 1     # cooling until 10 + COOLDOWN_S
        act.process(frame(ts=50.0))                  # the group left: timer resets
        start = 10.0 + COOLDOWN_S + 5
        act.process(pair(start, a="e", b="f"))
        assert len(act.process(pair(start + 10, a="e", b="f"))) == 1

    def test_a_group_sustained_during_the_cooldown_is_absorbed_as_supplied(self):
        """The supplied code marks a cluster's ids as reported BEFORE checking the
        cooldown, so a group that reaches the duration while the zone is cooling
        is never reported later either. Kept as supplied — see the report."""
        act = self.gathering(required_duration_s=10, last_time=30)
        act.process(pair(0.0))
        act.process(pair(10.0))                      # event; cooling until 110
        act.process(pair(20.0, a="c", b="d"))        # sustained, but cooling
        for ts in (111.0, 125.0, 200.0):
            assert act.process(pair(ts, a="c", b="d")) == []

    def test_tracker_ids_define_the_group_and_retired_ids_are_forgotten(self):
        act = self.gathering(required_duration_s=10)
        act.process(pair(0.0))
        act.process(pair(10.0))
        assert act.process(pair(200.0)) == []        # the same ids: already reported
        act.forget_tracks(frozenset({"a", "b"}))     # the tracker retired them
        events = [e for ts in (201.0, 211.0) for e in act.process(pair(ts))]
        assert len(events) == 1                      # the reused ids are a new group
        untracked = self.gathering(required_duration_s=1)
        untracked.process(frame(obj(None), obj(None, x=0.27), ts=0.0))
        assert untracked.process(frame(obj(None), obj(None, x=0.27), ts=5.0)) == []

    def test_standing_still_keeps_being_evaluated(self):
        act = self.gathering(required_duration_s=30)
        events = [e for i in range(0, 160) for e in act.process(pair(i * 0.2))]   # 5 FPS, no motion
        assert len(events) == 1

    def test_no_zone_is_the_whole_frame(self):
        act = self.gathering(regions=(), required_duration_s=1)
        act.process(pair(0.0, x=0.9))
        [ev] = act.process(pair(1.0, x=0.9))
        assert ev.zone.implicit

    def test_the_supplied_union_find_joins_chains(self):
        clusters = find_clusters_by_zone({"z": [["a", "b"], ["b", "c"]], "y": [["d", "e"]]})
        assert sorted(map(sorted, clusters["z"])) == [["a", "b", "c"]]
        assert sorted(map(sorted, clusters["y"])) == [["d", "e"]]


# ── stray_parking ────────────────────────────────────────────────────────────

class TestStrayParking:
    def parking(self, **params):
        params.setdefault("required_duration_s", 600)
        return build(config("stray_parking", **params))

    def test_non_vehicles_are_ignored(self):
        act = self.parking(required_duration_s=1)
        act.process(frame(obj("p"), ts=0.0))
        assert act.process(frame(obj("p"), ts=10.0)) == []

    def test_only_configured_vehicle_classes_count(self):
        act = self.parking(required_duration_s=1, vehicle_classes=["car"])
        act.process(frame(car("t", label="truck"), ts=0.0))
        assert act.process(frame(car("t", label="truck"), ts=10.0)) == []

    def test_a_vehicle_outside_the_zone_raises_nothing(self):
        act = self.parking(required_duration_s=1)
        act.process(frame(car(x=0.75), ts=0.0))
        assert act.process(frame(car(x=0.75), ts=10.0)) == []

    def test_a_vehicle_inside_for_less_than_the_duration_raises_nothing(self):
        act = self.parking()
        assert all(act.process(frame(car(), ts=ts)) == [] for ts in (0.0, 300.0, 599.0))

    def test_a_vehicle_parked_for_the_duration_raises_one_event_for_its_track(self):
        act = self.parking()
        act.process(frame(car("c9"), ts=1000.0))
        [ev] = act.process(frame(car("c9"), ts=1600.0))
        assert (ev.activity, ev.track_id, ev.object_class, ev.zone.key) == \
            ("stray_parking", "c9", "car", "left")
        assert ev.attributes["parked_s"] == 600.0 and ev.confidence == 0.8
        assert act.process(frame(car("c9"), ts=5000.0)) == []

    def test_the_configured_duration_reaches_the_runtime(self):
        act = self.parking(required_duration_s=30)
        act.process(frame(car(), ts=0.0))
        assert len(act.process(frame(car(), ts=30.0))) == 1

    def test_leaving_the_zone_resets_the_timer(self):
        act = self.parking(required_duration_s=100)
        act.process(frame(car(), ts=0.0))
        act.process(frame(car(x=0.75), ts=50.0))
        act.process(frame(car(), ts=60.0))
        assert act.process(frame(car(), ts=120.0)) == []
        assert len(act.process(frame(car(), ts=160.0))) == 1

    def test_frames_where_the_detector_misses_it_do_not_reset(self):
        act = self.parking(required_duration_s=100)
        act.process(frame(car(), ts=0.0))
        act.process(frame(ts=50.0))
        assert len(act.process(frame(car(), ts=100.0))) == 1

    def test_a_retired_track_loses_its_timer(self):
        act = self.parking(required_duration_s=100)
        act.process(frame(car("c1"), ts=0.0))
        act.forget_tracks(frozenset({"c1"}))
        assert act.process(frame(car("c1"), ts=100.0)) == []

    def test_a_stationary_vehicle_keeps_being_timed(self):
        act = self.parking(required_duration_s=60)
        events = [e for i in range(0, 400) for e in act.process(frame(car(), ts=i * 0.2))]
        assert len(events) == 1

    def test_a_zone_is_required(self):
        engine = ActivityEngine()
        engine.configure(CAM, config("stray_parking", regions=(), required_duration_s=1))
        assert not engine.has_work(CAM)


# ── entry_exit_WLE_logs ─────────────────────────────────────────────────────

def person(track="p1", x=0.5, top=0.4, bottom=0.6, w=0.06, confirmed=True,
           label="person", domain="person") -> TrackedObject:
    """A tracked person whose box spans rows `top`..`bottom`, centred on x."""
    return TrackedObject(track_id=track, domain=domain, label=label, confidence=0.8,
                         bbox=(x - w / 2, top, x + w / 2, bottom), confirmed=confirmed,
                         is_new=False)


#: Where a person's WHOLE box is relative to DOOR_LINE (y = 0.5, A left → B right).
BELOW = {"top": 0.55, "bottom": 0.75}
ON = {"top": 0.4, "bottom": 0.6}
ABOVE = {"top": 0.25, "bottom": 0.45}


class TestEntryExit:
    """DOOR_LINE runs from A (left) to B (right) with direction a2b, so its entry
    arrow points UP the frame: walking from below the line to above it is an
    Entry, from above to below an Exit. DOOR_B2A is the same line reversed."""

    REGIONS = {"door": DOOR_LINE, "door_b2a": DOOR_B2A, "left": LEFT_HALF,
               "short": {**SHORT_LINE, "direction": "a2b"},
               "unset": {**DOOR_LINE, "name": "Unset line", "direction": "both"}}

    def entry_exit(self, *wires, **params):
        return build(config("entry_exit_WLE_logs", regions=wires or ("door",),
                            region_map=self.REGIONS, **params))

    @staticmethod
    def walk(act, *steps, track="p1", x=0.5, start=0.0, every=0.2, **kw):
        """One person through `steps`, one frame each; every event raised."""
        return [e for i, step in enumerate(steps)
                for e in act.process(frame(person(track, x=x, **step, **kw), ts=start + i * every))]

    @staticmethod
    def directions(events):
        return [e.attributes["direction"] for e in events]

    def test_crossing_in_the_entry_direction_is_one_entry(self):
        [ev] = self.walk(self.entry_exit(), BELOW, ON, ABOVE, track="p7", start=100.0)
        assert ev.attributes["direction"] == "entry"
        assert (ev.activity, ev.zone.key, ev.track_id, ev.object_class) == \
            ("entry_exit_WLE_logs", "door", "p7", "person")
        assert ev.started_at == pytest.approx(100.4) and ev.confidence == 0.8
        assert (ev.attributes["tripwire_id"], ev.attributes["tripwire_name"]) == ("door", "Door line")
        wire = ev.to_ingest()
        assert wire["zone"] == "door" and wire["attributes"]["direction"] == "entry"
        assert wire["attributes"]["full_frame_zone"] is False

    def test_crossing_the_other_way_is_one_exit(self):
        assert self.directions(self.walk(self.entry_exit(), ABOVE, ON, BELOW)) == ["exit"]

    def test_the_opposite_of_the_entry_direction_is_always_exit(self):
        assert self.directions(self.walk(self.entry_exit("door"), BELOW, ON, ABOVE)) == ["entry"]
        assert self.directions(self.walk(self.entry_exit("door_b2a"), BELOW, ON, ABOVE)) == ["exit"]
        assert self.directions(self.walk(self.entry_exit("door_b2a"), ABOVE, ON, BELOW)) == ["entry"]

    def test_touching_the_line_and_going_back_is_nothing(self):
        assert self.walk(self.entry_exit(), BELOW, ON, ON, BELOW) == []
        assert self.walk(self.entry_exit(), ABOVE, ON, ABOVE) == []

    def test_standing_on_the_line_raises_nothing(self):
        assert self.walk(self.entry_exit(), BELOW, *[ON] * 100) == []

    def test_staying_on_the_far_side_raises_nothing_more(self):
        assert len(self.walk(self.entry_exit(), BELOW, ON, ABOVE, *[ABOVE] * 100)) == 1

    def test_every_genuine_crossing_by_the_same_person_counts(self):
        steps = (BELOW, ON, ABOVE, ABOVE, ON, BELOW, BELOW, ON, ABOVE)
        assert self.directions(self.walk(self.entry_exit(), *steps)) == ["entry", "exit", "entry"]

    def test_a_person_missed_for_a_few_frames_is_counted_once(self):
        act = self.entry_exit()
        act.process(frame(person(**BELOW), ts=0.0))
        act.process(frame(person(**ON), ts=0.2))
        act.process(frame(ts=0.4))                                   # not detected
        act.process(frame(ts=1.0))
        [ev] = act.process(frame(person(**ABOVE), ts=2.0))
        assert ev.attributes["direction"] == "entry" and ev.started_at == 2.0
        assert self.walk(act, ABOVE, ABOVE, start=2.2) == []

    def test_a_track_the_tracker_retires_mid_crossing_is_not_counted(self):
        act = self.entry_exit()
        self.walk(act, BELOW, ON)
        act.forget_tracks(frozenset({"p1"}))
        assert self.walk(act, ABOVE, ABOVE, start=10.0) == []

    def test_two_people_crossing_are_two_crossings(self):
        act = self.entry_exit()
        events = []
        for i, (a, b) in enumerate(((BELOW, ABOVE), (ON, ON), (ABOVE, BELOW))):
            events += act.process(frame(person("a", x=0.4, **a), person("b", x=0.6, **b), ts=i * 0.2))
        assert sorted((e.track_id, e.attributes["direction"]) for e in events) == \
            [("a", "entry"), ("b", "exit")]

    def test_the_whole_box_must_clear_the_line_not_the_feet(self):
        # Walking down: the feet are past the line in the middle frame, the box still spans it.
        [ev] = self.walk(self.entry_exit(), ABOVE, {"top": 0.45, "bottom": 0.6}, BELOW)
        assert ev.attributes["direction"] == "exit" and ev.started_at == pytest.approx(0.4)

    def test_a_crossing_between_frames_counts_when_the_path_meets_the_tripwire(self):
        assert self.directions(self.walk(self.entry_exit(), BELOW, ABOVE)) == ["entry"]

    def test_passing_beside_the_end_of_the_tripwire_is_not_a_crossing(self):
        assert self.walk(self.entry_exit(), BELOW, ON, ABOVE, x=0.9) == []   # DOOR_LINE ends at x = 0.8
        assert self.walk(self.entry_exit(), BELOW, ABOVE, x=0.95) == []

    def test_each_tripwire_counts_its_own_crossings(self):
        # SHORT_LINE spans x 0.45–0.55 on the same row as DOOR_LINE.
        events = self.walk(self.entry_exit("door", "short"), BELOW, ON, ABOVE, x=0.3)
        assert [e.zone.key for e in events] == ["door"]
        both = self.walk(self.entry_exit("door", "short"), BELOW, ON, ABOVE, x=0.5)
        assert sorted(e.zone.key for e in both) == ["door", "short"]

    def test_a_crossing_before_confirmation_is_raised_once_the_track_is_confirmed(self):
        act = self.entry_exit()
        assert self.walk(act, BELOW, ON, ABOVE, confirmed=False) == []
        [ev] = act.process(frame(person(**ABOVE), ts=5.0))
        assert ev.attributes["direction"] == "entry" and ev.started_at == pytest.approx(0.4)
        assert act.process(frame(person(**ABOVE), ts=5.2)) == []

    def test_an_unconfirmed_flicker_is_forgotten_with_its_track(self):
        act = self.entry_exit()
        self.walk(act, BELOW, ON, ABOVE, track="f1", confirmed=False)
        act.forget_tracks(frozenset({"f1"}))
        assert act.process(frame(person("f1", **ABOVE), ts=5.0)) == []

    def test_untracked_detections_and_other_classes_are_ignored(self):
        assert self.walk(self.entry_exit(), BELOW, ON, ABOVE, track=None) == []
        assert self.walk(self.entry_exit(), BELOW, ON, ABOVE, label="car", domain="vehicles") == []

    def test_the_schedule_applies(self):
        assert self.walk(self.entry_exit(active_days=[]), BELOW, ON, ABOVE) == []

    def test_a_tripwire_without_an_entry_direction_is_not_counted(self):
        act = self.entry_exit("unset")
        assert act.zones == () and act.unoriented == ("Unset line",)
        assert self.walk(act, BELOW, ON, ABOVE) == []
        engine = ActivityEngine()
        summary = engine.configure(CAM, config("entry_exit_WLE_logs", regions=("unset",),
                                               region_map=self.REGIONS))
        assert summary["not_running_no_zone"] == ["entry_exit_WLE_logs"] and not engine.has_work(CAM)
        assert [z.key for z in self.entry_exit("door", "unset").zones] == ["door"]

    def test_without_a_tripwire_it_does_not_run_and_is_never_widened(self):
        for regions in ((), ("left",)):
            engine = ActivityEngine()
            summary = engine.configure(CAM, config("entry_exit_WLE_logs", regions=regions,
                                                   region_map=self.REGIONS))
            assert summary["not_running_no_zone"] == ["entry_exit_WLE_logs"]
            assert not engine.has_work(CAM)

    def test_crossings_reach_the_engine_and_retirement_clears_them(self):
        engine = ActivityEngine()
        engine.configure(CAM, config("entry_exit_WLE_logs", regions=("door",), region_map=self.REGIONS))
        events = [e for i, step in enumerate((BELOW, ON, ABOVE))
                  for e in engine.observe(frame(person(**step), ts=i * 0.2))]
        assert self.directions(events) == ["entry"]
        engine.observe(frame(person(**ON), ts=1.0))
        engine.forget_tracks(["p1"])
        assert engine.observe(frame(person(**BELOW), ts=2.0)) == []


# ── the engine ───────────────────────────────────────────────────────────────

class TestEngine:
    def test_events_are_handed_to_the_sink(self):
        got = []
        engine = ActivityEngine(emit=got.extend)
        engine.configure(CAM, config("restricted_zone_entry"))
        engine.observe(frame(obj()))
        assert [e.activity for e in got] == ["restricted_zone_entry"]

    def test_reasserting_the_same_config_keeps_activity_memory(self):
        engine = ActivityEngine()
        engine.configure(CAM, config("restricted_zone_entry"))
        engine.observe(frame(obj(), ts=0.0))
        engine.configure(CAM, config("restricted_zone_entry"))
        assert engine.observe(frame(obj(), ts=10.0)) == []

    def test_a_changed_config_rebuilds_the_activity(self):
        engine = ActivityEngine()
        engine.configure(CAM, config("restricted_zone_entry"))
        engine.observe(frame(obj(), ts=0.0))
        engine.configure(CAM, config("restricted_zone_entry", cooldown_s=5))
        assert len(engine.observe(frame(obj(), ts=10.0))) == 1

    def test_people_gathering_settings_reach_the_running_activity(self):
        summary = ActivityEngine().configure(CAM, config(
            "people_gathering", regions=(), min_group_size=4, proximity_factor=2.5, cooldown_s=30))
        settings = summary["settings"]["people_gathering"]
        assert (settings["values"]["min_group_size"], settings["values"]["proximity_factor"],
                settings["values"]["cooldown_s"]) == (4, 2.5, 30.0)
        assert settings["problems"] == {}

    def test_domains_come_from_running_activities(self):
        engine = ActivityEngine()
        engine.configure(CAM, {"regions": {"left": LEFT_HALF}, "activities": [
            {"type": "stray_parking", "regions": ["left"], "params": {}},
            {"type": "people_gathering", "regions": [], "params": {}}]})
        assert engine.wanted_domains(CAM) == {"vehicles", "person"}

    def test_retired_tracks_reach_every_activity(self):
        engine = ActivityEngine()
        engine.configure(CAM, config("stray_parking", required_duration_s=10))
        engine.observe(frame(car("c1"), ts=0.0))
        engine.forget_tracks(["c1"])
        assert engine.observe(frame(car("c1"), ts=10.0)) == []

    def test_one_failing_activity_does_not_cost_the_others(self):
        class Broken(Activity):
            definition = ActivityDefinition(key="broken", label="Broken", description="",
                                            color="#000000", status=AVAILABLE,
                                            zone_rule=ZONE_OPTIONAL, domains=frozenset({"person"}))

            def evaluate(self, ctx):
                raise RuntimeError("boom")

        engine = ActivityEngine(registry={**ACTIVITY_CLASSES, "broken": Broken})
        cfg = config("restricted_zone_entry")
        cfg["activities"].insert(0, {"type": "broken", "regions": [], "params": {}})
        engine.configure(CAM, cfg)
        assert len(engine.observe(frame(obj()))) == 1
        assert engine.errors == 1 and "boom" in engine.last_error


# ── the event, as the VMS ingest reads it ───────────────────────────────────

def test_an_event_is_shaped_for_the_vms_ingest():
    act = build(config("stray_parking", required_duration_s=1))
    act.process(frame(car("t9"), ts=1_789_000_000.0))
    [ev] = act.process(frame(car("t9"), ts=1_789_000_001.25))
    wire = ev.to_ingest()
    assert set(wire) == {"id", "sensor_id", "activity", "zone", "started_at", "ended_at",
                         "confidence", "track_id", "object_class", "attributes", "source"}
    assert (wire["sensor_id"], wire["activity"], wire["zone"], wire["source"]) == \
        (CAM, "stray_parking", "left", "cpu")
    assert wire["started_at"] == "2026-09-10T00:26:41.250000+00:00"
    assert wire["attributes"]["parked_since"] == "2026-09-10T00:26:40+00:00"
    assert ev.to_ingest()["id"] == wire["id"]


SAMPLE = {"regions": {"region_1": {"kind": "zone", "name": "Zone 1", "color": "#4fd1c5",
                                   "points": [[0.1, 0.2], [0.9, 0.2], [0.5, 0.9]],
                                   "direction": None}},
          "activities": [{"type": "person_detection", "regions": ["region_1"],
                          "params": {"active_hours": None, "active_days": ["Mon", "Tue"],
                                     "person_classes": ["person"], "min_confidence": 0.5}}]}


def test_the_fingerprint_is_pinned_and_matches_camera_mgmt():
    assert config_fingerprint(SAMPLE) == "a1a5552606b565b1"
    assert config_fingerprint({}) == config_fingerprint(None) == "3e3462a9e90b2edd"
