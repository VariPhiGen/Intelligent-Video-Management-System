-- 001_init.sql — the Smart Search index: person and vehicle crops.
--
-- This database is SEPARATE from the camera registry on purpose.
--
--   * The registry lives in postgres:15-alpine (musl). pgvector's official
--     images are Debian (glibc). Adding pgvector to that volume by swapping
--     the image would change collation under an existing data directory and
--     silently invalidate every text btree index in it — including the ones on
--     the tamper-evident audit log. A NEW database on a NEW volume has no such
--     history, so the trap simply does not apply here.
--   * Smart Search is optional. A deployment that does not want it does not
--     start this container, and no recording path depends on it.
--   * It is a continuous, index-heavy write workload. It does not belong next
--     to the data that must survive.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ── Embedding dimension ──────────────────────────────────────────────────────
-- 512 = OpenCLIP ViT-B/32, which phase 0 validated for deduplication
-- (purity 0.983, ARI 0.946 against a 15 FPS tracker reference).
--
-- NOT the 1152 of ViT-SO400M-14-SigLIP-384 that the retiring clip-service uses.
-- Tier 1 has to run on CPU-only appliances, and SigLIP-SO400M does not; the
-- smaller model is the one that lets the free tier ship at all. It also
-- quarters the storage: 2,048 bytes per vector against 4,608.
--
-- Changing this is not a migration and must not be written as one. Vectors from
-- a different model are not comparable, so a dimension change is a full
-- re-index from footage rather than a schema change.

CREATE TABLE search_persons (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    embedding     vector(512)  NOT NULL,
    -- The VMS camera slug. Named sensor_id because that is what the wire
    -- contract calls it for this domain, and matching the contract in the
    -- column name is what stops the two drifting.
    sensor_id     text         NOT NULL,
    ts            timestamptz  NOT NULL,
    tracker_id    text,
    confidence    real,
    frame_number  bigint,
    pad_index     integer,
    -- Normalised 0-1 [x1,y1,x2,y2], the same convention the zone config uses.
    bbox          real[],
    -- Crops live on disk. A JPEG in a row is a row nobody can vacuum.
    crop_path     text         NOT NULL,
    -- Set at INSERT from the camera's own retention, not by a global sweep.
    -- This is the one schema decision that is expensive to defer: per-camera
    -- retention and a single global window need different columns, and adding
    -- this later means rewriting every row. Populated from a global default
    -- until per-camera retention is wired up; the column does not change.
    expires_at    timestamptz  NOT NULL,
    CONSTRAINT search_persons_bbox_len CHECK (bbox IS NULL OR array_length(bbox, 1) = 4)
);

CREATE TABLE search_vehicles (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    embedding     vector(512)  NOT NULL,
    -- camera_id, not sensor_id: the vehicles domain names it differently on the
    -- wire (/vehicles/stats takes camera_ids). Kept faithful deliberately.
    camera_id     text         NOT NULL,
    ts            timestamptz  NOT NULL,
    plate         text,
    vehicle_type  text,
    color         text,
    confidence    real,
    bbox          real[],
    crop_path     text         NOT NULL,
    expires_at    timestamptz  NOT NULL,
    CONSTRAINT search_vehicles_bbox_len CHECK (bbox IS NULL OR array_length(bbox, 1) = 4)
);

-- ── Vector search ────────────────────────────────────────────────────────────
-- HNSW with cosine distance, because the query path encodes text and compares
-- it to image vectors by cosine — the same operator the retiring service uses.
-- Defaults (m=16, ef_construction=64) are deliberate: they are what pgvector
-- recommends before there is a measured corpus to tune against, and phase 0
-- established that this appliance cannot supply one.
CREATE INDEX search_persons_embedding_hnsw
    ON search_persons USING hnsw (embedding vector_cosine_ops);
CREATE INDEX search_vehicles_embedding_hnsw
    ON search_vehicles USING hnsw (embedding vector_cosine_ops);

-- ── Scoping ──────────────────────────────────────────────────────────────────
-- Every query is scoped to a camera set and usually a time window; the API
-- refuses to answer unscoped at all. This index is therefore on the hot path of
-- every single request, not an optimisation.
CREATE INDEX search_persons_scope  ON search_persons  (sensor_id, ts DESC);
CREATE INDEX search_vehicles_scope ON search_vehicles (camera_id, ts DESC);

-- ── Plate lookup is text, not vectors ────────────────────────────────────────
-- A plate is an exact string, and trigram matching handles the OCR near-misses
-- ("0" for "O") that a vector search cannot express at all. This is strictly
-- better than the similarity search it replaces.
CREATE INDEX search_vehicles_plate_trgm
    ON search_vehicles USING gin (plate gin_trgm_ops)
    WHERE plate IS NOT NULL;

-- ── Retention sweep ──────────────────────────────────────────────────────────
-- A plain btree, swept by scripts/retention.py. Deliberately NOT partitioned:
-- monthly partitions would turn expiry into a DROP instead of a DELETE, but
-- they also fragment the HNSW index across partitions and complicate every
-- search. Phase 0 measured 11 of 12 sample windows containing no people at all,
-- so the row counts that would justify that complexity are not in evidence.
-- Revisit when a real site sustains more than ~5M live rows.
CREATE INDEX search_persons_expiry  ON search_persons  (expires_at);
CREATE INDEX search_vehicles_expiry ON search_vehicles (expires_at);
