-- 005_faces.sql — the faces domain: search a face by photograph.
--
-- A THIRD TABLE, NOT A COLUMN. Plates were an attribute of a vehicle row and
-- needed no table; a face is its own observation — one person crop can yield a
-- face or none, and the row has to be findable, retainable and erasable on its
-- own. Anything that treats faces as "person rows with a flag" cannot express
-- a face crop whose person crop was deduplicated away.
--
-- 128 DIMENSIONS, NOT 512, and it is a different model entirely. Persons and
-- vehicles are OpenCLIP ViT-B/32 over the whole crop; faces are SFace
-- (OpenCV zoo, face_recognition_sface_2021dec) over a 112x112 aligned face,
-- detected by YuNet. The two spaces are not comparable and must never be
-- searched against each other, which is the main reason this is a separate
-- table rather than a `kind` column on search_persons: a shared column would
-- make that mistake a runtime bug instead of a schema impossibility.
--
-- THE VECTOR IS WRITTEN BY THE PRODUCER, not by this service. Measured
-- 2026-09-14 over the live corpus: same-person pairs score median 0.387 cosine
-- and different-person pairs 0.117, so the identity signal is a ~0.27 band —
-- while re-encoding the SAME face at JPEG q60 moves its embedding by up to
-- 0.46. Embedding from the stored quality-82 crop would therefore spend most
-- of a thin margin on compression, so analytics embeds off the raw frame and
-- sends the vector with the observation.
--
-- WHAT THIS DOMAIN IS FOR, AND WHAT IT IS NOT. Search-by-photograph: "find
-- other appearances of this face". It is NOT identification — there is no
-- watchlist, no name, and nothing here associates a face with a person's
-- identity. That boundary is also why the domain is off by default per camera:
-- the same survey found a usable face on ~4% of person passes site-wide and
-- 0.2% on one camera, so switching it on everywhere would collect biometric
-- data almost none of which can answer a query.

CREATE TABLE IF NOT EXISTS search_faces (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    embedding     vector(128)  NOT NULL,
    -- camera_id, matching search_vehicles. Persons say sensor_id only because
    -- the wire contract of the retired service did; a new domain has no such
    -- history to honour.
    camera_id     text         NOT NULL,
    ts            timestamptz  NOT NULL,
    -- The face detector's own score, NOT the person detector's. One column,
    -- because for a face row there is only one number worth keeping: the
    -- person's confidence belongs to the person row that was written beside it.
    confidence    real,
    -- Face width in SOURCE pixels. The quality floor lives on this: retrieval
    -- rank-1 measured 24% below 40px against 40% at 64-111px, so a query that
    -- wants to exclude unusable faces needs the number, and a column is the
    -- only place it survives the crop being resized for storage.
    face_width_px integer,
    -- Normalised 0-1 [x1,y1,x2,y2] of the FACE within the source frame — the
    -- same convention every other domain uses, so a bbox means one thing.
    bbox          real[],
    crop_path     text         NOT NULL,
    -- The person track the face was cut from, when there was one. Lets "every
    -- appearance of this person" be asked of both domains at once.
    tracker_id    text,
    expires_at    timestamptz  NOT NULL,
    CONSTRAINT search_faces_bbox_len CHECK (bbox IS NULL OR array_length(bbox, 1) = 4)
);

-- Same index set as the other two domains, and for the same three reasons:
-- vector search, scoped reads (every query names its cameras), and expiry.
CREATE INDEX IF NOT EXISTS search_faces_embedding_hnsw
    ON search_faces USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS search_faces_scope  ON search_faces (camera_id, ts DESC);
CREATE INDEX IF NOT EXISTS search_faces_expiry ON search_faces (expires_at);

-- NOTE on 002: `hnsw.iterative_scan` is set at DATABASE level, so this table
-- inherits it. That is deliberate and worth not undoing — without it a scoped
-- search post-filters HNSW candidates and can return zero rows while reporting
-- success, which is indistinguishable from "nothing was ever recorded".
