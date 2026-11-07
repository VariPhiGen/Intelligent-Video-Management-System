"""The MQTT driver: what gets published, and what is refused before anything is.

No broker is started. The conftest asserts the absence of outbound connections
at the event loop, so the client is a fake and what these tests pin is the
DECISIONS — payload rendering, topic validation, QoS, retain, and the failure
detail that lands in alerts.notified.

Run (from services/camera-mgmt): python -m pytest tests/test_bridge_mqtt.py -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services.bridge import base  # noqa: E402
from backend.services.bridge import mqtt  # noqa: E402

# NOT a module-level asyncio mark: half this file is pure functions, and
# marking a sync test asyncio is a warning per test that buries a real one.
# The async cases carry the mark themselves.
_aio = pytest.mark.asyncio

NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)

#: Distinctive on purpose, so "it did not leak" is a real assertion
#: rather than a substring that could match anything.
BROKER_PASSWORD = "s3cr3t-broker-pw-9f2a"


def binding(**kw):
    base_ = {"peripheral_slug": "gate-3-barrier", "driver": "mqtt",
             "address": "site/gate3/set", "config": {},
             "state_topic": "site/gate3/state", "verified_at": NOW}
    base_.update(kw)
    return base.Binding(**base_)


class FakeClient:
    """Stands in for aiomqtt.Client. Records publishes; can be made to fail."""

    def __init__(self):
        self.published = []
        self.fail = False

    async def publish(self, topic, payload, qos=0, retain=False):
        if self.fail:
            raise ConnectionError("broker gone")
        self.published.append({"topic": topic, "payload": payload,
                               "qos": qos, "retain": retain})


@pytest.fixture
def driver(monkeypatch):
    d = mqtt.MqttDriver(url="mqtts://broker:8883", username="u",
                        password=BROKER_PASSWORD,
                        tls_verify=True, topic_prefix="vms")
    client = FakeClient()
    monkeypatch.setattr(d, "_client", client, raising=False)
    d._connected = True
    return d, client


# ── Topic validation ─────────────────────────────────────────────────────────

def test_a_state_topic_equal_to_the_command_topic_is_refused():
    """The bridge would hear its own command and record it as device state —
    a loop that looks exactly like the device responding instantly and
    correctly, which is the worst way for it to be wrong."""
    assert mqtt.validate_topics("site/gate3/set", "site/gate3/set") == "topic_loop"


def test_a_state_topic_the_command_wildcard_would_match_is_refused():
    """Subscribing to site/gate3/# while publishing to site/gate3/set is the
    same loop wearing a wildcard."""
    assert mqtt.validate_topics("site/gate3/set", "site/gate3/#") == "topic_loop"


def test_a_single_level_wildcard_that_covers_the_command_topic_is_refused():
    assert mqtt.validate_topics("site/gate3/set", "site/+/set") == "topic_loop"


def test_a_single_level_wildcard_that_does_not_cover_it_is_fine():
    """`+` matches one level. site/+/state never matches site/gate3/set."""
    assert mqtt.validate_topics("site/gate3/set", "site/+/state") is None


def test_a_wildcard_at_a_different_depth_is_fine():
    assert mqtt.validate_topics("site/gate3/set", "other/#") is None


def test_distinct_topics_are_fine():
    assert mqtt.validate_topics("site/gate3/set", "site/gate3/state") is None


def test_a_write_only_device_needs_no_state_topic():
    assert mqtt.validate_topics("site/gate3/set", None) is None


def test_an_empty_command_topic_is_refused():
    assert mqtt.validate_topics("", "site/gate3/state") == "empty_topic"


# ── Payload rendering ────────────────────────────────────────────────────────

def test_the_default_payload_is_the_bare_command():
    """The common case for a relay: publish "open" to its topic."""
    assert mqtt.render_payload({}, "open", {}) == "open"


def test_a_template_is_substituted():
    cfg = {"payload_template": '{"action": "{command}", "src": "vms"}'}
    assert mqtt.render_payload(cfg, "open", {}) == '{"action": "open", "src": "vms"}'


def test_params_are_available_to_the_template():
    cfg = {"payload_template": '{"action":"{command}","level":"{level}"}'}
    assert mqtt.render_payload(cfg, "dim", {"level": "40"}) == \
        '{"action":"dim","level":"40"}'


def test_a_template_referring_to_a_missing_param_does_not_raise():
    """An operator's template typo must be a refused actuation, not a 500 in
    the ingest handler that costs the rest of the batch its events."""
    cfg = {"payload_template": '{"action":"{nope}"}'}
    assert mqtt.render_payload(cfg, "open", {}) is None


def test_a_json_template_keeps_its_own_braces():
    """THE case that decided the mechanism. The most ordinary template here is
    a JSON body, and `str.format` reads its literal braces as fields and raises
    KeyError('"action"'). Substitution is targeted at `{identifier}` instead, so
    an operator never has to type `{{` into a text box."""
    cfg = {"payload_template": '{"action":"{command}","opts":{"retain":false}}'}
    assert mqtt.render_payload(cfg, "open", {}) == \
        '{"action":"open","opts":{"retain":false}}'


def test_an_unbalanced_brace_is_literal_text_not_an_error():
    """The corollary of the above: a brace that does not close is not a
    placeholder, so it is content. The typo worth catching is a MISSPELLED
    placeholder, which is the test above this one."""
    assert mqtt.render_payload({"payload_template": "{unclosed"}, "open", {}) == \
        "{unclosed"


def test_a_none_config_behaves_like_an_empty_one():
    """`config` is NOT NULL in the schema, but a Binding built in a test or by
    a future caller may still carry None, and that must not be a crash."""
    assert mqtt.render_payload(None, "open", {}) == "open"


# ── Actuation ────────────────────────────────────────────────────────────────

@_aio
async def test_actuate_publishes_to_the_binding_address(driver):
    d, client = driver
    r = await d.actuate(binding(), "open", {})
    assert r.ok is True
    assert client.published[0]["topic"] == "site/gate3/set"
    assert client.published[0]["payload"] == "open"


@_aio
async def test_commands_go_out_at_qos_1(driver):
    """At-least-once. A lost "close the barrier" is worse than a duplicate one,
    and every command in the v1 vocabulary is idempotent by design."""
    d, client = driver
    await d.actuate(binding(), "close", {})
    assert client.published[0]["qos"] == 1


@_aio
async def test_commands_are_not_retained(driver):
    """A retained command replays to every future subscriber — including the
    broker handing "open" to a barrier controller that reconnects at 3am."""
    d, client = driver
    await d.actuate(binding(), "open", {})
    assert client.published[0]["retain"] is False


@_aio
async def test_a_broker_failure_is_a_refusal_not_an_exception(driver):
    """The caller is the ingest handler. Nothing here may raise into it."""
    d, client = driver
    client.fail = True
    r = await d.actuate(binding(), "open", {})
    assert r.ok is False
    assert r.detail == "publish_failed"


@_aio
async def test_actuating_while_disconnected_refuses_without_publishing(driver):
    d, client = driver
    d._connected = False
    r = await d.actuate(binding(), "open", {})
    assert r.ok is False
    assert r.detail == "offline"
    assert client.published == []


@_aio
async def test_a_bad_template_refuses_before_publishing(driver):
    """Order matters: a refusal after the publish would mean the device moved
    and the record says it did not."""
    d, client = driver
    r = await d.actuate(binding(config={"payload_template": "{nope}"}), "open", {})
    assert r.ok is False
    assert r.detail == "bad_payload_template"
    assert client.published == []


# ── Health ───────────────────────────────────────────────────────────────────

@_aio
async def test_health_is_not_configured_when_there_is_no_url():
    """No broker configured is a SUPPORTED deployment, not a fault. Reporting
    "offline" would send an operator hunting a broker that was never meant to
    exist."""
    d = mqtt.MqttDriver(url="", username=None, password=None,
                        tls_verify=True, topic_prefix="vms")
    assert await d.health() == "not_configured"


@_aio
async def test_health_is_offline_when_configured_but_disconnected(driver):
    d, _ = driver
    d._connected = False
    assert await d.health() == "offline"


@_aio
async def test_health_is_connected_when_connected(driver):
    d, _ = driver
    assert await d.health() == "connected"


@_aio
async def test_the_broker_password_never_reaches_a_result_or_a_log(driver, caplog):
    """A credential in a log line outlives the incident that produced it. The
    failure path is the one that tends to leak, so that is the one tested."""
    import logging
    caplog.set_level(logging.DEBUG)
    d, client = driver
    client.fail = True
    r = await d.actuate(binding(), "open", {})
    assert r.ok is False
    assert BROKER_PASSWORD not in r.detail
    assert BROKER_PASSWORD not in caplog.text


# ── start(): the arguments the real client is built with ─────────────────────
#
# start() is the one method the rest of this file cannot reach, because it
# opens a socket. Three bugs were found there against a real Mosquitto that
# nothing here could have caught. A stub `aiomqtt` module pins the DECISION —
# which kwargs the client is built with — without a connection.

class _StubTLSParameters:
    def __init__(self, **kw):
        self.kw = kw


class _StubClientFactory:
    """Records the kwargs and refuses to connect, so start() returns at once."""

    def __init__(self):
        self.kwargs = None

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        factory = self

        class _Ctx:
            async def __aenter__(self_):
                raise ConnectionError("stub: no broker")

            async def __aexit__(self_, *a):
                return False
        return _Ctx()


@pytest.fixture
def stub_aiomqtt(monkeypatch):
    import types
    factory = _StubClientFactory()
    mod = types.ModuleType("aiomqtt")
    mod.Client = factory
    mod.TLSParameters = _StubTLSParameters
    monkeypatch.setitem(sys.modules, "aiomqtt", mod)
    return factory


@_aio
async def test_a_plain_broker_gets_no_tls_arguments_at_all(stub_aiomqtt):
    """paho refuses `tls_insecure` without an SSL context — even False — so
    passing it for mqtt:// broke every plain broker. Found on the appliance."""
    d = mqtt.MqttDriver(url="mqtt://broker:1883", username=None, password=None,
                        tls_verify=True, topic_prefix="vms")
    with pytest.raises(ConnectionError):
        await d.start()
    kw = stub_aiomqtt.kwargs
    assert kw["hostname"] == "broker" and kw["port"] == 1883
    assert "tls_params" not in kw and "tls_insecure" not in kw


@_aio
async def test_verified_tls_uses_the_system_cas(stub_aiomqtt):
    d = mqtt.MqttDriver(url="mqtts://broker:8883", username="u", password="p",
                        tls_verify=True, topic_prefix="vms")
    with pytest.raises(ConnectionError):
        await d.start()
    kw = stub_aiomqtt.kwargs
    assert kw["tls_params"].kw == {"cert_reqs": None}   # paho's CERT_REQUIRED
    assert "tls_insecure" not in kw


@_aio
async def test_the_verify_off_switch_actually_switches_verification_off(stub_aiomqtt):
    """`tls_insecure` alone only skips the hostname match. A site with a
    private CA it has not installed needs the certificate check off too."""
    import ssl
    d = mqtt.MqttDriver(url="mqtts://broker:8883", username="u", password="p",
                        tls_verify=False, topic_prefix="vms")
    with pytest.raises(ConnectionError):
        await d.start()
    kw = stub_aiomqtt.kwargs
    assert kw["tls_params"].kw == {"cert_reqs": ssl.CERT_NONE}
    assert kw["tls_insecure"] is True


@_aio
async def test_a_failed_start_leaves_the_driver_offline(stub_aiomqtt):
    d = mqtt.MqttDriver(url="mqtt://broker:1883", username=None, password=None,
                        tls_verify=True, topic_prefix="vms")
    with pytest.raises(ConnectionError):
        await d.start()
    assert await d.health() == "offline"
    assert d._client is None


class _DyingClientFactory:
    """A client that connects, then has its session drop: the message iterator
    raises the way aiomqtt's does when the broker goes away. Records whether
    the session subscribed, and how many messages the driver drained."""

    def __init__(self):
        self.subscribed = []
        self.iterated = 0

    def __call__(self, **kwargs):
        factory = self

        class _Messages:
            def __aiter__(self_):
                return self_

            async def __anext__(self_):
                factory.iterated += 1
                raise ConnectionError("Disconnected during message iteration")

        class _Client:
            messages = _Messages()

            async def subscribe(self_, topic, qos=0):
                factory.subscribed.append(topic)

        class _Ctx:
            async def __aenter__(self_):
                return _Client()

            async def __aexit__(self_, *a):
                return False
        return _Ctx()


@pytest.fixture
def dying_aiomqtt(monkeypatch):
    import types
    factory = _DyingClientFactory()
    mod = types.ModuleType("aiomqtt")
    mod.Client = factory
    mod.TLSParameters = _StubTLSParameters
    monkeypatch.setitem(sys.modules, "aiomqtt", mod)
    return factory


@_aio
async def test_a_publish_only_session_ends_when_the_broker_goes(dying_aiomqtt):
    """The kill-the-broker step. The subscriber noticed at once; the four
    publish-only sessions slept through it and reported `connected` for
    minutes, so an actuation would have been "published" into a dead socket.
    A publish-only session must watch the session too — and without
    subscribing to anything, because that is the elected worker's job."""
    d = mqtt.MqttDriver(url="mqtt://broker:1883", username=None, password=None,
                        tls_verify=True, topic_prefix="vms")
    d.set_state_topics(["vms/gate3/state"])
    with pytest.raises(ConnectionError):
        await d.start(subscriptions=False)
    assert dying_aiomqtt.iterated == 1          # it was watching the session
    assert dying_aiomqtt.subscribed == []       # and subscribed to nothing
    assert await d.health() == "offline"        # and says so afterwards
    assert d._client is None


@_aio
async def test_the_subscriber_session_subscribes_and_ends_the_same_way(dying_aiomqtt):
    d = mqtt.MqttDriver(url="mqtt://broker:1883", username=None, password=None,
                        tls_verify=True, topic_prefix="vms")
    d.set_state_topics(["vms/gate3/state", "vms/door1/state"])
    with pytest.raises(ConnectionError):
        await d.start(subscriptions=True)
    assert dying_aiomqtt.subscribed == ["vms/gate3/state", "vms/door1/state"]
    assert await d.health() == "offline"


# ── Subscribing to bindings created after the session opened ─────────────────
#
# The subscription set used to be fixed at connect time: `set_state_topics`
# then `start()`, and nothing re-read it. An operator who bound a peripheral
# after the API booted got a device that actuated correctly and reported
# `last_state` NULL forever — the wall said "unknown" and it read as a broken
# bridge. Found by binding a siren against a live Mosquitto and watching the
# retained state arrive only after a restart.

class SubRecordingClient(FakeClient):
    """Records subscribe/unsubscribe as well as publishes."""

    def __init__(self):
        super().__init__()
        self.subscribed = []
        self.unsubscribed = []

    async def subscribe(self, topic, qos=0):
        if self.fail:
            raise ConnectionError("broker gone")
        self.subscribed.append(topic)

    async def unsubscribe(self, topic):
        if self.fail:
            raise ConnectionError("broker gone")
        self.unsubscribed.append(topic)


@pytest.fixture
def live(monkeypatch):
    d = mqtt.MqttDriver(url="mqtt://broker:1883", username=None, password=None,
                        tls_verify=False, topic_prefix="vms")
    client = SubRecordingClient()
    monkeypatch.setattr(d, "_client", client, raising=False)
    d._connected = True
    return d, client


@_aio
async def test_sync_topics_subscribes_a_topic_added_after_connect(live):
    """THE regression. A binding saved while the session is already open must
    start reporting state without a restart."""
    d, client = live
    d.set_state_topics(["vms/gate3/state"])
    await d.sync_topics(["vms/gate3/state"])
    client.subscribed.clear()

    await d.sync_topics(["vms/gate3/state", "vms/siren/state"])
    assert client.subscribed == ["vms/siren/state"]


@_aio
async def test_sync_topics_does_not_resubscribe_what_it_already_holds(live):
    """Re-subscribing every tick would be needless broker traffic, and on some
    brokers a duplicate subscription is a duplicate delivery."""
    d, client = live
    await d.sync_topics(["vms/gate3/state"])
    client.subscribed.clear()
    await d.sync_topics(["vms/gate3/state"])
    assert client.subscribed == []


@_aio
async def test_sync_topics_unsubscribes_a_removed_binding(live):
    """Unbinding a peripheral should stop its traffic. The customer's broker
    carries their whole smart home; holding a subscription we no longer honour
    is the rudeness this design set out to avoid."""
    d, client = live
    await d.sync_topics(["vms/gate3/state", "vms/siren/state"])
    await d.sync_topics(["vms/gate3/state"])
    assert client.unsubscribed == ["vms/siren/state"]


@_aio
async def test_sync_topics_is_a_no_op_while_disconnected(live):
    """Between sessions there is no client. The next connect subscribes the
    full set, so a reconcile tick that lands in the gap must not raise."""
    d, client = live
    d._connected = False
    await d.sync_topics(["vms/gate3/state"])
    assert client.subscribed == []


@_aio
async def test_a_broker_failure_during_sync_does_not_raise(live):
    """This runs in a background reconcile task. Raising there kills the task
    and the subscription silently stops healing — the exact failure mode this
    fix exists to remove."""
    d, client = live
    client.fail = True
    await d.sync_topics(["vms/gate3/state"])      # must not raise


@_aio
async def test_a_failed_subscribe_is_retried_next_tick(live):
    """A topic that failed to subscribe must not be recorded as held, or the
    reconcile would skip it forever."""
    d, client = live
    client.fail = True
    await d.sync_topics(["vms/gate3/state"])
    client.fail = False
    await d.sync_topics(["vms/gate3/state"])
    assert client.subscribed == ["vms/gate3/state"]


@_aio
async def test_reconnecting_resubscribes_the_whole_set(live):
    """A dropped session loses its subscriptions. The driver must not believe
    it still holds them, or a reconnect would leave the wall dark."""
    d, client = live
    await d.sync_topics(["vms/gate3/state"])
    await d.stop()
    # A reconnect brings a NEW client: stop() drops both the flag and the
    # handle, and restoring only the flag would leave sync_topics rightly
    # refusing to touch a client that is not there.
    d._client, d._connected = client, True
    client.subscribed.clear()
    await d.sync_topics(["vms/gate3/state"])
    assert client.subscribed == ["vms/gate3/state"]
