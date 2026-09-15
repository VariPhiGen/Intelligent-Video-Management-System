-- 006 — record WHICH model produced each face vector.
--
-- THE PROBLEM THIS SOLVES BEFORE IT HAPPENS. A face vector is only comparable
-- with vectors from the same model. 001 says as much about CLIP and stops
-- there, which leaves the older domains with a real hazard: swap the encoder
-- and the index becomes a silent mixture, queried by whichever encoder is
-- current, with every pre-swap row ranking against a vector it shares no space
-- with. Nothing in the data says which rows are which.
--
-- Faces are the domain where that swap is LIKELY rather than hypothetical.
-- Measured on this appliance 2026-09-14, SFace separates same-person from
-- different-person pairs at AUC 0.881 with rank-1 32% on real crops — good
-- enough to ship, weak enough that an ArcFace-class model is a live option once
-- a site has been calibrated. So the column goes in now, while the table holds
-- almost nothing, rather than after the first swap has already mixed it.
--
-- WHAT IT BUYS: index/queries.py scopes every face query to the ACTIVE model,
-- so a half-finished re-index returns FEWER results, never wrong ones, and
-- `select embedding_model, count(*)` says exactly how far a re-index has got.
--
-- NULLABLE, and deliberately not backfilled to a guess. Rows written before
-- this migration came from sface-2021dec — that is knowable from the deployment
-- history but not from the data, and writing a value the database did not
-- observe would make the column lie in the one case it exists to catch. They
-- are instead adopted explicitly below, because on this appliance the history
-- IS known: one model has ever run here.

ALTER TABLE search_faces ADD COLUMN IF NOT EXISTS embedding_model text;

-- The rows that predate the column. Safe here and nowhere else: this index has
-- only ever run sface-2021dec. A deployment that cannot say that should leave
-- them NULL and let them age out of the retention window instead.
UPDATE search_faces SET embedding_model = 'sface-2021dec' WHERE embedding_model IS NULL;

-- Scoped queries already filter on (camera_id, ts); the model predicate rides
-- along on a table whose whole point is a vector scan, so it needs no index of
-- its own. It is here for the count, not for the plan.
COMMENT ON COLUMN search_faces.embedding_model IS
  'Model that produced this embedding. Queries are scoped to the active model; '
  'see index/faces.py. Changing models is a re-index, not a migration.';
