"""bridge — the outbound port to the physical world.

The only module the rest of the application imports; everything below it is an
implementation detail of "how do we reach this device".

`Bridge.actuate` is THE single entry point to physical actuation in the
product. Every refusal is decided there, before a driver is chosen, so the
boundary rule (spec 3.1) cannot be lost by a driver that forgets it — a driver
never sees a target it was not allowed to touch.

DIRECTION DECIDES ELECTION. Outbound commands are NOT elected: any of the four
uvicorn workers may publish, several MQTT clients publishing is normal, and
every v1 command is idempotent. Inbound state IS elected, because
`peripherals.last_state` is the column the device wall renders and it must have
exactly one writer. See main.py.
"""
from __future__ import annotations

from typing import Optional

import structlog

from ...models import PendingAction, Peripheral, PeripheralBinding
from ...config import settings
from . import base
from .base import ActuationResult, Binding
from .homeassistant import HaDriver
from .mqtt import MqttDriver
from .webhook import WebhookDriver

log = structlog.get_logger(__name__)


# verify() outcomes that mean "could not check", as opposed to "checked and
# it is wrong". Every driver answers `offline` for a dead session, and
# verify_bindings maps a driver that raised to `verify_error`.
_COULD_NOT_CHECK = frozenset({"offline", "verify_error"})
#: A verify that reached the address and found the DEVICE unwell (webhook
#: health checks: `unhealthy:modbus-tcp/timeout`). See verify_bindings.
UNHEALTHY = "unhealthy:"


class Bridge:
    """Holds the configured drivers and routes one actuation to one of them."""

    def __init__(self) -> None:
        self._drivers: dict = {}

    # ── construction ─────────────────────────────────────────────────────────
    def configure(self) -> None:
        """Build the drivers this deployment has configuration for.

        A driver with no configuration is still CONSTRUCTED — it reports
        `not_configured` rather than being absent — so `driver_not_configured`
        and "this driver does not exist" stay the same, honest answer.
        """
        self._drivers = {
            "mqtt": MqttDriver(
                url=settings.mqtt_url, username=settings.mqtt_username or None,
                password=settings.mqtt_password or None,
                tls_verify=settings.mqtt_tls_verify,
                topic_prefix=settings.mqtt_topic_prefix),
            "homeassistant": HaDriver(url=settings.ha_url,
                                      token=settings.ha_token or None),
            "webhook": WebhookDriver(enabled=settings.webhook_driver_enabled),
        }

    # ── the one entry point ──────────────────────────────────────────────────
    async def actuate(self, db, peripheral_slug: str, command: str,
                      params: Optional[dict] = None) -> ActuationResult:
        """The ONLY path to physical actuation in the product.

        Order matters and is the whole design: the registry lookup and
        base.resolve_target come BEFORE any driver is chosen, so a refusal
        cannot be skipped by a driver that forgets to check.
        """
        peripheral, binding = await self._lookup(db, peripheral_slug)
        refusal = base.resolve_target(peripheral, binding)
        if refusal is not None:
            return refusal
        driver = self._drivers.get(binding.driver)
        if driver is None:
            # Configured once, not any more. Silence would look like success.
            return ActuationResult(False, "driver_not_configured")
        state = await driver.health()
        if state == "not_configured":
            return ActuationResult(False, "driver_not_configured")
        try:
            return await driver.actuate(binding, command, params or {})
        except Exception as exc:                                # noqa: BLE001
            # A driver written later must not be able to 500 the ingest
            # handler by raising something nobody anticipated here.
            log.warning("bridge.driver_error", driver=binding.driver,
                        peripheral=peripheral_slug, error=str(exc))
            return ActuationResult(False, "driver_error")

    async def pulse(self, db, peripheral_slug: str, command: str, *, then: str,
                    for_seconds: int, params: Optional[dict] = None,
                    alert_id=None, now: Optional[datetime] = None) -> ActuationResult:
        """Open-then-close: the close made durable BEFORE the open publishes.

        The other half of actuate(), for an action with `for_seconds` and
        `then`. pending.pulse owns the ordering; this owns the boundary — the
        same registry lookup and resolve_target as actuate(), so an unknown
        or unbound peripheral is refused before a follow-up row exists for
        it. A refusal AFTER the row is pending.pulse's job (it withdraws the
        row), because by then the open may have been attempted.

        FOUND BY RUNNING THE CHAIN. pending.pulse had no caller: the engine
        stripped for_seconds/then from every action and called actuate(), so
        a siren rule's `on` had no `off` behind it and only the translator's
        own watchdog silenced it. Every pulse test passed, because they
        tested the function and not the wiring.
        """
        from . import pending

        peripheral, binding = await self._lookup(db, peripheral_slug)
        refusal = base.resolve_target(peripheral, binding)
        if refusal is not None:
            return refusal

        async def _publish(slug, cmd, prm):
            return await self.actuate(db, slug, cmd, prm)

        return await pending.pulse(
            db, peripheral_id=peripheral.id, slug=peripheral_slug,
            command=command, then=then, for_seconds=int(for_seconds),
            params=params or {}, alert_id=alert_id,
            now=now or datetime.now(timezone.utc), publish=_publish)

    # ── health ───────────────────────────────────────────────────────────────
    async def hold(self, db, peripheral_slug: str, command: str, *, then: str,
                   camera: str, zone: Optional[str], cap_seconds: int,
                   grace_s: int, params: Optional[dict] = None,
                   alert_id=None, now: Optional[datetime] = None) -> ActuationResult:
        """ON now, OFF once the zone is empty: pulse()'s boundary and ordering,
        with pending.hold deciding when the OFF is due."""
        from . import pending

        peripheral, binding = await self._lookup(db, peripheral_slug)
        refusal = base.resolve_target(peripheral, binding)
        if refusal is not None:
            return refusal

        async def _publish(slug, cmd, prm):
            return await self.actuate(db, slug, cmd, prm)

        return await pending.hold(
            db, peripheral_id=peripheral.id, slug=peripheral_slug, command=command,
            then=then, cap_seconds=int(cap_seconds), grace_s=int(grace_s),
            camera=camera, zone=zone, params=params or {}, alert_id=alert_id,
            now=now or datetime.now(timezone.utc), publish=_publish)

    async def health(self, db=None) -> dict:
        """One state for the chip, plus each driver for the detail panel.

        A driver that is merely UNCONFIGURED does not drag the bridge to
        degraded: an HA driver with no URL on an MQTT-only site is not a fault,
        and counting it as one would leave every such deployment permanently
        showing a problem it does not have.
        """
        states = {name: await d.health() for name, d in self._drivers.items()}
        if db is not None and states.get("webhook") == "connected":
            # A webhook driver has no session to be "connected" with: it is
            # only configured if some device is bound to it. Without this a
            # site with nothing set up would show "Bridge connected".
            from sqlalchemy import select
            used = (await db.execute(
                select(PeripheralBinding.peripheral_id)
                .where(PeripheralBinding.driver == "webhook").limit(1))).first()
            if used is None:
                states["webhook"] = "not_configured"
        live = [s for s in states.values() if s != "not_configured"]
        if not live:
            overall = "not_configured"
        elif all(s == "connected" for s in live):
            overall = "connected"
        elif any(s == "connected" for s in live):
            overall = "degraded"
        else:
            overall = "offline"
        return {"state": overall, "drivers": states}

    # ── verification ─────────────────────────────────────────────────────────
    async def verify_bindings(self, db, *, now: datetime) -> int:
        """Re-resolve every binding. Returns how many now verify.

        WITHOUT THIS THE WHOLE FEATURE IS INERT. `base.resolve_target` refuses
        a NULL `verified_at`, which is the right default — but nothing else
        ever sets it, so until this runs every binding is permanently
        unverified and no rule and no operator can move anything.

        A binding that STOPS resolving loses its stamp. That is the point of
        re-running rather than verifying once at save: a renamed HA entity must
        not keep actuating on a check that passed last month.

        A binding that COULD NOT BE CHECKED keeps it. "The broker is down" says
        nothing about the address, and actuate() refuses on a dead session
        anyway; see _COULD_NOT_CHECK.
        """
        rows = (await db.execute(select(PeripheralBinding))).scalars().all()
        verified = 0
        for row in rows:
            binding = Binding(
                peripheral_slug="", driver=row.driver, address=row.address,
                config=row.config or {}, state_topic=row.state_topic,
                verified_at=row.verified_at)
            driver = self._drivers.get(row.driver)
            state = None
            if driver is None:
                ok, detail = False, "driver_not_configured"
            else:
                try:
                    result = await driver.verify(binding)
                    ok, detail, state = result.ok, result.detail, result.state
                except Exception as exc:                        # noqa: BLE001
                    # One bad driver must not stop every other binding being
                    # checked — this is a background tick, not a request.
                    log.warning("bridge.verify.driver_raised",
                                driver=row.driver, error=str(exc))
                    ok, detail = False, "verify_error"
            if ok:
                row.verified_at = now
                row.verify_error = None
                verified += 1
                if state:
                    # A health check that read the device (a webhook to the
                    # device gateway) is the wall's state for a write-only
                    # driver. This loop is elected like state_loop, and
                    # _write_state is still the one function that writes it.
                    await _write_state(db, row.peripheral_id, state[:40], now)
            elif detail.startswith(UNHEALTHY):
                # The address is right but the DEVICE did not answer well.
                # Keep a stamp it already had: one missed health check must
                # not block an alarm, and the attempt itself still reports
                # its own failure. The wall shows the reason, and the state
                # it last read is no longer true, so it goes to unknown.
                row.verify_error = detail[:120]
                if row.verified_at is not None:
                    verified += 1
                await db.execute(
                    update(Peripheral).where(Peripheral.id == row.peripheral_id)
                    .values(last_state=None))
            elif detail in _COULD_NOT_CHECK:
                # The check did not run; nothing is known about the address
                # either way. KEEP the stamp it had. Stripping it here turned
                # every broker blip into a lockout: the session came back and
                # nothing could be actuated until the next tick — 300 s by
                # default — with the UI blaming the ADDRESS for it. actuate()
                # already refuses on a dead session, so the strip protected
                # nothing. Seen on the appliance during the kill-the-broker
                # step. A binding that was never verified stays that way.
                row.verify_error = detail[:120]
                if row.verified_at is not None:
                    verified += 1
            else:
                row.verified_at = None
                row.verify_error = detail[:120]
        if rows:
            await db.commit()
        return verified

    def driver_for(self, binding: Binding):
        return self._drivers.get(binding.driver)

    # ── the registry lookup ──────────────────────────────────────────────────
    async def _lookup(self, db, slug: str):
        """(peripheral, Binding|None). The registry is the boundary."""
        from sqlalchemy import select

        peripheral = (await db.execute(
            select(Peripheral).where(Peripheral.slug == slug)
        )).scalars().first()
        if peripheral is None:
            return None, None
        row = (await db.execute(
            select(PeripheralBinding)
            .where(PeripheralBinding.peripheral_id == peripheral.id)
        )).scalars().first()
        if row is None:
            return peripheral, None
        return peripheral, Binding(
            peripheral_slug=peripheral.slug, driver=row.driver,
            address=row.address, config=row.config or {},
            state_topic=row.state_topic, verified_at=row.verified_at)


_bridge: Optional[Bridge] = None


def get_bridge() -> Bridge:
    """The per-worker singleton. Four workers hold four of these, which is
    correct for the outbound half — see the module docstring."""
    global _bridge
    if _bridge is None:
        _bridge = Bridge()
        _bridge.configure()
    return _bridge


# ── Inbound state: the elected half ──────────────────────────────────────────
# Everything below runs in ONE worker. `peripherals.last_state` is the column
# the device wall renders and it must have exactly one writer; four workers
# racing on it would be a write storm on the one table whose honesty this
# design rests on.
#
# run_as_leader is FAIL-OPEN, and that is correct here: if Valkey is gone,
# several workers writing the same state value is wasteful, not wrong. It would
# NOT be correct for an actuation gate, which fails closed in rules_engine.
# The two rules look alike and mean the opposite.

import asyncio
import contextlib                                                  # noqa: E402
from datetime import datetime, timedelta, timezone              # noqa: E402

from sqlalchemy import delete, func, select, update             # noqa: E402


def is_stale(last_seen, *, now: datetime) -> bool:
    """Is this reading history rather than status?

    Pure, so the rule is testable without a clock. A naive timestamp is read as
    UTC rather than raising: comparing aware to naive is a TypeError, and a
    status column must not be able to take down the poll that refreshes it.

    A reading from the FUTURE is not stale — clock skew runs both ways and a
    device slightly ahead is not history.
    """
    if last_seen is None:
        return True
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    return (now - last_seen) > timedelta(seconds=settings.bridge_state_stale_s)


async def _peripheral_id_for_topic(db, topic: str):
    """The peripheral this state topic belongs to, or None.

    None is the common case and not an error: the customer's broker carries
    their whole smart home, and the boundary rule applies inbound as well as
    out — only devices in the VMS inventory get rows.
    """
    return (await db.execute(
        select(PeripheralBinding.peripheral_id)
        .where(PeripheralBinding.state_topic == topic)
    )).scalars().first()


async def _write_state(db, peripheral_id: int, state: str,
                       at: datetime) -> None:
    """THE ONLY WRITER of peripherals.last_state / last_seen.

    Not a method on Bridge, and not reachable from a router: the API refuses
    both columns (PeripheralIn / PeripheralPatch exclude them) and this is what
    makes that refusal mean something rather than being a formality.
    """
    await db.execute(
        update(Peripheral).where(Peripheral.id == peripheral_id)
        .values(last_state=state, last_seen=at))
    await db.commit()


async def apply_state(db, topic: str, payload: str, at: datetime) -> None:
    """One inbound message. Never raises.

    A single bad message must not end the subscription for every device: the
    subscriber is elected, so if it dies nothing writes state until the lock
    expires and another worker picks it up.
    """
    try:
        text = (payload or "").strip()
        if not text:
            # MQTT publishes an empty payload to CLEAR a retained topic.
            # Storing it would put an empty string on the wall where a state
            # belongs.
            return
        peripheral_id = await _peripheral_id_for_topic(db, topic)
        if peripheral_id is None:
            return
        # last_state is String(40). A device publishing a JSON blob must not
        # make the write fail — the first 40 characters on the wall beats a
        # subscriber that dies on one chatty device.
        await _write_state(db, peripheral_id, text[:40], at)
    except Exception as exc:                                    # noqa: BLE001
        log.warning("bridge.state.apply_failed", topic=topic, error=str(exc))


async def publisher_loop() -> None:
    """Hold a PUBLISH-ONLY client, in EVERY worker. Not elected.

    Outbound commands are deliberately not elected — any worker may publish,
    several MQTT clients publishing is normal, and every v1 command is
    idempotent. But a worker with no client refuses every actuation with
    `offline`, so each one needs its own session.

    Found on the appliance: with only the elected worker connecting, three
    requests in four were refused and `bridge/health` reported offline from
    three workers while the broker showed a live subscriber. The unit tests
    could not see it — they never call start(), because the suite forbids
    opening a socket.

    SEPARATE FROM THE SUBSCRIBER'S CLIENT, on purpose. One session cannot be
    both "publish from any worker" and "subscribe in exactly one" as leadership
    moves; two sessions is what the split by direction actually means.
    """
    driver = get_bridge()._drivers.get("mqtt")
    while True:
        try:
            if driver is not None and await driver.health() != "not_configured":
                # Returns when the session drops; the sleep below is the
                # reconnect backoff.
                await driver.start(subscriptions=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                                # noqa: BLE001
            log.warning("bridge.publisher.failed", error=str(exc))
        await asyncio.sleep(10)


async def state_loop() -> None:
    """Hold the SUBSCRIPTION for as long as this worker is the leader.

    Started by main.py under run_as_leader("bridge_state", ...), and it builds
    its own driver instance rather than sharing the publisher's: the two have
    different lifetimes, and a session that must move with leadership cannot be
    the same one every actuation publishes through.
    """
    from ...db import AsyncSessionLocal
    from .mqtt import MqttDriver

    driver = MqttDriver(
        url=settings.mqtt_url, username=settings.mqtt_username or None,
        password=settings.mqtt_password or None,
        tls_verify=settings.mqtt_tls_verify,
        topic_prefix=settings.mqtt_topic_prefix)
    if await driver.health() == "not_configured":
        # Nothing to subscribe to. Sleep rather than spin, so a site that
        # configures a broker later picks it up without a restart.
        while True:
            await asyncio.sleep(60)
            if settings.mqtt_url:
                break

    async def _on_state(topic: str, payload: str, at: datetime) -> None:
        async with AsyncSessionLocal() as db:
            await apply_state(db, topic, payload, at)

    driver.subscribe_state(_on_state)

    async def _desired_topics() -> list:
        """The state topics this VMS owns, from the bindings table.

        Only ours. The customer's broker carries their whole smart home, and
        subscribing to all of it would be both rude and a lot of traffic to
        drop on the floor.
        """
        async with AsyncSessionLocal() as db:
            return list((await db.execute(
                select(PeripheralBinding.state_topic)
                .where(PeripheralBinding.state_topic.isnot(None))
            )).scalars().all())

    try:
        driver.set_state_topics(await _desired_topics())
    except Exception as exc:                                    # noqa: BLE001
        log.warning("bridge.state.topics_unavailable", error=str(exc))

    async def _reconcile_topics() -> None:
        """Re-assert the topic set for as long as the session lives.

        WITHOUT THIS THE SUBSCRIPTION IS FROZEN AT BOOT. The set used to be read
        once, before start(), and held until leadership moved or the process
        restarted — so a peripheral bound afterwards actuated correctly and
        reported `last_state` NULL forever. The wall said "unknown", which an
        operator reads as a broken bridge rather than as a subscriber that has
        never heard of their binding. Found against a live Mosquitto: the
        retained state arrived only after an API restart.

        A reconcile, in the shape the rest of this codebase uses: it heals
        drift rather than deciding anything, and `sync_topics` sends only the
        difference. Never raises — this task dying is how the subscription
        would silently stop healing again.
        """
        while True:
            await asyncio.sleep(settings.bridge_topic_sync_s)
            try:
                await driver.sync_topics(await _desired_topics())
            except asyncio.CancelledError:
                raise
            except Exception as exc:                            # noqa: BLE001
                log.warning("bridge.state.topic_sync_failed", error=str(exc))

    reconcile = asyncio.create_task(_reconcile_topics(), name="bridge_topic_sync")
    try:
        await driver.start(subscriptions=True)
    except asyncio.CancelledError:
        raise
    except Exception as exc:                                    # noqa: BLE001
        log.warning("bridge.state.loop_failed", error=str(exc))
    finally:
        reconcile.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reconcile
        await driver.stop()


async def verify_loop() -> None:
    """Re-resolve bindings, and retire readings that have gone quiet.

    Two jobs, one tick, because both answer "is what the wall says still true".
    HA entity ids move between monthly releases, so a binding that stops
    resolving has to become visibly degraded rather than silently doing
    nothing; and a reading past the staleness threshold has to go back to
    unknown.
    """
    from ...db import AsyncSessionLocal

    while True:
        try:
            async with AsyncSessionLocal() as db:
                now = datetime.now(timezone.utc)
                await get_bridge().verify_bindings(db, now=now)
                await _clear_stale_states(db, now=now)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                                # noqa: BLE001
            log.warning("bridge.verify.failed", error=str(exc))
        await asyncio.sleep(settings.bridge_verify_interval_s)


#: How often each worker re-reads the saved HA connection. An admin's save
#: reconfigures the worker that took the request at once; the other workers
#: catch up within this.
HA_CONFIG_SYNC_S = 10


async def apply_ha_config(db):
    """Load the HA connection (ui > env > none) into THIS worker's driver.

    Returns the HaConfig, so the router can report what it applied.
    """
    from . import ha_settings

    cfg = await ha_settings.load(db)
    driver = get_bridge()._drivers.get("homeassistant")
    if driver is not None:
        if await driver.reconfigure(cfg.url, cfg.token):
            log.info("bridge.ha.reconfigured", source=cfg.source,
                     url=cfg.url or None)
    return cfg


async def ha_config_loop() -> None:
    """Keep this worker's HA driver on the saved connection. NOT elected.

    Like publisher_loop, every worker needs it: any of the four may take the
    request that actuates, and a worker still holding the old token would
    refuse it with `unauthorized` while the panel says connected.
    """
    from ...db import AsyncSessionLocal

    while True:
        try:
            async with AsyncSessionLocal() as db:
                await apply_ha_config(db)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                                # noqa: BLE001
            log.warning("bridge.ha.config_sync_failed", error=str(exc))
        await asyncio.sleep(HA_CONFIG_SYNC_S)


async def _ha_bound_ids() -> dict:
    """entity_id -> peripheral id, for every homeassistant binding."""
    from ...db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(PeripheralBinding.address, PeripheralBinding.peripheral_id)
            .where(PeripheralBinding.driver == "homeassistant"))).all()
    return {address: pid for address, pid in rows}


async def write_ha_states(updates: list, at: datetime) -> None:
    """Write HA state updates to the wall. A None state is UNKNOWN: the
    reading is blanked and last_seen kept, as the unhealthy-verify path does —
    HA said it cannot reach the device, which is not hearing from it."""
    from ...db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        for peripheral_id, state in updates:
            if state is None:
                await db.execute(
                    update(Peripheral).where(Peripheral.id == peripheral_id)
                    .values(last_state=None))
                await db.commit()
            else:
                await _write_state(db, peripheral_id, state, at)


async def ha_state_loop() -> None:
    """HA's live state onto the wall. Started under run_as_leader("bridge_ha_state").

    Reads the connection itself (ha_settings) rather than this worker's driver:
    leadership can land on any worker, and the saved connection is the truth.
    """
    from ...db import AsyncSessionLocal
    from . import ha_settings, ha_state

    async def _load():
        async with AsyncSessionLocal() as db:
            return await ha_settings.load(db)

    await ha_state.run_forever(bound_ids=_ha_bound_ids, write=write_ha_states,
                               load_config=_load)


async def _clear_stale_states(db, *, now: datetime) -> int:
    """Blank last_state for devices that have gone quiet. Returns how many.

    The wall showing "closed" for a device that stopped reporting an hour ago
    is the specific dishonesty this whole feature was built to avoid.
    """
    cutoff = now - timedelta(seconds=settings.bridge_state_stale_s)
    result = await db.execute(
        update(Peripheral)
        .where(Peripheral.last_seen.isnot(None), Peripheral.last_seen < cutoff,
               Peripheral.last_state.isnot(None))
        .values(last_state=None))
    await db.commit()
    return int(result.rowcount or 0)


# ── Unbinding ────────────────────────────────────────────────────────────────

class FollowUpOwed(Exception):
    """A pulse's second half has not run yet, so the address must stay.

    Unbinding a siren three seconds into an eight-second pulse would strand
    the `off`: the sweep would claim it, find no binding, retry, and finally
    raise a critical alert for a device the operator had just told us to
    forget — with the siren still sounding. Refusing is the honest answer,
    and the wait is at most the pulse length.
    """

    def __init__(self, owed: int) -> None:
        super().__init__(f"{owed} follow-up(s) still owed")
        self.owed = owed


async def unbind(db, peripheral_id: int) -> bool:
    """Forget where a device lives. Returns False if it had no binding.

    Lives here and not in the router for the same reason _clear_stale_states
    does: nothing can report for this device now, so its last reading is
    history and the wall must say "unknown" — and blanking last_state is a
    write to the column with exactly one writer. `last_seen` is kept, as the
    stale sweep keeps it: when it was last heard from is still true.

    Rules that name this device keep their rows; the next fire is refused by
    base.resolve_target as `no_binding` and audited, which is what an operator
    who removed an address would expect to read.
    """
    owed = (await db.execute(
        select(func.count()).select_from(PendingAction)
        .where(PendingAction.peripheral_id == peripheral_id,
               PendingAction.state.in_(("pending", "claimed")))
    )).scalar()
    if owed:
        raise FollowUpOwed(int(owed))
    result = await db.execute(
        delete(PeripheralBinding)
        .where(PeripheralBinding.peripheral_id == peripheral_id))
    if not result.rowcount:
        return False
    await db.execute(
        update(Peripheral).where(Peripheral.id == peripheral_id)
        .values(last_state=None))
    await db.commit()
    return True
