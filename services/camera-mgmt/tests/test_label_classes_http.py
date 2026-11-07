"""The taxonomy over HTTP: who may touch it, and what the edge refuses.

WHAT IS NOT HERE. The conftest `db` is a StubSession whose reads come back
empty, so anything that needs a SELECT — listing, the annotation counts, the
delete-vs-retire rule — lives in test_label_classes_db.py against a real
database. This file is the authorisation and validation layer, which is the
half that only exists once a REQUEST is routed.
"""
from __future__ import annotations

import pytest

URL = "/api/label-classes"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["viewer", "operator", "supervisor", "dpo"])
async def test_only_an_admin_may_author_the_taxonomy(client, mint, role):
    """The taxonomy is fleet configuration, not an operator action. A
    supervisor may draw boxes; deciding what the fleet is allowed to call them
    is administration."""
    r = await client.post(URL, json={"key": "forklift", "label": "Forklift",
                                     "color": "#ff8800"},
                          headers=mint([role]))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_an_admin_may_reach_the_route(client, mint):
    r = await client.post(URL, json={"key": "forklift", "label": "Forklift",
                                     "color": "#ff8800"},
                          headers=mint(["admin"]))
    assert r.status_code == 201


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["Forklift", "fork lift", "fork-lift", "",
                                 "x" * 65])
async def test_a_key_that_is_not_a_slug_is_refused(client, mint, key):
    """The key travels into COCO categories and Label Studio configs. Anything
    that needs quoting there is refused here instead."""
    r = await client.post(URL, json={"key": key, "label": "X",
                                     "color": "#ff8800"},
                          headers=mint(["admin"]))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_a_colour_that_is_not_a_hex_triplet_is_refused(client, mint):
    r = await client.post(URL, json={"key": "forklift", "label": "Forklift",
                                     "color": "orange"},
                          headers=mint(["admin"]))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_the_key_cannot_be_renamed(client, mint):
    """Annotations name a class by key and exports carry it, so a rename would
    rewrite history. extra=forbid turns the attempt into a 422 rather than a
    silent no-op that looks like it worked."""
    r = await client.patch(f"{URL}/forklift", json={"key": "crate"},
                           headers=mint(["admin"]))
    assert r.status_code == 422


# ── Reading the taxonomy ────────────────────────────────────────────────────
#
# This block replaces a test that asserted reads were admin-only. That was not
# a policy being relaxed — it was a bug being fixed. `POST /annotations` is
# gated on `playback_search` because drawing a box is an operator action, but
# the picker an operator chooses the class from is fed by this list route.
# Admin-only here meant a non-admin was allowed to draw and was then shown an
# empty taxonomy with "no label classes yet", and the Save button never
# enabled. The write was open to them; the read they needed to perform it was
# not.


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["operator", "supervisor", "dpo"])
async def test_every_role_that_may_draw_can_read_the_taxonomy(client, mint, role):
    """Each of these holds `playback_search` in the default policy, so each
    can reach Playback's annotate bar — and must be able to populate it."""
    assert (await client.get(URL, headers=mint([role]))).status_code == 200


@pytest.mark.asyncio
async def test_an_admin_can_still_read_it(client, mint):
    """`policy.allowed` short-circuits for admin, so the capability gate needs
    no special case — this proves the short-circuit is actually reached."""
    assert (await client.get(URL, headers=mint(["admin"]))).status_code == 200


@pytest.mark.asyncio
async def test_a_viewer_still_cannot_read_it(client, mint):
    """A viewer holds neither capability and cannot draw, so the vocabulary
    for drawing is not theirs to see. This is the assertion that stops the fix
    sliding into "any authenticated principal", which would have been the
    easier change and the wrong one."""
    assert (await client.get(URL, headers=mint(["viewer"]))).status_code == 403


@pytest.mark.asyncio
async def test_unauthenticated_still_cannot_read_it(client):
    r = await client.get(URL)
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_the_label_studio_snippet_stays_admin_only(client, mint):
    """It configures somebody's external project, which is administration
    however it is rendered — and it is the one read that did NOT move."""
    assert (await client.get(f"{URL}/label-config",
                             headers=mint(["operator"]))).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["operator", "supervisor", "dpo"])
async def test_being_able_to_read_it_does_not_imply_editing_it(
        client, mint, role):
    """The whole point of splitting the gate. A role that can now LIST the
    taxonomy must not thereby be able to retire a class out from under every
    annotation that names it."""
    assert (await client.patch(f"{URL}/forklift", json={"label": "X"},
                               headers=mint([role]))).status_code == 403
    assert (await client.delete(f"{URL}/forklift",
                                headers=mint([role]))).status_code == 403
