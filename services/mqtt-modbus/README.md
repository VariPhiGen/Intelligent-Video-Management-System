# mqtt-modbus — one coil, one topic, one process on the bus

The VMS speaks only MQTT. This is the translator between that and a Modbus RTU
relay board: it subscribes to one command topic, writes one coil, and
publishes the coil's state back. For a single siren it is the whole
integration; Home Assistant would be a second platform to administer for one
coil write. When the site grows a gate, a UPS and meters, swap this for HA —
the VMS side does not change, which is what the driver port was for.

```
VMS ──"on"──▶ broker ──▶ mqtt-modbus ──Modbus RTU──▶ relay board ──dry contacts──▶ siren
                          (write coil 0)                   │
                                                  siren's OWN 12/24 V supply
```

The relay's contacts are dry: they switch the siren's own supply. The siren is
not powered from the bus. You need the board (A/B/GND from an RS-485 adapter),
the siren's PSU, and the siren wired through **NO/COM** on the channel.

Typical cheap board: Modbus RTU, 9600 8N1, slave id 1, coil `0x0000` =
channel 1, function 05 (write single coil), `0xFF00` = on, `0x0000` = off.
That is what the defaults describe.

## Safety — read this before connecting a siren

A stuck-on siren is a real incident: neighbours, noise ordinances, and someone
pulling a fuse in a way that also kills the relay board.

1. **Wire the siren normally-open.** If the relay board loses power the siren
   is silent, not sounding.
2. **Put a hardware timer relay or watchdog in series.** The siren must
   self-silence if it stops hearing from this translator. Everything below is
   software and can be killed, unplugged or out of memory.
3. **`MODBUS_MAX_ON_S` (default 60)** — the translator writes OFF this long
   after any ON, whether or not an `off` ever arrives. This is the second line,
   in a different process, behind the VMS's own pulse follow-up
   (`pending_actions`, whose close half is committed *before* the open
   publishes). Keep it at or below the longest pulse any rule asks for. The
   VMS accepts `for_seconds` up to 600 because a gate may need that; a siren
   must not, and this cap is where that is enforced.
4. **OFF on start, on broker loss, on shutdown.** If this process cannot hear
   the VMS, the coil is written off.
5. **Test with the siren disconnected first** — a multimeter across NO/COM, or
   just listen for the relay click. Prove the chain before you make noise.

## Run it before a wire is connected: `--dry-run`

`MODBUS_DRY_RUN=1` (the default) opens no port and logs
`DRY RUN: would write slave 1 coil 0 = ON  (01 05 00 00 ff 00 8c 3a)`
instead — and publishes state exactly as it would for real, so the wall shows
the siren switching. The full chain can be run end to end:

```
# .env: COMPOSE_PROFILES=...,mqtt,modbus  MQTT_URL=mqtt://127.0.0.1:1883
#       MQTT_USERNAME=vms  MQTT_PASSWORD=<generated>  MODBUS_DRY_RUN=1
./vms up -d                          # api + broker + mqtt-modbus, dry run
```

Bind the device — `PUT /api/peripherals/site-siren/binding`:

```json
{ "driver": "mqtt",
  "address": "vms/siren/site-siren/set",
  "state_topic": "vms/siren/site-siren/state" }
```

Two things gate the first actuation:

- `verified_at` must be stamped or `resolve_target` refuses with
  `binding_unverified`. The verify tick runs every `BRIDGE_VERIFY_INTERVAL_S`
  (300 s by default): after saving the binding either wait one interval or
  restart the api.
- The payload is the bare command string (`on`) unless the binding sets
  `config.payload_template`. The translator accepts `on`/`off`, `1`/`0`,
  `true`/`false`, and a JSON object carrying `"command"`; anything else is
  logged and ignored — a relay must never guess.

Rule action — use the pulse, not a bare `on`:

```json
{ "type": "peripheral", "peripheral": "site-siren",
  "command": "on", "for_seconds": 20, "then": "off" }
```

Then fire the rule (or press the control on the device wall) and watch:

```
docker logs -f vms_mqtt_modbus
  DRY RUN: would write slave 1 coil 0 = ON  (01 05 00 00 ff 00 8c 3a)
  ... 20 s later, from the VMS's follow-up:
  DRY RUN: would write slave 1 coil 0 = OFF (01 05 00 00 00 00 cd ca)
```

`peripherals.last_state` follows the state topic, so the wall shows `on` then
`off`, and `audit_log → alerts → analytics_events → camera` resolves as it
does for any actuation.

## Going live

1. Plug in the RS-485 adapter and confirm the node: `ls -l /dev/ttyUSB0`
   (root:dialout, 660). If it is a different path, set `MODBUS_PORT`.
2. `MODBUS_DRY_RUN=0` in `.env`.
3. `./vms up -d` — the wrapper sees the modbus profile and an existing
   `MODBUS_PORT`, and layers `docker-compose.modbus.yml`, which maps the
   device in. By hand:
   `docker compose -f docker-compose.yml -f docker-compose.modbus.yml up -d mqtt-modbus`.
4. The first log line is the startup OFF. If the board does not echo the
   frame the translator exits and restarts rather than serve a bus it cannot
   trust; check A/B polarity, baud and slave id.
5. Siren still disconnected: press the control, hear the click, watch the
   state topic. Then connect the siren.

Every write is verified by the board's echo (function 05 responds with the
request). A short, different or missing reply is a `BusError`: the coil may
or may not have moved, and no state is published for it — the wall stays on
its last known value rather than claiming one.

## Settings

All from the environment, each overridable by a flag (`python translator.py --help`):

| variable | default | |
|---|---|---|
| `MQTT_URL` / `MQTT_USERNAME` / `MQTT_PASSWORD` / `MQTT_TLS_VERIFY` | — | the same broker the VMS bridge uses |
| `MODBUS_DRY_RUN` | `1` | log writes; open no port |
| `MODBUS_PORT` | `/dev/ttyUSB0` | serial device |
| `MODBUS_BAUD` | `9600` | 8N1 always |
| `MODBUS_SLAVE_ID` | `1` | 1–247 (0 is broadcast: no echo, no proof) |
| `MODBUS_COIL` | `0` | 0-based coil address |
| `MODBUS_MAX_ON_S` | `60` | the translator's own OFF timer; `0` disables — do not |
| `MODBUS_COMMAND_TOPIC` | `vms/siren/site-siren/set` | = the binding's `address` |
| `MODBUS_STATE_TOPIC` | `vms/siren/site-siren/state` | = the binding's `state_topic` |
| `MODBUS_RECONNECT_S` | `5` | broker reconnect backoff |

Health: no HTTP. The container touches `/tmp/mqtt-modbus.heartbeat` while a
broker session is live (on connect, on every message, every 30 s between);
the compose healthcheck fails once it is two minutes old, so a translator
sitting in reconnect backoff shows unhealthy. Measured: unhealthy 3 min after
the broker stopped, healthy 30 s after it returned.

## The broker

The VMS ships its own: `COMPOSE_PROFILES=...,mqtt` starts `vms_mosquitto`,
bound to `127.0.0.1:${MQTT_PORT}`, password-only (`MQTT_USERNAME` /
`MQTT_PASSWORD`, rendered into the broker at start), with persistence so the
retained state survives a restart. `mqtt` + `modbus` is the siren set. A site
whose Home Assistant already runs a broker uses that instead and runs neither.

## Tests

```
cd services/mqtt-modbus && python -m pytest tests -q
```

A fake serial port answers the way a relay board does (function 05 echoes the
request). No port and no broker are opened; pyserial and aiomqtt are imported
lazily so the suite runs wherever pytest does. What is pinned: the wire
frames against the textbook CRC vector, that a short or different echo is a
refusal with no state, that unknown payloads are ignored rather than guessed,
the watchdog, its cancellation by a timely `off`, and that a second `on`
restarts it rather than stacking. A stub `aiomqtt` covers the session: OFF
published retained before the subscription exists, a refused startup OFF
serves nothing, a bus refusal mid-session claims no state, and broker loss
silences and reconnects.
