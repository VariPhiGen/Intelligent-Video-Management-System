-- seed_demo_peripherals.sql — TEMPORARY demo inventory for the Peripherals page
-- and the Map tab's HA Devices layer.
--
-- Peripheral control has no backend yet (no Home-Assistant bridge, no rule
-- engine), so a fresh appliance shows an empty device wall and an empty HA
-- layer. This fills both FROM ONE SOURCE: rows in `peripherals`, and pins in
-- `sitemap_ha_placements` that reference them by foreign key. The Peripherals
-- page and the Map are reading the same rows — edit a device on one and the
-- other follows, delete it and its pin cascades away. There is no second copy
-- of this data anywhere.
--
-- What this is NOT: it does not fake device state. `last_state` stays NULL, so
-- every tile reads UNKNOWN and the wall keeps saying the bridge is offline —
-- which is true. See the opt-in block at the bottom if you need states filled
-- for a screenshot, and read the warning attached to it.
--
--   Seed (all plans get their own copy of the fleet):
--     docker exec -i vms_postgres psql -U rtsp -d rtsp_relay < scripts/seed_demo_peripherals.sql
--
--   Seed onto one plan only:
--     docker exec -i vms_postgres psql -U rtsp -d rtsp_relay \
--       -v plan="'floor'" < scripts/seed_demo_peripherals.sql
--
--   Remove everything this seeded (pins cascade with the devices):
--     docker exec vms_postgres psql -U rtsp -d rtsp_relay \
--       -c "DELETE FROM peripherals WHERE created_by = 'seed:demo';"
--
-- Every row is stamped `created_by = 'seed:demo'`, which is what makes that
-- cleanup exact — it can never take a device an operator added by hand. Re-run
-- this file as often as you like: names are unique (ix_peripherals_name_lower)
-- and both inserts are ON CONFLICT DO NOTHING, so a second run is a no-op
-- rather than a duplicate fleet.

\set ON_ERROR_STOP on

-- Default: every sitemap. Override with -v plan="'floor'".
-- The default is an EMPTY STRING, not NULL: `:'plan'` interpolates as a quoted
-- literal, so an unset variable would arrive as the four characters 'NULL' and
-- match no sitemap (and silently place nothing).
\if :{?plan}
\else
  \set plan ''
\endif

BEGIN;

-- ── Devices ──────────────────────────────────────────────────────────────────
-- A plausible mixed fleet across the three categories the UI groups by. The
-- `external_id` column holds the address a bridge would bind to: Home-Assistant
-- entity ids here, since that is the integration the page is written against.
-- Nothing reads them yet — they are recorded so the integration has something
-- to match on when it lands.
INSERT INTO peripherals
  (slug, name, category, location, vendor, external_id, notes, enabled,
   created_at, updated_at, created_by)
VALUES
  ('demo-floodlight-perimeter-n', 'Floodlight — Perimeter North', 'Lighting',
   'Perimeter North', 'Havells', 'light.floodlight_perimeter_north',
   'Demo seed — replace with the real fixture when the bridge lands.', true,
   now(), now(), 'seed:demo'),
  ('demo-atrium-lights', 'Atrium lights', 'Lighting',
   'Civic Centre atrium', 'Philips', 'light.atrium',
   'Demo seed.', true, now(), now(), 'seed:demo'),
  ('demo-strobe-zone-b', 'Strobe — Zone B', 'Lighting',
   'Zone B emergency', 'Agni', 'light.strobe_zone_b',
   'Demo seed.', true, now(), now(), 'seed:demo'),
  ('demo-parking-lights', 'Parking lights', 'Lighting',
   'Zone C parking', 'Havells', 'switch.parking_lights',
   'Demo seed.', true, now(), now(), 'seed:demo'),

  ('demo-gate3-maglock', 'Gate 3 — Maglock', 'Access control',
   'Main entry', 'Godrej', 'lock.gate_3_maglock',
   'Demo seed.', true, now(), now(), 'seed:demo'),
  ('demo-barrier-arm', 'Barrier arm', 'Access control',
   'Parking entry', 'Boom Barrier Co', 'cover.barrier_arm',
   'Demo seed.', true, now(), now(), 'seed:demo'),
  ('demo-turnstile-a', 'Turnstile A', 'Access control',
   'Gate 1', 'Godrej', 'lock.turnstile_a',
   'Demo seed.', true, now(), now(), 'seed:demo'),
  ('demo-strike-server-room', 'Strike — Server room', 'Access control',
   'Server room', 'Godrej', 'lock.strike_server_room',
   'Demo seed.', true, now(), now(), 'seed:demo'),

  ('demo-siren-zone-b', 'Siren — Zone B', 'Audio · Sensors',
   'Perimeter', 'Agni', 'siren.zone_b',
   'Demo seed.', true, now(), now(), 'seed:demo'),
  ('demo-pa-atrium', 'PA — Atrium', 'Audio · Sensors',
   'Atrium IP speaker', 'Bosch', 'media_player.pa_atrium',
   'Demo seed.', true, now(), now(), 'seed:demo'),
  ('demo-door-contact-server', 'Door contact — Server room', 'Audio · Sensors',
   'Server room', 'Honeywell', 'binary_sensor.door_contact_server_room',
   'Demo seed.', true, now(), now(), 'seed:demo'),
  ('demo-temp-sensor-powerhouse', 'Temp sensor — Powerhouse', 'Audio · Sensors',
   'Powerhouse', 'Honeywell', 'sensor.powerhouse_temperature',
   'Demo seed.', true, now(), now(), 'seed:demo')
ON CONFLICT DO NOTHING;

-- ── Pins ─────────────────────────────────────────────────────────────────────
-- Laid out on a 4×3 grid, columns stopping at 0.78 rather than the edge: the
-- name label renders to the RIGHT of its pin, so a device parked at 0.86 has
-- its label clipped by the plan edge. Rows avoid the zoom controls, and the
-- three categories read as bands. The
-- coordinates are normalized 0–1, the same convention camera placement uses —
-- they land in the same relative spot whatever the plan's aspect ratio.
--
-- Every device is placed on every matching plan: placement is per-map, so a
-- device can legitimately sit on two floors, and a demo wants both plans
-- populated. Drag any pin in the Map tab afterwards and the drop persists —
-- these are ordinary rows, not a fixture the UI treats specially.
WITH target AS (
  SELECT id FROM sitemaps
  WHERE :'plan' = '' OR name = :'plan'
),
grid(slug, x, y) AS (
  VALUES
    ('demo-floodlight-perimeter-n',   0.12, 0.18),
    ('demo-atrium-lights',            0.34, 0.18),
    ('demo-strobe-zone-b',            0.56, 0.18),
    ('demo-parking-lights',           0.78, 0.18),

    ('demo-gate3-maglock',            0.12, 0.50),
    ('demo-barrier-arm',              0.34, 0.50),
    ('demo-turnstile-a',              0.56, 0.50),
    ('demo-strike-server-room',       0.78, 0.50),

    ('demo-siren-zone-b',             0.12, 0.82),
    ('demo-pa-atrium',                0.34, 0.82),
    ('demo-door-contact-server',      0.56, 0.82),
    ('demo-temp-sensor-powerhouse',   0.78, 0.82)
)
INSERT INTO sitemap_ha_placements (sitemap_id, peripheral_id, x, y)
SELECT t.id, p.id, g.x, g.y
FROM target t
CROSS JOIN grid g
JOIN peripherals p ON p.slug = g.slug
ON CONFLICT (sitemap_id, peripheral_id) DO NOTHING;

COMMIT;

SELECT
  (SELECT count(*) FROM peripherals WHERE created_by = 'seed:demo') AS demo_devices,
  (SELECT count(*) FROM sitemap_ha_placements h
     JOIN peripherals p ON p.id = h.peripheral_id
    WHERE p.created_by = 'seed:demo')                               AS demo_pins;

-- ─────────────────────────────────────────────────────────────────────────────
-- OPTIONAL — fabricated device states. Read this before uncommenting.
--
-- `last_state` is the column a Home-Assistant bridge writes. Filling it by hand
-- makes every tile show a confident ON / LOCKED / 28°C, in colour, beside live
-- camera data — with nothing having reported it and no timestamp to age it out.
-- That is the exact thing the hardcoded device array used to do, and the reason
-- the wall now reads UNKNOWN. If you need it for a screenshot, run this and then
-- run the cleanup above before anyone treats the page as operational.
--
-- UPDATE peripherals SET last_state = CASE category
--     WHEN 'Lighting'        THEN 'ON'
--     WHEN 'Access control'  THEN 'LOCKED'
--     ELSE 'READY'
--   END,
--   last_seen = now()
--  WHERE created_by = 'seed:demo';
--
-- Clear them again (back to honest) without removing the inventory:
-- UPDATE peripherals SET last_state = NULL, last_seen = NULL
--  WHERE created_by = 'seed:demo';
