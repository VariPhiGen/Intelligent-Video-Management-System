"""The faces domain in the index: the registry, the write, and the query.

THE REGISTRY TESTS ARE THE POINT. Adding a third domain to this service meant
touching expire_once, erase_range, sweep_orphans, write_batch, health, the crop
route and the stats route — the exact shape of the defect the NVR's sub-track
audit found eleven times, where a new per-camera thing arrives and four of five
surfaces learn about it. So the assertions below are about ENUMERATION: every
sweep must reach every registered domain, stated in a way that a fourth domain
would break rather than quietly skip.

The erasure one is not tidiness. An erasure request is closed as fulfilled on
the strength of this call; a table it never visits makes that a false claim
rather than a partial one.

Run: python3 -m pytest tests/test_faces_domain.py -q
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from index.domains import ALL_TABLES, DOMAINS, ERASE_KEYS, FACE, spec  # noqa: E402
from index.store import Store  # noqa: E402


# ── a pool that records the SQL it is given ────────────────────────────────

class FakeCursor:
    def __init__(self, pool, returns=None):
        self._pool = pool
        self._returns = returns or []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._pool.sql.append(sql)
        self._rows = self._pool.answer(sql)
        self.rowcount = len(self._rows)

    def executemany(self, sql, params):
        self._pool.sql.append(sql)
        self._pool.params.extend(params)

    def fetchall(self):
        return list(getattr(self, "_rows", []))

    def fetchone(self):
        rows = getattr(self, "_rows", [])
        return rows[0] if rows else None

    def __iter__(self):
        return iter(getattr(self, "_rows", []))


class FakeConn:
    def __init__(self, pool):
        self._pool = pool

    def cursor(self, *a, **kw):
        return FakeCursor(self._pool)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakePool:
    """Answers every DELETE ... RETURNING once, then empties — so the store's
    drain loops terminate the way they do against a real database."""

    def __init__(self):
        self.sql: list[str] = []
        self.params: list = []
        self._drained: set[str] = set()

    def answer(self, sql: str):
        if "RETURNING" in sql and "DELETE" in sql:
            key = sql
            if key in self._drained:
                return []
            self._drained.add(key)
            # As wide as the statement, the way Postgres answers it: since whole
            # frames landed every table returns `crop_path, frame_path` — or
            # `crop_path, NULL` where the domain keeps no frame.
            ncols = sql.split("RETURNING", 1)[1].count(",") + 1
            return [("/crops/a.jpg",) + (None,) * (ncols - 1)]
        return []

    def connection(self):
        return FakeConn(self)


def store_with(pool):
    s = Store("postgresql://unused/unused")
    s._get_pool = lambda: pool          # noqa: SLF001 — the seam under test
    return s


# ── the registry reaches every sweep ───────────────────────────────────────

def test_the_faces_domain_is_registered_with_its_own_table_and_dimension():
    d = spec(FACE)
    assert d.table == "search_faces"
    assert d.dims == 128, "SFace is 128-dim; CLIP's 512 would be a different space"
    assert d.supplies_own_vector, \
        "faces must arrive embedded — see index/domains.py for the measurement"


@pytest.mark.parametrize("table", ALL_TABLES)
def test_expiry_sweeps_every_registered_table(table):
    pool = FakePool()
    store_with(pool).expire_once()
    assert any(table in sql for sql in pool.sql), f"{table} is never expired"


@pytest.mark.parametrize("table", ALL_TABLES)
def test_erasure_reaches_every_registered_table(table):
    """An erasure that skips a table reports success with the data
    subject still searchable."""
    pool = FakePool()
    store_with(pool).erase_range(
        "cam-a", datetime(2026, 9, 1, tzinfo=timezone.utc),
        datetime(2026, 9, 2, tzinfo=timezone.utc))
    assert any(table in sql for sql in pool.sql), f"{table} survives an erasure"


def test_erasure_reports_a_count_for_every_domain():
    """Counts are summed by camera-mgmt into the erasure record. A domain that is
    erased but not counted under-reports the erasure — and a missing key reads
    as zero, silently."""
    pool = FakePool()
    out = store_with(pool).erase_range(
        "cam-a", datetime(2026, 9, 1, tzinfo=timezone.utc),
        datetime(2026, 9, 2, tzinfo=timezone.utc))
    for key in ERASE_KEYS:
        assert key in out, f"{key} missing from the erasure result"
    assert "faces_deleted" in out


@pytest.mark.parametrize("table", ALL_TABLES)
def test_the_orphan_sweep_asks_every_table_what_it_references(table, tmp_path):
    pool = FakePool()
    store_with(pool).sweep_orphans(str(tmp_path))
    assert any(table in sql for sql in pool.sql), \
        f"crops referenced by {table} would be deleted as orphans"


def test_no_statement_asks_the_face_table_for_a_frame(tmp_path):
    """search_faces has no frame_path column; only person and vehicle rows keep
    a frame. When whole-frame results (migrations 005_frame_path and
    006_frame_boxes) met the face domain in one tree, expiry asked EVERY table
    for frame_path — which fails the whole sweep on its first run, and that
    sweep is the only thing keeping the index bounded. Each statement has to
    ask the registry first, and the frame tables must keep their frames."""
    pool = FakePool()
    s = store_with(pool)
    s.expire_once()
    s.erase_range("cam-a", datetime(2026, 9, 1, tzinfo=timezone.utc),
                  datetime(2026, 9, 2, tzinfo=timezone.utc))
    s.sweep_orphans(str(tmp_path))
    face_sql = [q for q in pool.sql if "search_faces" in q]
    assert len(face_sql) >= 3, "expiry, erasure and the sweep must all reach faces"
    assert not any("frame_" in q for q in face_sql), face_sql
    for table in (d.table for d in DOMAINS.values() if d.frames):
        assert any(table in q and "frame_path" in q for q in pool.sql), \
            f"{table} no longer has its frames expired, erased or referenced"


# ── the write ──────────────────────────────────────────────────────────────

def test_a_face_row_is_written_to_its_own_table_with_its_own_columns():
    pool = FakePool()
    n = store_with(pool).write_batch(FACE, [{
        "embedding": "[0.1, 0.2]", "camera": "cam-a",
        "ts": datetime(2026, 9, 14, tzinfo=timezone.utc),
        "confidence": 0.91, "bbox": [0.1, 0.2, 0.3, 0.4],
        "crop_path": "/crops/face.jpg", "face_width_px": 73,
        "tracker_id": "cam-a:abc:1", "embedding_model": "sface-2021dec",
    }])
    assert n == 1
    sql = pool.sql[-1]
    assert "INSERT INTO search_faces" in sql
    assert "camera_id" in sql

    # THE EXTRAS LINE UP WITH THEIR COLUMNS, asserted by NAME rather than by
    # index. A positional assertion passes a shifted column order as long as the
    # count matches — and a shifted order writes the face width into the tracker
    # column with nothing raising, which is migration 003's defect exactly.
    extras = spec(FACE).extras
    values = pool.params[0][-len(extras):]
    assert dict(zip(extras, values)) == {
        "face_width_px": 73,
        "tracker_id": "cam-a:abc:1",
        "embedding_model": "sface-2021dec",
    }
    for name in extras:
        assert name in sql, f"{name} is not in the INSERT"


def test_an_unknown_domain_is_dropped_loudly_rather_than_written_somewhere():
    pool = FakePool()
    s = store_with(pool)
    assert s.write_batch("wombats", [{"embedding": "[]", "camera": "c",
                                      "ts": datetime.now(timezone.utc),
                                      "crop_path": "/x.jpg"}]) == 0
    assert pool.sql == []
    assert s.write_failures == 1


# ── ingest: the vector arrives with the observation ────────────────────────

class StubEmbedder:
    def __init__(self):
        self.calls = 0

    def embed_images(self, crops):
        self.calls += 1
        return [np.ones(512, dtype="float32") for _ in crops]


class StubWriter:
    def __init__(self):
        self.frames = []

    def save(self, domain, slug, crop, ts):
        return f"/crops/{domain}.jpg"

    def save_frame(self, slug, ts, jpeg):
        self.frames.append((slug, ts))
        return f"/crops/frames/{slug}/{int(ts * 1000)}.jpg"


class StubStore:
    def __init__(self):
        self.batches = []

    def write_batch(self, domain, rows):
        self.batches.append((domain, rows))
        return len(rows)


def make_pipeline():
    from index.config import AppConfig
    from index.pipeline import IngestPipeline
    cfg = AppConfig.from_yaml("config.yaml")
    emb, store = StubEmbedder(), StubStore()
    p = IngestPipeline(cfg, emb, store, StubWriter())
    p.set_domains("cam-a", ("person", "face"))
    return p, emb, store


def observation(domain, embedding=None, **kw):
    from index.pipeline import ObservationInput
    from PIL import Image
    return ObservationInput(
        slug="cam-a", crop=Image.new("RGB", (112, 112)), ts=1757404800.0,
        domain=domain, confidence=0.9, label=domain, plate=None,
        plate_confidence=None, bbox=(0.1, 0.2, 0.3, 0.4), tracker_id="t1",
        embedding=embedding, **kw)


def test_a_face_is_stored_with_the_vector_it_arrived_with_and_clip_is_not_called():
    p, emb, store = make_pipeline()
    vec = [0.5] * 128
    assert p.ingest_observations([observation("face", embedding=vec,
                                              face_width_px=80)]) == 1
    assert emb.calls == 0, "CLIP ran on a face — that is a 512-dim vector for a 128-dim column"
    domain, rows = store.batches[-1]
    assert domain == "face"
    assert rows[0]["face_width_px"] == 80


def test_a_person_in_the_same_batch_is_still_embedded_here():
    """Mixed batches are the normal case — analytics posts a person and the
    face cut from it moments apart. Splitting on the registry must not stop the
    CLIP domains being embedded."""
    p, emb, store = make_pipeline()
    n = p.ingest_observations([
        observation("person"),
        observation("face", embedding=[0.1] * 128, face_width_px=64),
    ])
    assert n == 2
    assert emb.calls == 1
    assert {d for d, _ in store.batches} == {"person", "face"}


def test_a_face_observation_never_writes_a_frame_but_its_person_does():
    """Analytics sends the whole frame beside every observation. A face row has
    no frame_path column, so a frame written for it is a picture of a person
    that no row references — the sweep deletes it an hour later, having stored
    biometric imagery for nothing. The person row of the same moment keeps it."""
    p, emb, store = make_pipeline()
    writer = p._writer                                   # noqa: SLF001
    p.ingest_observations([
        observation("person", frame_jpeg=b"\xff\xd8 person frame"),
        observation("face", embedding=[0.1] * 128, face_width_px=64,
                    frame_jpeg=b"\xff\xd8 same frame"),
    ])
    rows = {d: r[0] for d, r in store.batches}
    assert rows["person"]["frame_path"], "the person row lost its frame"
    assert not rows["face"].get("frame_path"), "a face row was given a frame"
    assert len(writer.frames) == 1, "a frame was written for the face observation"


def test_a_face_from_a_camera_that_did_not_ask_is_refused_by_the_backstop():
    """Analytics filters domains too. This is the backstop, and it is the store
    an operator later trusts."""
    p, _, store = make_pipeline()
    p.set_domains("cam-a", ("person",))
    assert p.ingest_observations([observation("face", embedding=[0.1] * 128)]) == 0
    assert store.batches == []


# ── the query: the vector has to survive the trip into SQL ─────────────────

class CapturingStore:
    """Records what the query layer hands the database."""

    def __init__(self):
        self.calls = []

    def fetch(self, sql, params=None):
        self.calls.append((sql, params or {}))
        return []


def test_the_query_vector_is_a_pgvector_literal_not_a_numpy_repr():
    """FOUND IN A LIVE RUN, not by the stubs above.

    `str(list(arr.astype(float)))` and `str(arr.tolist())` both look like a list
    of floats in Python. Under NumPy 2 only the second one IS: the first keeps
    numpy scalars, so the string reads "[np.float64(0.0088), ...]" and pgvector
    answers `invalid input syntax for type vector` — a 500 on every face search,
    with every unit test green because none of them had a real database.

    So the assertion is on the literal itself, which is the thing that was
    wrong, rather than on the search returning rows.
    """
    from index.queries import QueryService
    store = CapturingStore()
    q = QueryService(store, pool=None)
    q.search_faces(vector=np.ones(128, dtype="float32"), top_k=5,
                   score_threshold=0.0, camera_ids=["cam-a"])
    sql, params = store.calls[-1]
    assert "np." not in params["vec"], f"numpy repr leaked into SQL: {params['vec'][:60]}"
    assert params["vec"].startswith("[1.0, 1.0")
    assert "search_faces" in sql


def test_an_empty_camera_list_still_scopes_a_face_search_to_nothing():
    """Every domain draws this line: an empty scope is a real scope that may
    match nothing, never "no filter". An index that read it the other way once
    answered with another deployment's collection."""
    from index.queries import QueryService
    store = CapturingStore()
    QueryService(store, pool=None).search_faces(
        vector=np.ones(128, dtype="float32"), top_k=5, score_threshold=0.0,
        camera_ids=[])
    sql, params = store.calls[-1]
    assert "camera_id = ANY" in sql
    assert params["camera_ids"] == []


# ── registration: the domain has to survive the front door ─────────────────

def test_a_camera_can_be_registered_with_every_domain_the_registry_declares():
    """FOUND BY RUNNING IT, after all the tests above were green.

    `POST /cameras/{name}` filtered the requested domains against a hard-coded
    `{"person", "vehicles", "plate"}` and kept the rest. Registering a face
    camera therefore answered 200 with the face domain quietly removed, the
    ingest backstop then rejected every face observation as "a domain this
    camera does not contribute", and the only visible symptom was an empty
    gallery — on a feature whose honest yield is already low enough to make
    "empty" look like the expected answer.

    Parameterised over the registry so a fifth domain cannot repeat it.
    """
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from api.server import create_app
    from index.domains import CAMERA_DOMAINS

    seen = {}

    class Engine:
        def upsert_camera(self, name, url, domains, retention_days=None):
            seen["domains"] = domains
            entry = SimpleNamespace(
                name=name, domains=domains,
                to_dict=lambda *a, **k: {"name": name, "domains": list(domains)})
            return entry, "created"

        def describe(self, cam):
            return {}

    store = SimpleNamespace(
        set_camera_retention=lambda *a, **k: {"rows_restamped": 0, "error": None})
    app = create_app(engine=Engine(), config=SimpleNamespace(),
                     store=store, queries=SimpleNamespace(),
                     pool=SimpleNamespace(), retention=None)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/cameras/cam-a",
                    params=[("rtsp_url", "rtsp://x/y")]
                           + [("domains", d) for d in CAMERA_DOMAINS])
    assert r.status_code < 400, r.text
    assert set(seen["domains"]) == set(CAMERA_DOMAINS), \
        f"registration dropped {set(CAMERA_DOMAINS) - set(seen['domains'])}"


# ── the model seam: what makes a swap a re-index rather than a corruption ──

def test_every_registered_face_model_matches_the_column_it_writes_to():
    """The registry is the authority on what a domain stores, so an entry whose
    width the schema cannot hold is a bug in the registry — caught here rather
    than at the first INSERT, inside a write path that swallows failures."""
    from index.faces import FACE_DIM, FACE_MODELS
    for name, spec_ in FACE_MODELS.items():
        assert spec_.dim == FACE_DIM, (
            f"{name} is {spec_.dim}-dim but search_faces is vector({FACE_DIM}); "
            f"a new width needs a migration and a re-index, not a registry entry")


def test_a_model_of_the_wrong_width_is_refused_with_the_instruction():
    """models.py refuses a mis-sized CLIP encoder permanently because retrying
    only writes a wrong answer later. Same rule here, and the message has to
    name the remedy — a re-index — because nothing else in the system will."""
    from types import SimpleNamespace
    from index import faces as faces_mod
    wide = faces_mod.FaceModel(name="pretend-arcface", dim=512,
                               weights="/models/x.onnx", representation="onnx",
                               note="not real")
    cfg = SimpleNamespace(model="pretend-arcface", detector_weights="/x",
                          recogniser_weights="/y", score_threshold=0.85,
                          min_width_px=40)
    saved = dict(faces_mod.FACE_MODELS)
    faces_mod.FACE_MODELS["pretend-arcface"] = wide
    try:
        enc = faces_mod.build(cfg)
    finally:
        faces_mod.FACE_MODELS.clear()
        faces_mod.FACE_MODELS.update(saved)
    assert enc.active is False
    assert "512" in enc.error and "re-index" in enc.error


def test_an_unknown_model_name_says_what_is_registered():
    from types import SimpleNamespace
    from index.faces import build
    enc = build(SimpleNamespace(model="wombat-net", detector_weights="/x",
                                recogniser_weights="/y", score_threshold=0.85,
                                min_width_px=40))
    assert enc.active is False
    assert "wombat-net" in enc.error and "sface-2021dec" in enc.error


def test_an_observation_from_another_model_is_refused_at_the_door():
    """The half-upgraded-fleet case. Two 128-dim face models share no vector
    space, so this INSERTs cleanly and ranks meaninglessly — the failure the
    width check cannot see."""
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from api.server import create_app
    import io, json
    from PIL import Image

    class Pipe:
        def ingest_observations(self, items):
            return len(items)

    store = SimpleNamespace(
        set_camera_retention=lambda *a, **k: {"rows_restamped": 0, "error": None})
    pool = SimpleNamespace(configured_face_model="sface-2021dec",
                           faces_available=True, face_encoder=lambda: None)
    app = create_app(engine=SimpleNamespace(pipeline=Pipe()),
                     config=SimpleNamespace(), store=store,
                     queries=SimpleNamespace(), pool=pool, retention=None)
    client = TestClient(app, raise_server_exceptions=False)

    buf = io.BytesIO()
    Image.new("RGB", (112, 112)).save(buf, format="PNG")
    meta = {"camera": "cam-a", "ts": 1.0, "domain": "face",
            "embedding": [0.1] * 128, "face_width_px": 64,
            "embedding_model": "arcface-w600k"}
    r = client.post("/observations", files={
        "meta": (None, json.dumps(meta)),
        "crop": ("c.png", buf.getvalue(), "image/png")})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "arcface-w600k" in detail and "sface-2021dec" in detail


def test_a_face_query_only_reaches_rows_from_the_active_model():
    """A row from another embedder would rank by noise — and rank PLAUSIBLY,
    which is worse than not appearing. Scoping turns a half-finished re-index
    into fewer results instead of wrong ones."""
    from index.queries import QueryService
    store = CapturingStore()
    q = QueryService(store, pool=None)
    q.search_faces(vector=np.ones(128, dtype="float32"), top_k=5,
                   score_threshold=0.0, camera_ids=["cam-a"],
                   model="sface-2021dec")
    sql, params = store.calls[-1]
    assert "embedding_model = %(model)s" in sql
    assert params["model"] == "sface-2021dec"


def test_the_gallery_is_scoped_to_the_same_model_as_the_search():
    """An unscoped gallery after a swap shows rows no query can reach."""
    from index.queries import QueryService
    store = CapturingStore()
    QueryService(store, pool=None).recent_faces(limit=10, model="sface-2021dec")
    sql, params = store.calls[-1]
    assert "embedding_model = %(model)s" in sql
    assert params["model"] == "sface-2021dec"


def test_without_a_configured_model_nothing_is_scoped_away():
    """Browsing what was collected must not require the model files to be
    installed — a deployment that removed them can still see its own history."""
    from index.queries import QueryService
    store = CapturingStore()
    QueryService(store, pool=None).recent_faces(limit=10, model=None)
    sql, _ = store.calls[-1]
    assert "embedding_model" not in sql


# ── consumption: the query-side model must not be pinned for the process ───

def test_the_face_encoder_is_released_when_nothing_is_searching(tmp_path, monkeypatch):
    """ON-PREM, SO IT HAS TO GIVE THE MEMORY BACK.

    The face encoder is query-side only — analytics embeds at ingest — so the
    only thing that can want it is a search. Loaded lazily and never released,
    ONE face query pinned ~37 MB of SFace plus cv2's DNN arenas for the life of
    the process, on a service that deliberately hands its models back when
    nothing is asking.

    Driven through the reaper's own body rather than a sleep, so the release is
    deterministic — the pattern test_lifecycle.py uses for the same reason.
    """
    import index.models as models_mod
    from index.config import AppConfig
    from index.models import ModelPool

    class FakeEncoder:
        active = True
        name = "sface-2021dec"
        dim = 128

        def snapshot(self):
            return {"active": True}

    cfg = AppConfig()
    cfg.lifecycle.idle_timeout_seconds = 0.001
    cfg.store.crop_dir = str(tmp_path)
    pool = ModelPool(cfg, store=None, writer=None)
    monkeypatch.setattr("index.faces.build", lambda *a, **k: FakeEncoder())

    assert pool.face_encoder() is not None
    assert pool._faces is not None                      # noqa: SLF001

    # CAMERAS ARE REGISTERED, which is the state every live appliance is in.
    # The ingest encoder stays resident here on purpose; the face encoder must
    # not, because nothing at ingest uses it. Tying it to the ingest idle clock
    # pinned it for the life of the process on every box that has cameras —
    # found by watching the real service rather than by this test.
    pool.want_ingest(True)

    import time as _t
    _t.sleep(5 * cfg.lifecycle.idle_timeout_seconds)
    pool._tick()                                        # noqa: SLF001

    assert pool._faces is None, \
        "the face model stayed resident with nothing searching"        # noqa: SLF001
    # And it must be loadable again — the "already tried" flag exists to stop
    # re-probing a missing file, not to make a released model unreachable.
    assert pool.face_encoder() is not None


def test_a_face_query_keeps_the_encoder_alive_while_someone_is_working(tmp_path, monkeypatch):
    """The other half. Releasing between two searches by an operator who has
    not stopped would reload 37 MB per query — the trade hibernation exists to
    avoid."""
    from index.config import AppConfig
    from index.models import ModelPool

    cfg = AppConfig()
    cfg.lifecycle.idle_timeout_seconds = 60
    cfg.store.crop_dir = str(tmp_path)
    pool = ModelPool(cfg, store=None, writer=None)
    monkeypatch.setattr("index.faces.build",
                        lambda *a, **k: type("E", (), {"active": True,
                                                       "name": "sface-2021dec",
                                                       "dim": 128,
                                                       "snapshot": lambda s: {}})())
    before = pool._last_face_query                      # noqa: SLF001
    pool.face_encoder()
    assert pool._last_face_query > before, \
        "a face search did not count as a search, so the reaper may drop the model under it"  # noqa: SLF001
    pool._tick()                                        # noqa: SLF001
    assert pool._faces is not None                      # noqa: SLF001
