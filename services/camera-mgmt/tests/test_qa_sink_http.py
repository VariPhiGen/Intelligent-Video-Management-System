"""The QA sink form: who may touch it, and what it refuses to accept.

THE AUTHORISATION TESTS ARE THE POINT OF THIS FILE. This form is the decision
to send frames of identifiable people to a system nobody here administers, to
copies that an erasure request raised on this appliance cannot reach. Drawing a
box is an operator act; deciding where boxes go is not. A regression that
widened this gate from admin to `data_collection` — or to any authenticated
principal, which is what forgetting the dependency produces — would not break a
single other test in this suite.

The validation tests exist because every field this form can leave blank fails
SILENTLY at runtime, hours later, inside a relay loop. Refusing at save time is
the only point where the person who made the mistake is still present.

WHAT IS NOT HERE: what reached the `qa_sink_config` table. The conftest `db` is
a StubSession; these are contract tests over status codes and response shapes.
"""
from __future__ import annotations

import pytest

URL = "/api/qa-sink"


def _body(**over):
    body = {
        "enabled": True,
        "bootstrap_servers": "43.205.29.76:9092",
        "kafka_topic": "vms-event-consume",
        "payload_topic": "vms_annotation",
        "severity": "low",
        "s3_bucket": "vms-qa-frames",
        "s3_prefix": "annotations/",
        "class_map": {"no_helmet": "PPE-No Helmet"},
    }
    body.update(over)
    return body


# ── Who may touch it ────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [
    ("get", URL), ("put", URL), ("get", f"{URL}/outbox"),
])
async def test_unauthenticated_is_refused(client, method, path):
    # `request` rather than `client.get(...)`: httpx refuses a body on GET
    # through the named helpers, and the body is irrelevant here anyway — the
    # refusal must happen before anything looks at it.
    response = await client.request(method.upper(), path, json=_body())
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
@pytest.mark.parametrize("roles", [(), ("operator",), ("viewer",)])
async def test_reading_the_destination_is_admin_only(client, mint, roles):
    """Read included, and deliberately so.

    The brokers, the bucket and the class map together describe exactly where
    this site's footage goes. That is not information an operator needs to do
    their job, and the taxonomy page next door already sets the precedent of
    admin-gating fleet configuration on read as well as write.
    """
    response = await client.get(URL, headers=mint(roles=list(roles)))
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_data_collection_capability_is_not_enough(client, mint):
    """The capability that governs the LOCAL training pile does not govern
    shipping frames off the appliance. Different authority, different gate."""
    response = await client.put(URL, json=_body(),
                                headers=mint(roles=["operator"]))
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_admin_may_read_it(client, mint):
    response = await client.get(URL, headers=mint(roles=["admin"]))
    assert response.status_code == 200


# ── What a fresh appliance looks like ───────────────────────────────────────


@pytest.mark.asyncio
async def test_an_appliance_that_never_configured_one_answers_disabled(
        client, mint):
    """Not a 404. "No sink" is a valid, expected state with a valid answer,
    and a 404 would make the admin page render an error for the majority of
    installations that will never use this."""
    response = await client.get(URL, headers=mint(roles=["admin"]))
    assert response.status_code == 200
    assert response.json()["enabled"] is False


# ── Refusing a sink that cannot deliver ─────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ["bootstrap_servers", "kafka_topic",
                                   "s3_bucket"])
async def test_enabling_without_a_complete_destination_is_refused(
        client, mint, blank):
    """Each of these fails silently later: no brokers means the producer never
    connects, no topic means messages land nowhere a consumer is listening, no
    bucket means every review item shows a broken image."""
    response = await client.put(URL, json=_body(**{blank: ""}),
                                headers=mint(roles=["admin"]))
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_an_incomplete_sink_may_be_saved_while_switched_off(client, mint):
    """Half-filling the form and coming back to it must work.

    The completeness rule is about ENABLING, not about storing — forcing every
    field before a draft can be saved would make the page unusable.
    """
    response = await client.put(
        URL, json=_body(enabled=False, bootstrap_servers="", s3_bucket=None),
        headers=mint(roles=["admin"]))
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_a_malformed_bucket_name_is_refused_at_save_time(client, mint):
    """Not at first publish, days later, inside a relay log.

    S3 rejects this with a raw botocore ParamValidationError that surfaces as
    a 500 from a background task — by which time nobody remembers what they
    typed.
    """
    response = await client.put(URL, json=_body(s3_bucket="Not A Bucket"),
                                headers=mint(roles=["admin"]))
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_a_class_map_key_that_is_not_a_label_class_key_is_refused(
        client, mint):
    """Label class keys are lowercase, digits and underscores. A key shaped
    like anything else can never match a real class, so the mapping it carries
    is dead on arrival and is better refused than silently inert."""
    response = await client.put(
        URL, json=_body(class_map={"PPE-No Helmet": "no_helmet"}),
        headers=mint(roles=["admin"]))
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_an_empty_subcategory_is_refused(client, mint):
    """A blank mapping would publish an empty box label, which renders as an
    unlabelled grey rectangle in the reviewer's queue."""
    response = await client.put(URL, json=_body(class_map={"no_helmet": ""}),
                                headers=mint(roles=["admin"]))
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_unknown_fields_are_refused(client, mint):
    """`extra="forbid"`, so a renamed field fails loudly instead of being
    accepted and ignored — the failure mode where the UI says "saved" and the
    setting never took effect."""
    response = await client.put(URL, json=_body(kafka_brokers="x:9092"),
                                headers=mint(roles=["admin"]))
    assert response.status_code == 422


# ── The credential ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_sasl_password_never_comes_back_out(client, mint):
    """Write-only, the same contract camera credentials have.

    The form still needs to distinguish "a password is stored" from "no
    password", which is what the boolean is for — it answers the question
    without the value leaving the appliance.
    """
    saved = await client.put(
        URL, json=_body(sasl_mechanism="PLAIN", sasl_username="vms",
                        sasl_password="hunter2"),
        headers=mint(roles=["admin"]))
    assert saved.status_code == 200
    body = saved.json()
    assert "sasl_password" not in body
    assert "hunter2" not in saved.text
    assert body["sasl_password_set"] is True


@pytest.mark.asyncio
async def test_an_unsupported_sasl_mechanism_is_refused(client, mint):
    response = await client.put(URL, json=_body(sasl_mechanism="KERBEROS"),
                                headers=mint(roles=["admin"]))
    assert response.status_code == 422


# ── The outbox ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_outbox_is_admin_only(client, mint):
    response = await client.get(f"{URL}/outbox", headers=mint(roles=["operator"]))
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_outbox_reports_every_state_even_at_zero(client, mint):
    """A missing key and a zero are different answers, and the admin page has
    to render "0 failed" rather than nothing where the number should be."""
    response = await client.get(f"{URL}/outbox", headers=mint(roles=["admin"]))
    assert response.status_code == 200
    assert set(response.json()["counts"]) == {"pending", "sent", "failed"}


@pytest.mark.asyncio
async def test_outbox_state_filter_rejects_an_invented_state(client, mint):
    response = await client.get(f"{URL}/outbox?state=exploded",
                                headers=mint(roles=["admin"]))
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_retrying_something_that_was_never_queued_is_a_404(client, mint):
    response = await client.post(
        f"{URL}/outbox/3f2504e0-4f89-11d3-9a0c-0305e82c3301/retry",
        headers=mint(roles=["admin"]))
    assert response.status_code == 404


# ── The sink's own S3 credentials ───────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("half", [
    {"aws_access_key_id": "AKIAEXAMPLE"},
    {"aws_secret_access_key": "s3cret"},
])
async def test_half_a_credential_is_refused(client, mint, half):
    """Neither half works alone, and the failure each causes is different.

    A key ID with no secret cannot sign at all. A secret with no key ID is an
    orphaned credential encrypted in a row nothing reads. Both surface, if at
    all, as an AccessDenied in a relay log an hour later — so they are refused
    at the form, while the person who typed them is still looking at it.
    """
    response = await client.put(URL, json=_body(**half),
                                headers=mint(roles=["admin"]))
    assert response.status_code == 422
    assert "access key" in response.text.lower()


@pytest.mark.asyncio
async def test_a_complete_key_pair_is_accepted_and_NEITHER_half_comes_back(
        client, mint):
    """The key ID is write-only alongside the secret.

    It is an identifier rather than a secret, but it names a live credential —
    it is what tells an attacker WHICH key to hunt for, and a screenshot of
    this form should not identify the account's keys. So the GET answers two
    booleans and no values.
    """
    saved = await client.put(
        URL, json=_body(aws_access_key_id="AKIAEXAMPLE",
                        aws_secret_access_key="s3cret", aws_region="ap-south-1"),
        headers=mint(roles=["admin"]))
    assert saved.status_code == 200
    body = saved.json()
    assert "s3cret" not in saved.text
    assert "AKIAEXAMPLE" not in saved.text
    assert "aws_access_key_id" not in body
    assert body["aws_access_key_id_set"] is True
    assert body["aws_secret_access_key_set"] is True
    # The form renders "using this appliance's credentials" off this flag, so
    # it has to flip the moment a key is set.
    assert body["aws_uses_environment"] is False


@pytest.mark.asyncio
async def test_clearing_one_half_retires_the_other(client, mint):
    """An explicit empty string clears the pair, rather than leaving a secret
    encrypted in a row whose key ID is gone."""
    headers = mint(roles=["admin"])
    await client.put(URL, json=_body(aws_access_key_id="AKIAEXAMPLE",
                                     aws_secret_access_key="s3cret"),
                     headers=headers)
    cleared = await client.put(URL, json=_body(aws_access_key_id=""),
                               headers=headers)
    assert cleared.status_code == 200
    body = cleared.json()
    assert body["aws_access_key_id_set"] is False
    assert body["aws_secret_access_key_set"] is False
    assert body["aws_uses_environment"] is True


@pytest.mark.asyncio
async def test_no_credentials_reports_that_it_uses_the_environment(client, mint):
    """Two blank fields are the supported default. Without this flag the form
    cannot tell an administrator that, and blank reads as broken."""
    saved = await client.put(URL, json=_body(), headers=mint(roles=["admin"]))
    assert saved.status_code == 200
    body = saved.json()
    assert body["aws_uses_environment"] is True
    assert body["aws_access_key_id_set"] is False
    assert body["aws_secret_access_key_set"] is False
