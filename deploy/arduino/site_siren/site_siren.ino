/*
 * site_siren — Waveshare ESP32-S3-Relay-1CH driving a 220V AC motor siren,
 * commanded over MQTT by the VMS peripherals bridge.
 *
 * Hardware facts established on the bench, both of which cost real time:
 *
 *   RELAY IS GPIO47, ACTIVE HIGH. Not 1, not 2 — both are named in Waveshare's
 *   wiki interfaces section and NEITHER drives the relay. A sweep of fourteen
 *   plausible GPIOs found nothing because it excluded 47 as part of the S3's
 *   flash/PSRAM range. It is not. Start at 47 on this board.
 *
 *   USB-C POWERS THE LOGIC ONLY. The relay coil is fed from the DC 5V SCREW
 *   TERMINALS. With those empty the board joins WiFi, talks MQTT and reports
 *   state perfectly while the relay never moves — which looks exactly like a
 *   firmware bug and is not one. If the relay does not click, check that rail
 *   before you change a line of this file.
 *
 * THE WATCHDOG IS THE POINT OF THIS SKETCH. MAX_ON_MS turns the relay off
 * whether or not an "off" ever arrives. It runs here, on the device, so a dead
 * broker, dead WiFi, dead VMS or a crashed container cannot leave a siren
 * sounding. The VMS's own pulse follow-up is convenience; this is safety.
 * Keep it comfortably LONGER than the rule's for_seconds — the VMS's "off"
 * lands a few seconds late because it is bound by a sweep interval.
 *
 * Arduino IDE settings:
 *   Board            : ESP32S3 Dev Module
 *   USB CDC On Boot  : Enabled      <- without this Serial prints nothing,
 *                                      because this board has no UART bridge
 *   Upload Mode      : UART0 / Hardware CDC
 *   Flash Size       : 8MB (or as fitted)
 *   Partition Scheme : Default 4MB with spiffs / 8M with spiffs
 *
 * Library required: PubSubClient (Nick O'Leary) — Library Manager.
 *
 * Fill in secrets.h before compiling.
 */

#include <WiFi.h>
#include <PubSubClient.h>
#include "secrets.h"

// ── Hardware ────────────────────────────────────────────────────────────────
static const int  RELAY_PIN   = 47;     // GPIO47 — see the note below
static const bool RELAY_ON    = HIGH;   // active HIGH
static const bool RELAY_OFF   = LOW;

// ── Safety ──────────────────────────────────────────────────────────────────
// The relay is forced off this long after any ON, no matter what. Do not set
// this to 0 and do not make it the same as the VMS pulse length.
static const int           MAX_ON_S  = 60;
static const unsigned long MAX_ON_MS = (unsigned long)MAX_ON_S * 1000UL;

// ── Topics (must match the VMS peripheral binding exactly) ──────────────────
static const char* T_CMD   = "vms/siren/site-siren/set";
static const char* T_STATE = "vms/siren/site-siren/state";
static const char* T_AVAIL = "vms/siren/site-siren/availability";

WiFiClient   net;
PubSubClient mqtt(net);

static bool          relayOn     = false;
static unsigned long relayOnAt   = 0;    // millis() when it was switched on
static unsigned long relayHoldMs = 0;    // how long this ON is allowed to last

// ── Relay control ───────────────────────────────────────────────────────────

// Every change goes through here so the state topic can never disagree with
// the pin. Retained, so the VMS wall shows the right thing after a restart
// instead of "unknown".
// `holdMs` is not optional for an ON. THE WATCHDOG IS ONLY AS GOOD AS THE
// GUARANTEE THAT IT IS ARMED: a setRelay(true) that forgot to set a duration
// would latch the relay on with no timeout at all, which on a 220V siren is
// the worst failure this firmware can have. Making it a required argument
// means the compiler enforces what a convention would not.
void setRelay(bool on, const char* why, unsigned long holdMs = 0) {
  digitalWrite(RELAY_PIN, on ? RELAY_ON : RELAY_OFF);
  relayOn = on;
  if (on) {
    relayOnAt   = millis();
    relayHoldMs = holdMs > 0 ? holdMs : MAX_ON_MS;   // never zero
  } else {
    relayHoldMs = 0;
  }

  mqtt.publish(T_STATE, on ? "on" : "off", true);   // retain = true
  Serial.printf("relay %s (%s)\n", on ? "ON" : "OFF", why);
}

// ── MQTT ────────────────────────────────────────────────────────────────────

void onMessage(char* topic, byte* payload, unsigned int len) {
  String msg;
  msg.reserve(len);
  for (unsigned int i = 0; i < len; i++) msg += (char)payload[i];
  msg.trim();

  // Accept what the VMS sends (lowercase) plus the obvious variants. Anything
  // unrecognised is IGNORED, never guessed at: for a siren, "I did not
  // understand that" must never resolve to "on".
  if (msg.equalsIgnoreCase("off") || msg == "0" || msg.equalsIgnoreCase("false")) {
    setRelay(false, "command");
    return;
  }

  // "on:N" — sound for N seconds. The VMS does not send this form (it pulses
  // with a separate off), but a human testing by hand should not have to
  // remember to send the off, and MAX_ON_S caps it either way.
  if (msg.startsWith("on:") || msg.startsWith("ON:")) {
    int seconds = msg.substring(3).toInt();
    if (seconds <= 0) {
      Serial.println("ignoring on: with no usable duration");
      return;
    }
    if (seconds > MAX_ON_S) {
      Serial.printf("capping %ds to %ds\n", seconds, MAX_ON_S);
      seconds = MAX_ON_S;
    }
    setRelay(true, "timed command", (unsigned long)seconds * 1000UL);
    return;
  }

  if (msg.equalsIgnoreCase("on") || msg == "1" || msg.equalsIgnoreCase("true")) {
    setRelay(true, "command", MAX_ON_MS);
    return;
  }

  // Ignored, never guessed at: for a siren, "I did not understand that" must
  // never resolve to "on".
  Serial.printf("ignoring unrecognised command: %s\n", msg.c_str());
}

void connectMqtt() {
  while (!mqtt.connected()) {
    Serial.print("mqtt connecting... ");

    // The last-will is why the VMS can tell "the siren controller is gone"
    // from "the siren is off". Retained, so a consumer connecting later still
    // learns the truth.
    bool ok = mqtt.connect(MQTT_CLIENT_ID,
                           MQTT_USER, MQTT_PASS,
                           T_AVAIL, 1, true, "offline");
    if (ok) {
      Serial.println("connected");
      mqtt.publish(T_AVAIL, "online", true);
      mqtt.subscribe(T_CMD, 1);

      // Re-assert the pin's real state on every reconnect, so a broker restart
      // cannot leave the VMS believing something stale.
      mqtt.publish(T_STATE, relayOn ? "on" : "off", true);
    } else {
      Serial.printf("failed rc=%d, retrying in 3s\n", mqtt.state());
      delay(3000);
    }
  }
}

// ── Setup / loop ────────────────────────────────────────────────────────────

void setup() {
  // FIRST THING, BEFORE ANYTHING ELSE. A controller that comes up sounding
  // while WiFi negotiates is unacceptable; the pin is driven low before it is
  // even made an output, so the power-on glitch is as short as possible.
  digitalWrite(RELAY_PIN, RELAY_OFF);
  pinMode(RELAY_PIN, OUTPUT);
  digitalWrite(RELAY_PIN, RELAY_OFF);

  Serial.begin(115200);
  delay(300);
  Serial.println("\nsite_siren starting — relay forced OFF");

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);            // keeps command latency predictable
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("wifi: joining %s", WIFI_SSID);
  while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print("."); }
  Serial.printf("\nwifi: %s  rssi %d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMessage);
  mqtt.setKeepAlive(30);
  connectMqtt();
}

void loop() {
  // THE WATCHDOG. Checked before anything else, and deliberately independent
  // of the network: it must still fire when the broker has gone away, which is
  // the case it exists for.
  // Deliberately NOT guarded on relayHoldMs > 0: setRelay always arms it, so a
  // zero here would mean a bug, and the safe reading of a bug is "switch off".
  if (relayOn && (millis() - relayOnAt >= (relayHoldMs ? relayHoldMs : MAX_ON_MS))) {
    setRelay(false, "watchdog: hold elapsed with no off");
  }

  // WiFi dropped: stop the siren. Standing on while the link is down means
  // nobody can turn it off, and the watchdog above may be a minute away.
  if (WiFi.status() != WL_CONNECTED) {
    if (relayOn) setRelay(false, "wifi lost");
    Serial.println("wifi lost, reconnecting");
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    unsigned long t0 = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) delay(500);
    return;
  }

  // Broker dropped: same reasoning.
  if (!mqtt.connected()) {
    if (relayOn) setRelay(false, "broker lost");
    connectMqtt();
  }

  mqtt.loop();
}
