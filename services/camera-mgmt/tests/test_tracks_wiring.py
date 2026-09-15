"""The two-namespace invariant that the sub track rests on.

There are two name spaces in this product and confusing them is silent.

  CAMERA SLUG    what per-camera facts are keyed on — the re-stamp flag,
                 health, reconnect backoff, permissions, audit.
  RECORDING NAME what the relay and the NVR build one path per: the bare slug
                 for the main track, `<slug>_sub` for the sub.

`models.py` owns the conversion in both directions (`sub_recording_name`,
`owning_slug`) and `services/tracks.py` owns the enumeration. Its docstring is
explicit about why: "One definition means no surface can silently drift — the
failure mode being a sub that gets recorded but never groomed, or removed from
the relay but left recording in the NVR."

test_sub_track_wiring.py already pins the surfaces that drifted once. These
tests pin the RULES those surfaces broke, so the next surface is caught before
it has a bug filed against it:

  1. Nobody hand-builds a track name. The `_sub` literal lives in models.py.
  2. The surfaces that project cameras onto recording names go through
     tracks.py rather than reading `sub_track` and deciding for themselves.
  3. The conversion round-trips, and the slug space stays disjoint from the
     track space — which is the assumption `owning_slug` is safe under, and
     the assumption that failed when a re-stamped camera kept feeding its sub
     from the same broken clock.
  4. A decision about RELAY paths asks the RELAY's ownership, not the
     recorder's. The two are different questions and the answers differ by a
     real path — see section 4.

Tests 1, 2 and 4a are structural (ast/text over the source). Tests 3 and 4b-c
are behavioural.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(ROOT))

from backend.models import (  # noqa: E402
    SUB_TRACK_SUFFIX,
    generate_slug,
    owning_slug,
    sub_recording_name,
    validate_slug_format,
)

# models.py defines the suffix and both conversions; it is allowed to spell the
# literal. Nothing else is — everything else imports SUB_TRACK_SUFFIX.
SUFFIX_LITERAL_OWNER = "models.py"

# The surfaces that build one path per TRACK. tracks.py exists for exactly
# these two, per its docstring: "Only two surfaces in the product need to know
# that — the relay and the NVR."
TRACK_PROJECTING_MODULES = [
    BACKEND / "services" / "relay.py",
    BACKEND / "services" / "nvr_client.py",
]


def _python_files():
    return [p for p in BACKEND.rglob("*.py") if "__pycache__" not in p.parts]


# ── 1. Nobody hand-builds a track name ─────────────────────────────────────

def test_the_sub_suffix_literal_lives_in_exactly_one_file():
    """A hand-written "_sub" is a second definition of the track namespace.

    It is how a surface ends up agreeing with models.py right up until the
    suffix changes, or until someone writes `f"{slug}_sub"` for a camera whose
    sub does not exist. Import SUB_TRACK_SUFFIX, or call sub_recording_name().
    """
    offenders = []
    for path in _python_files():
        if path.name == SUFFIX_LITERAL_OWNER:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            # Only string CONSTANTS, so a comment or a docstring mentioning
            # `_sub` in prose does not trip this. Comments carry the reasoning
            # in this repo and must stay free to say the word.
            if isinstance(node, ast.Constant) and node.value == SUB_TRACK_SUFFIX:
                offenders.append(f"{path.relative_to(BACKEND)}:{node.lineno}")
    assert not offenders, (
        f"the literal {SUB_TRACK_SUFFIX!r} appears outside models.py at "
        f"{offenders}. Use SUB_TRACK_SUFFIX / sub_recording_name() / "
        f"owning_slug() so the track namespace has one definition."
    )


def test_no_module_builds_a_sub_name_with_an_f_string():
    """`f"{slug}_sub"` is the same bug as above wearing a different syntax, and
    an f-string hides the literal from the constant check — its pieces are not
    ast.Constant nodes of "_sub".

    Matched structurally, on an interpolation followed by a part ENDING in the
    suffix, rather than by text: `"usable_as_sub"` and `"no_usable_sub"` in
    substream.py are ordinary vocabulary, not track names, and a substring
    search flags them.
    """
    offenders = []
    for path in _python_files():
        if path.name == SUFFIX_LITERAL_OWNER:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            parts = node.values
            for i, part in enumerate(parts):
                if not (isinstance(part, ast.Constant)
                        and isinstance(part.value, str)
                        and part.value.endswith(SUB_TRACK_SUFFIX)):
                    continue
                # Only a suffix grafted onto something interpolated is a
                # constructed name; a bare f-string with no placeholder before
                # it is just text.
                if i > 0 and isinstance(parts[i - 1], ast.FormattedValue):
                    offenders.append(f"{path.relative_to(BACKEND)}:{node.lineno}")
    assert not offenders, (
        f"a sub track name looks hand-built at {offenders}; "
        f"call models.sub_recording_name() instead"
    )


# ── 2. The projecting surfaces go through tracks.py ────────────────────────

@pytest.mark.parametrize(
    "module", TRACK_PROJECTING_MODULES, ids=lambda p: p.name
)
def test_a_track_projecting_module_goes_through_tracks(module):
    """These two decide WHICH streams of a camera exist. Both must ask
    tracks.py rather than reading `sub_track` and deciding for themselves —
    that divergence is how a sub was removed from the relay but left recording
    in the NVR."""
    source = module.read_text()
    assert "tracks" in source, f"{module.name} no longer references tracks.py"
    tree = ast.parse(source)
    imports_tracks = any(
        (isinstance(n, ast.ImportFrom) and n.module and "tracks" in n.module)
        or (isinstance(n, ast.ImportFrom)
            and any("tracks" in a.name for a in n.names))
        for n in ast.walk(tree)
    )
    assert imports_tracks, (
        f"{module.name} builds per-track paths but does not import tracks.py. "
        f"Enumerate with tracks_for()/relay_tracks_for() so the relay and the "
        f"NVR cannot disagree about what a camera's tracks are."
    )


def test_tracks_is_the_only_module_that_enumerates_both_kinds():
    """tracks.py must keep exporting the enumeration the others depend on.
    Renaming these without updating the callers is caught by the test above;
    deleting them is caught here."""
    from backend.services import tracks

    for fn in ("tracks_for", "relay_tracks_for", "recording_names",
               "relay_names"):
        assert callable(getattr(tracks, fn, None)), (
            f"tracks.{fn} is gone; the relay and the NVR project cameras onto "
            f"recording names through it"
        )


# ── 3. The two namespaces stay disjoint and round-trip ─────────────────────

def test_a_track_name_converts_back_to_the_camera_that_owns_it():
    """The pair the whole design rests on. If this stops round-tripping, every
    per-camera fact looked up from a relay path reads a key nothing writes —
    and reads as false, which is the silent half."""
    slug = "gate-a1b2"
    assert owning_slug(sub_recording_name(slug)) == slug
    assert owning_slug(slug) == slug, "a main track's name is the bare slug"


def test_a_generated_slug_can_never_look_like_a_sub_track():
    """`owning_slug` is only safe because no real slug ends in the suffix —
    it says so in its own docstring. generate_slug always ends `-<4 alnum>`,
    so assert that rather than trusting the comment."""
    for name in ("gate", "gate_sub", "Camera _SUB", "sub", "", "front door"):
        slug = generate_slug(name)
        assert not slug.endswith(SUB_TRACK_SUFFIX), (
            f"generate_slug({name!r}) produced {slug!r}, which owning_slug "
            f"would attribute to a different camera"
        )
        assert owning_slug(slug) == slug


def test_an_operator_cannot_name_a_camera_into_the_track_namespace():
    """A camera slugged `foo_sub` would share a recording name with camera
    `foo`'s sub track and the two would overwrite each other's footage.

    Asserted on the rejection, not on the message: today the underscore is
    refused by the character pattern and the explicit `_sub` guard behind it is
    unreachable — deliberately so, per its comment, "the explicit guard so the
    reservation survives a future pattern change". Pinning the message here
    would pin WHICH of the two fired, and a future pattern that allowed
    underscores would then fail this test for doing the right thing.
    """
    with pytest.raises(ValueError):
        validate_slug_format(f"camera{SUB_TRACK_SUFFIX}")


def test_the_reservation_survives_a_pattern_that_allows_the_suffix(monkeypatch):
    """The guard the test above cannot reach, reached.

    `validate_slug_format` refuses `camera_sub` on the underscore, so the
    explicit reserved-suffix check behind it never runs today. It exists for
    the day the pattern changes — which means it is exactly the kind of code
    that rots unnoticed, because no test drives it.

    Widening the pattern is the cheapest way to stand in for that day: with
    underscores admitted, `camera_sub` must still be refused, and refused by
    the reservation rather than sailing through.
    """
    import re

    from backend import models

    monkeypatch.setattr(models, "_SLUG_VALID", re.compile(r"^[a-z0-9_-]+$"))
    # Sanity: the widened pattern really does admit the name, so a pass below
    # would mean the reservation fired, not that the pattern rejected it again.
    assert models._SLUG_VALID.match(f"camera{SUB_TRACK_SUFFIX}")

    with pytest.raises(ValueError, match="reserved"):
        validate_slug_format(f"camera{SUB_TRACK_SUFFIX}")

    # And an ordinary name still passes under the widened pattern, so the test
    # above is not passing merely because everything raises.
    assert validate_slug_format("gate-a1b2") == "gate-a1b2"


# ── 4. Relay decisions use RELAY ownership ─────────────────────────────────
#
# THE THIRTEENTH INSTANCE OF THIS BUG CLASS, turned into a rule.
#
# There are two ownership questions and they are not interchangeable:
#
#   tracks_for()/recording_names()        what the RECORDER should run.
#                                         The sub only when it is switched on,
#                                         because recording one costs a second
#                                         ffmpeg and ~10-25% more disk.
#   relay_tracks_for()/relay_names()      what the RELAY may legitimately hold.
#                                         A strict superset: a RESOLVED sub is
#                                         carried on demand so the live view can
#                                         fall back to it on a codec the browser
#                                         cannot decode.
#
# The health monitor's orphan sweep deletes every relay path it does not
# recognise, and it asked the recorder's question. `substream.resolve()` always
# stores `recording_enabled: False`, so every camera with a discovered sub was
# in the one state the recorder does not count — the sweep deleted the path ~17
# ms after each reconcile re-added it, once per poll, forever, and the live
# view's H.265 fallback had nothing to attach to.
#
# Keeping the two concepts separate is deliberate and must stay that way. The
# rule is not "always use the relay set"; it is "ask the question that matches
# the thing you are deciding about".

# What enumerating the relay's ACTUAL paths looks like. A function that calls
# this is, by definition, making a decision about relay paths.
RELAY_PATH_ENUMERATORS = {"list_active_paths"}
# Asking the recorder's question while holding relay paths is the defect.
RECORDER_ONLY_OWNERSHIP = {"recording_names", "tracks_for"}


def _called_names(node: ast.AST) -> set[str]:
    """Every function name called anywhere under `node`, bare or attributed."""
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def test_a_relay_path_decision_never_asks_the_recorder_for_ownership():
    """THE ARCHITECTURAL RULE. Structural on purpose: it has to catch the next
    surface, not re-catch health.py.

    Any function that enumerates the relay's live paths is deciding something
    about relay paths. If it also derives an ownership set, that set must come
    from the relay's enumeration. Reintroducing

        known_names = tracks.recording_names(...)   # or tracks_for(...)

    into such a function fails here.

    If a future function legitimately needs BOTH answers, split it: the point of
    this test is that mixing them in one scope is how they get confused, and
    that confusion is invisible at runtime.
    """
    offenders = []
    for path in _python_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            called = _called_names(node)
            if not (called & RELAY_PATH_ENUMERATORS):
                continue
            wrong = called & RECORDER_ONLY_OWNERSHIP
            if wrong:
                offenders.append(
                    f"{path.relative_to(BACKEND)}:{node.lineno} {node.name}() "
                    f"enumerates relay paths but asks {sorted(wrong)}"
                )
    assert not offenders, (
        "a relay-path decision is using RECORDER ownership:\n  "
        + "\n  ".join(offenders)
        + "\n\nThe relay carries a resolved-but-unrecorded sub that the "
          "recorder does not. Use tracks.relay_names()/relay_tracks_for() for "
          "anything judging a relay path; recording_names()/tracks_for() answer "
          "what the NVR should RUN."
    )


def test_relay_ownership_really_is_wider_than_recorder_ownership():
    """The rule above is only worth enforcing if the two sets actually differ.

    Behavioural, and it pins the exact shape the defect turned on: a sub that is
    resolved but NOT recording. If this ever stops differing, either the product
    changed or someone collapsed the helpers, and the structural test above
    silently stops protecting anything.
    """
    from types import SimpleNamespace

    from backend.services import tracks

    cam = SimpleNamespace(
        slug="gate-a1b2",
        rtsp_url="rtsp://u:pw@10.0.0.5:554/main",
        # Exactly what substream.resolve() stores, every time.
        sub_track={"url_raw": "rtsp://10.0.0.5:554/sub", "codec": "h264",
                   "width": 704, "height": 576, "recording_enabled": False},
    )
    sub = sub_recording_name("gate-a1b2")

    assert sub in tracks.relay_names([cam]), (
        "the relay must carry a resolved sub on demand — it is the live view's "
        "only fallback on a camera the browser cannot decode"
    )
    assert sub not in tracks.recording_names([cam]), (
        "recording a sub is opt-in; counting it here would start a second "
        "ffmpeg nobody asked for"
    )


def test_the_two_ownership_questions_are_not_collapsed():
    """Neither helper may become an alias for the other.

    Collapsing them would make the structural test above pass trivially while
    reintroducing the bug everywhere at once — the relay would either lose the
    resolved sub again, or the NVR would start recording every discovered one.
    """
    from types import SimpleNamespace

    from backend.services import tracks

    recording = SimpleNamespace(
        slug="gate-a1b2", rtsp_url="rtsp://u:pw@10.0.0.5:554/main",
        sub_track={"url_raw": "rtsp://10.0.0.5:554/sub", "codec": "h264",
                   "width": 704, "height": 576, "recording_enabled": True})
    resolved = SimpleNamespace(
        slug="gate-a1b2", rtsp_url="rtsp://u:pw@10.0.0.5:554/main",
        sub_track={"url_raw": "rtsp://10.0.0.5:554/sub", "codec": "h264",
                   "width": 704, "height": 576, "recording_enabled": False})

    # They agree where they should: a RECORDING sub is both recorded and relayed.
    assert tracks.relay_names([recording]) == tracks.recording_names([recording])
    # And differ where they must.
    assert tracks.relay_names([resolved]) != tracks.recording_names([resolved]), (
        "relay_names and recording_names answer the same for a resolved sub — "
        "the two ownership concepts have been collapsed"
    )
