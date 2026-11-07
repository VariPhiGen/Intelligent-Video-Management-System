"""peripherals: two more categories, for what Home Assistant brings in

029 fixed the category to three values because the SPA keys icons and actions
off it. Importing from Home Assistant brings device kinds none of the three
fits, so two are added:

  Automation           an HA scene, script or button — "run this", not a device
                       with a state of its own
  Climate · Utilities  fans and valves

The existing three keep their meaning. The constraint is still a closed list —
the SPA must know every value — just a longer one.

Revision ID: 082
Revises: 081
"""
from typing import Union

from alembic import op

revision: str = "082"
down_revision: Union[str, None] = "081"
branch_labels = None
depends_on = None

_OLD = "category IN ('Lighting', 'Access control', 'Audio · Sensors')"
_NEW = ("category IN ('Lighting', 'Access control', 'Audio · Sensors', "
        "'Automation', 'Climate · Utilities')")


def upgrade() -> None:
    op.drop_constraint("ck_peripherals_category", "peripherals", type_="check")
    op.create_check_constraint("ck_peripherals_category", "peripherals", _NEW)


def downgrade() -> None:
    # Rows using the new values would violate the old constraint; they go back
    # to the nearest old category rather than blocking the downgrade.
    op.execute("UPDATE peripherals SET category = 'Access control' "
               "WHERE category IN ('Automation', 'Climate · Utilities')")
    op.drop_constraint("ck_peripherals_category", "peripherals", type_="check")
    op.create_check_constraint("ck_peripherals_category", "peripherals", _OLD)
