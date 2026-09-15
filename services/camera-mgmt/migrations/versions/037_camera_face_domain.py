"""Allow `face` in a camera's search_domains.

Revision ID: 037
Revises: 036
Create Date: 2026-09-14

Migration 031 gave each camera a set of domains and a CHECK constraint holding
it to person / vehicles / plate. The face domain is a fourth, and the constraint
is what caught the attempt to enable it — which is the constraint doing its job,
and the reason it is widened here rather than dropped.

NOT ADDED TO ANY DEFAULT, and that is the point of the change rather than an
omission. 031 defaults every camera to person+vehicles and this leaves that
exactly as it was. Measured on this appliance 2026-09-14 over 61,762 stored
person crops: a face usable for search (>=40px wide, detector score >=0.85)
appears on about 4% of the people who walk past, 17% on a close indoor camera
and 0.2% on a distant one. A domain that yields almost nothing on most cameras
must be switched on deliberately, per camera, by someone who has looked at what
that camera sees.

There is a second reason, and it outranks the first. A face crop is biometric
personal data. Collecting it site-wide by default, on cameras where it cannot
answer a query, is the kind of collection data-protection law asks an operator
to justify — so the default has to be off and the operator has to choose.

RENUMBERED 035 → 037 on 2026-09-15. It was written as 035 in parallel with
035_events_fleet_time_index and 036_activity_registry_catalog, and two revisions
both called 035 with down_revision 034 fork the chain. A database that already
ran this as 035 — the appliance did, on 2026-09-14 — records "035" and would
skip the other 035. Stamp it back first:

    alembic stamp 034 && alembic upgrade head

which runs the fleet-time index (created IF NOT EXISTS), 036, and then this.
Running this twice is harmless: it drops and re-creates the same constraint.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "037"
down_revision: Union[str, None] = "036"
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None

#: The full set after this migration. Spelled out rather than appended to, so
#: the constraint's definition is readable in one place — and so a future
#: migration that forgets one has to say which.
DOMAINS = ("person", "vehicles", "plate", "face")
OLD_DOMAINS = ("person", "vehicles", "plate")


def _array(domains: Sequence[str]) -> str:
    return "ARRAY[" + ", ".join(f"'{d}'::text" for d in domains) + "]"


def upgrade() -> None:
    op.drop_constraint("cameras_search_domains_valid", "cameras", type_="check")
    op.create_check_constraint(
        "cameras_search_domains_valid", "cameras",
        f"search_domains <@ {_array(DOMAINS)}",
    )


def downgrade() -> None:
    # Rows first: a camera left with `face` selected would make the narrower
    # constraint unaddable, and failing halfway through a downgrade is worse
    # than dropping a setting the downgraded code cannot honour anyway.
    op.execute(
        "UPDATE cameras SET search_domains = array_remove(search_domains, 'face') "
        "WHERE 'face' = ANY(search_domains)"
    )
    op.drop_constraint("cameras_search_domains_valid", "cameras", type_="check")
    op.create_check_constraint(
        "cameras_search_domains_valid", "cameras",
        f"search_domains <@ {_array(OLD_DOMAINS)}",
    )
