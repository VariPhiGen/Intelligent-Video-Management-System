"""A camera that was just added runs no AI.

Registering a camera is a RECORDING decision. Indexing the people and vehicles
in it is a second decision — it costs CPU, it costs disk, and because a person
crop is personal data it needs its own justification. `search_indexing` shipped
as an opt-OUT defaulting true (migration 030), so onboarding a camera made both
decisions at once and silently, and an operator who wanted Smart Search on ONE
camera had to go and switch it off on every other.

Migration 038 moves the default. These tests pin the three places the default
is expressed, because they are in three different languages and nothing else
makes them agree:

  * the ORM column, which is what an INSERT through the API uses,
  * the response model, whose default is what a payload omitting the field
    means to the SPA,
  * the two `desired_sets` rules, which decide whether the camera costs a
    decode and a detector pass at all.

NOT ASSERTED HERE, deliberately: that existing cameras keep indexing. That is
the migration's job (038 alters the default only, it touches no row) and this
suite has no database. Emptying a working appliance's index is the failure this
change must not cause, and it is called out in the migration's docstring.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.models import Camera, CameraResponse  # noqa: E402
from backend.services import analytics_client, frames_client  # noqa: E402


def test_the_orm_column_defaults_to_not_indexed():
    """What a camera created through POST /cameras gets. The ORM default is the
    one that fires — SQLAlchemy supplies it on insert, so the server default is
    never consulted on this path."""
    default = Camera.__table__.c.search_indexing.default
    assert default is not None and default.arg is False


def test_the_domains_default_survives_the_switch_being_off():
    """`search_domains` stays person+vehicles, and that is not an oversight.

    It means nothing while `search_indexing` is false, and keeping it is what
    makes switching indexing on a single click that WORKS. Defaulting it empty
    too would give an operator a switch that visibly does nothing until they
    find a second control."""
    domains = Camera.__table__.c.search_domains.default
    assert domains is not None and domains.arg(None) == ["person", "vehicles"]


def test_a_payload_that_omits_the_flag_reads_as_not_indexed():
    """The response model's default is what an older or partial payload means.
    True here would make the SPA draw AI as running on a camera nobody switched
    on — the same mistake in the other direction as the pre-038 SPA."""
    assert CameraResponse.model_fields["search_indexing"].default is False


def test_a_freshly_added_camera_costs_no_decode_and_no_detector():
    """The two rules that spend money. A new camera is in neither desired set,
    which is the whole point of the default: it records, and nothing else."""
    row_frames = ("gate-a1b2", True, False, False, ["person", "vehicles"], {})
    desired, absent = frames_client.desired_sets([row_frames])
    assert desired == set() and absent == {"gate-a1b2"}

    row_analytics = ("gate-a1b2", True, False, ["person", "vehicles"], {})
    desired, absent = analytics_client.desired_sets([row_analytics])
    assert desired == {} and absent == {"gate-a1b2"}


def test_switching_indexing_on_is_enough_to_start_both():
    """One switch, nothing else. The stored domains are already there, so the
    camera goes straight into both desired sets."""
    desired, _ = frames_client.desired_sets(
        [("gate-a1b2", True, False, True, ["person", "vehicles"], {})])
    assert desired == {"gate-a1b2"}

    desired, _ = analytics_client.desired_sets(
        [("gate-a1b2", True, True, ["person", "vehicles"], {})])
    assert list(desired) == ["gate-a1b2"]
    assert desired["gate-a1b2"].domains == ["person", "vehicles"]
