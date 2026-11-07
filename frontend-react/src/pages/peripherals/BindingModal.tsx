/**
 * BindingModal — where a device lives, as far as the bridge is concerned.
 *
 * SEPARATE FROM THE INVENTORY FORM ON PURPOSE. Migration 029 drew the line:
 * the peripherals row holds what a PERSON can know — what it is, where it is,
 * what it is called — and the binding is what a MACHINE needs. They are also
 * different authorities: editing the inventory is `peripheral_manage`, and the
 * binding is the thing that lets a rule move a barrier.
 *
 * WHY THIS SCREEN HAD TO EXIST. `putBinding` shipped in lib/bridge.ts and
 * nothing called it. An operator could add "Site siren" and then had no way to
 * say it answers on `vms/siren/site-siren/set` — the tile sat at "address has
 * not been verified" forever with no button that fixed it, which reads as a
 * broken product rather than as configuration nobody has done yet.
 *
 * THE TEST BUTTON IS NOT A NICETY. A topic typo is the single most likely
 * mistake here, and without a way to try one command the only feedback is a
 * device that silently never moves. Test sends a real command to real
 * hardware, so it says so, and it is not the primary action.
 */
import { useState } from 'react';

import { Modal } from '@/components/Modal';
import { useToast } from '@/components/Toast';
import {
  bindingPayload, defaultTopics, deleteBinding, putBinding, sendCommand,
  topicLoops, type Binding,
} from '@/lib/bridge';

import {
  DEFAULT_GATEWAY, buildGatewayUrl, healthOf, parseGatewayUrl, type GatewayTarget,
} from '@/lib/deviceGateway';

import type { Device } from './peripheralsData';

/** The device-gateway sub-form: what the operator knows about the device
 *  (board IP and channel, or speaker group), never a URL to type by hand. */
type GwMode = 'relay' | 'speaker' | 'url';
interface GwForm {
  mode: GwMode; base: string;
  host: string; rport: string; channel: string; slave: string;
  group: string; sport: string; probe: string; sound: string; iface: string;
}
const GW_BLANK: GwForm = {
  mode: 'relay', base: DEFAULT_GATEWAY,
  host: '', rport: '4196', channel: '1', slave: '1',
  group: '239.1.1.10', sport: '10000', probe: '', sound: '', iface: '',
};

function gwFromAddress(address: string): GwForm {
  const p = parseGatewayUrl(address);
  if (!p) return { ...GW_BLANK, mode: address ? 'url' : 'relay' };
  const t = p.target;
  return t.kind === 'relay'
    ? { ...GW_BLANK, mode: 'relay', base: p.base, host: t.host, rport: String(t.port),
        channel: String(t.channel), slave: String(t.slave) }
    : { ...GW_BLANK, mode: 'speaker', base: p.base, group: t.group, sport: String(t.port),
        probe: t.probe ?? '', sound: t.sound ?? '', iface: t.iface ?? '' };
}

/** The gateway URL for the sub-form, or null while a required field is blank. */
function gwUrl(g: GwForm): string | null {
  let t: GatewayTarget;
  if (g.mode === 'relay') {
    if (!g.host.trim() || !Number(g.channel)) return null;
    t = { kind: 'relay', host: g.host, port: Number(g.rport) || 4196,
          channel: Number(g.channel), slave: Number(g.slave) || 1 };
  } else if (g.mode === 'speaker') {
    if (!g.group.trim() || !Number(g.sport)) return null;
    t = { kind: 'speaker', group: g.group, port: Number(g.sport), probe: g.probe,
          sound: g.sound, iface: g.iface };
  } else {
    return null;
  }
  return buildGatewayUrl(g.base, t);
}

/** What each driver calls its address, and what a valid one looks like. A
 *  single "Address" placeholder would be wrong for two of the three. */
const DRIVERS = {
  mqtt: {
    label: 'MQTT',
    addressLabel: 'Command topic',
    addressHint: 'vms/siren/site-siren/set',
    blurb: 'The bridge publishes commands to this topic. Your device subscribes to it.',
  },
  homeassistant: {
    label: 'Home Assistant',
    addressLabel: 'Entity id',
    addressHint: 'switch.gate_3_barrier',
    blurb: 'The bridge calls the matching HA service for this entity.',
  },
  webhook: {
    label: 'Webhook',
    addressLabel: 'URL',
    addressHint: 'http://10.0.0.9/relay/1',
    blurb: 'The bridge POSTs a JSON body to this URL.',
  },
} as const;

type DriverKey = keyof typeof DRIVERS;

const BLANK = { driver: 'mqtt' as DriverKey, address: '', state_topic: '', secret: '' };

export function BindingModal({ open, device, onClose, onSaved }: {
  open: boolean;
  device: Device | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [form, setForm] = useState(BLANK);
  const [seed, setSeed] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState(false);
  // The topics are correct by construction, so they are folded away. Someone
  // integrating with an existing broker still needs to change them, which is
  // why the fields exist at all rather than being hidden outright.
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);
  const [gw, setGw] = useState<GwForm>(GW_BLANK);
  const setG = (k: keyof GwForm) => (e: any) => setGw(g => ({ ...g, [k]: e.target.value }));

  // Re-seed when the modal opens for a different device, the same pattern
  // DeviceModal uses — a useEffect here would fight the parent's open state.
  const key = device ? device.id : '__none__';
  if (open && seed !== key) {
    setSeed(key);
    const b = device?.binding;
    // A NEW binding arrives pre-filled with the topics the VMS will use. The
    // operator's job becomes "paste these into the device", not "invent a
    // convention and keep it in sync by hand".
    const gen = device ? defaultTopics(device.id) : { command: '', state: '' };
    setForm(b
      ? {
          driver: (b.driver as DriverKey) ?? 'mqtt',
          address: b.address ?? '',
          state_topic: b.state_topic ?? '',
          secret: '',            // never round-trips; blank means "unchanged"
        }
      : { ...BLANK, address: gen.command, state_topic: gen.state });
    setShowAdvanced(false);
    setGw(b?.driver === 'webhook' ? gwFromAddress(b.address ?? '') : GW_BLANK);
  }
  if (!open && seed !== null) setSeed(null);

  if (!device) return null;
  const meta = DRIVERS[form.driver];
  const b = device.binding;
  // Checked here so the operator is told while typing rather than after a
  // failed save. The API validates it too and remains the authority.
  const loops = form.driver === 'mqtt'
    && topicLoops(form.address, form.state_topic);
  // Through the device gateway the URL is built, and health is checked.
  const viaGateway = form.driver === 'webhook' && gw.mode !== 'url';
  const address = viaGateway ? (gwUrl(gw) ?? '') : form.address;
  const health = healthOf(b);

  const save = async () => {
    setBusy(true);
    try {
      await putBinding(device.id, bindingPayload({
        ...form, address,
        config: viaGateway ? { health: true }
          : form.driver === b?.driver ? (b?.config ?? {}) : {},
      }) as Binding & { secret?: string });
      toast('Binding saved. The bridge verifies it within a minute; the device '
          + 'cannot be operated until it does.');
      onSaved();
      onClose();
    } catch (e: any) {
      // The API returns 422 with the reason — `topic_loop` for a state topic
      // the command topic would match, which is the mistake worth naming.
      const msg = String(e?.message ?? e);
      toast(msg.includes('topic_loop')
        ? 'That state topic overlaps the command topic — the bridge would hear '
          + 'its own commands and record them as device state.'
        : msg, 'err');
    } finally {
      setBusy(false);
    }
  };

  const unbind = async () => {
    setBusy(true);
    try {
      await deleteBinding(device.id);
      toast('Binding removed. The wall will show this device as unknown.');
      onSaved();
      onClose();
    } catch (e: any) {
      // 409 while a pulse still owes this device its off.
      toast(String(e?.message ?? e), 'err');
    } finally {
      setBusy(false);
    }
  };

  /** Send one real command, and report exactly what came back. */
  const test = async () => {
    setTesting(true);
    try {
      const r = await sendCommand(device.id, 'off');
      toast(`Bridge accepted the command (${r.detail}).`);
    } catch (e: any) {
      toast(`Test failed: ${String(e?.message ?? e)}`, 'err');
    } finally {
      setTesting(false);
    }
  };

  return (
    <Modal open={open} title={`Binding — ${device.name}`} onClose={onClose} width={560}
      footer={
        <div style={{ display: 'flex', gap: 8, justifyContent: 'space-between' }}>
          <div style={{ display: 'flex', gap: 8 }}>
            {b && (
              <button className="btn-danger btn-sm" disabled={busy} onClick={unbind}>
                Unbind
              </button>
            )}
            {b && (
              // OFF, not ON. A test that could start a siren is a test nobody
              // runs twice; off is idempotent and proves the same path.
              <button className="btn-subtle btn-sm" disabled={busy || testing} onClick={test}
                title="Sends a real 'off' command to this device">
                {testing ? 'Testing…' : 'Send test command'}
              </button>
            )}
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <button className="btn-subtle btn-sm" onClick={onClose} disabled={busy}>Cancel</button>
            <button className="btn-primary btn-sm"
              disabled={busy || !address.trim() || loops}
              title={loops
                ? 'The state topic overlaps the command topic'
                : undefined}
              onClick={save}>
              {busy ? 'Saving…' : 'Save binding'}
            </button>
          </div>
        </div>
      }>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>

        {/* Verification state, in words. "Unverified" with no explanation is
            what made this screen necessary in the first place. */}
        {b && health && b.config?.health ? (
          <div className="panel-inset" style={{ padding: '8px 10px', fontSize: 12 }}>
            <span style={{ color: `var(--${health.tone === 'amber' ? 'yellow' : health.tone === 'dim' ? 'muted' : health.tone})` }}>
              {health.tone === 'green' ? '✓ ' : health.tone === 'red' ? '✕ ' : ''}{health.label}
              {health.detail ? ` — ${health.detail}` : ''}
            </span>
            <div style={{ color: 'var(--dim)', marginTop: 4 }}>
              The bridge asks the gateway about this device on every verify tick;
              asking never switches it.
            </div>
          </div>
        ) : b ? (
          <div className="panel-inset" style={{ padding: '8px 10px', fontSize: 12 }}>
            {b.verified_at ? (
              <span style={{ color: 'var(--green)' }}>
                ✓ Verified — the bridge reached this address
                {' '}({new Date(b.verified_at).toLocaleString()})
              </span>
            ) : (
              <span style={{ color: 'var(--amber)' }}>
                ⚠ Not verified yet{b.verify_error ? ` — ${b.verify_error}` : ''}.
                {' '}This device cannot be operated until the bridge confirms the address.
              </span>
            )}
          </div>
        ) : (
          <div className="panel-inset" style={{ padding: '8px 10px', fontSize: 12, color: 'var(--dim)' }}>
            No binding yet. This device is inventory only — it appears on the map
            and in reports, and nothing can switch it.
          </div>
        )}

        <label style={{ display: 'block' }}>
          <div className="lbl">Driver</div>
          <select value={form.driver} style={{ width: '100%' }}
            onChange={e => setForm(f => ({ ...f, driver: e.target.value as DriverKey }))}>
            {(Object.keys(DRIVERS) as DriverKey[]).map(k =>
              <option key={k} value={k}>{DRIVERS[k].label}</option>)}
          </select>
          <div style={{ fontSize: 11, color: 'var(--dim)', marginTop: 4 }}>{meta.blurb}</div>
        </label>

        {/* THE INVERSION. The VMS owns this namespace, so it states the
            topics and the operator copies them into the device. Asking someone
            to type a topic means asking them to guess a convention, keep it in
            sync with firmware by hand, and get a silent no-op on a typo. */}
        {form.driver === 'webhook' && (
          <label style={{ display: 'block' }}>
            <div className="lbl">Connects to</div>
            <select value={gw.mode} style={{ width: '100%' }}
              onChange={e => setGw(g => ({ ...g, mode: e.target.value as GwMode }))}>
              <option value="relay">Relay board channel — Modbus TCP, via the device gateway</option>
              <option value="speaker">Paging speaker — multicast audio, via the device gateway</option>
              <option value="url">Any other URL</option>
            </select>
          </label>
        )}

        {viaGateway && (
          <div className="panel-inset" style={{ padding: 10, display: 'flex', flexDirection: 'column', gap: 8 }}>
            {gw.mode === 'relay' ? (
              <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr 1fr 1fr', gap: 8 }}>
                <label><div className="lbl">Board IP</div>
                  <input value={gw.host} onChange={setG('host')} placeholder="192.168.1.200" style={{ width: '100%' }} autoFocus /></label>
                <label><div className="lbl">Port</div>
                  <input value={gw.rport} onChange={setG('rport')} inputMode="numeric" style={{ width: '100%' }} /></label>
                <label><div className="lbl">Channel</div>
                  <input value={gw.channel} onChange={setG('channel')} inputMode="numeric" style={{ width: '100%' }} /></label>
                <label><div className="lbl">Slave ID</div>
                  <input value={gw.slave} onChange={setG('slave')} inputMode="numeric" style={{ width: '100%' }} /></label>
              </div>
            ) : (
              <>
                <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 8 }}>
                  <label><div className="lbl">Multicast group</div>
                    <input value={gw.group} onChange={setG('group')} placeholder="239.1.1.10" style={{ width: '100%' }} autoFocus /></label>
                  <label><div className="lbl">Port</div>
                    <input value={gw.sport} onChange={setG('sport')} inputMode="numeric" style={{ width: '100%' }} /></label>
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 8 }}>
                  <label><div className="lbl">Speaker IP <span style={{ color: 'var(--dim)' }}>(health)</span></div>
                    <input value={gw.probe} onChange={setG('probe')} placeholder="192.168.1.102" style={{ width: '100%' }} /></label>
                  <label><div className="lbl">Sound</div>
                    <input value={gw.sound} onChange={setG('sound')} placeholder="tone" style={{ width: '100%' }} /></label>
                  <label><div className="lbl">Send from IP</div>
                    <input value={gw.iface} onChange={setG('iface')} placeholder="gateway default" style={{ width: '100%' }} /></label>
                </div>
                <div style={{ fontSize: 11, color: 'var(--dim)' }}>
                  Multicast cannot answer, so health checks the sound and, if you give the
                  speaker's own IP, that the speaker is on the network.
                </div>
              </>
            )}
            <label><div className="lbl">Gateway</div>
              <input value={gw.base} onChange={setG('base')} style={{ width: '100%' }} /></label>
            <div style={{ fontSize: 11, color: 'var(--dim)' }}>
              URL the bridge will use: <code>{address || '— fill in the fields above'}</code>
            </div>
          </div>
        )}

        {viaGateway ? null : form.driver === 'mqtt' && !showAdvanced ? (
          <div className="panel-inset" style={{ padding: 10 }}>
            <div style={{ fontSize: 12, marginBottom: 8 }}>
              Configure your device with these. The VMS publishes commands to the
              first and listens for the device's state on the second.
            </div>
            {([['Command topic', form.address, 'cmd'],
               ['State topic', form.state_topic, 'state']] as const).map(
              ([label, value, id]) => (
                <div key={id} style={{ marginBottom: 6 }}>
                  <div className="lbl" style={{ fontSize: 11 }}>{label}</div>
                  <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                    <code style={{ flex: 1, fontSize: 12, padding: '4px 6px',
                                   background: 'var(--bg2)', borderRadius: 4,
                                   overflow: 'hidden', textOverflow: 'ellipsis' }}>
                      {value || '—'}
                    </code>
                    <button className="btn-subtle btn-sm" type="button"
                      onClick={() => {
                        navigator.clipboard?.writeText(value || '');
                        setCopied(id);
                        window.setTimeout(() => setCopied(null), 1500);
                      }}>
                      {copied === id ? 'Copied' : 'Copy'}
                    </button>
                  </div>
                </div>
              ))}
            <button className="btn-subtle btn-sm" type="button" style={{ marginTop: 4 }}
              onClick={() => setShowAdvanced(true)}>
              Use different topics
            </button>
          </div>
        ) : (
          <>
            <label style={{ display: 'block' }}>
              <div className="lbl">{meta.addressLabel}</div>
              <input value={form.address} style={{ width: '100%' }} placeholder={meta.addressHint}
                onChange={e => setForm(f => ({ ...f, address: e.target.value }))} autoFocus />
            </label>

            {form.driver === 'mqtt' && (
              <label style={{ display: 'block' }}>
                <div className="lbl">State topic <span style={{ color: 'var(--dim)' }}>(optional)</span></div>
                <input value={form.state_topic} style={{ width: '100%' }}
                  placeholder="vms/site-siren/state"
                  onChange={e => setForm(f => ({ ...f, state_topic: e.target.value }))} />
                <div style={{ fontSize: 11, marginTop: 4,
                              color: loops ? 'var(--red)' : 'var(--dim)' }}>
                  {loops
                    ? 'This overlaps the command topic — the bridge would hear its own '
                      + 'commands and record them as device state.'
                    : 'Where the device reports back. Leave blank for a write-only '
                      + 'device; the wall will show its state as unknown, which is honest.'}
                </div>
              </label>
            )}
          </>
        )}

        <label style={{ display: 'block' }}>
          <div className="lbl">
            Secret <span style={{ color: 'var(--dim)' }}>(optional)</span>
          </div>
          <input type="password" value={form.secret} style={{ width: '100%' }}
            placeholder={b ? 'unchanged' : 'per-device token, if this driver needs one'}
            onChange={e => setForm(f => ({ ...f, secret: e.target.value }))} />
          <div style={{ fontSize: 11, color: 'var(--dim)', marginTop: 4 }}>
            Stored encrypted and never sent back to this page. Leave blank to keep
            the existing one.
          </div>
        </label>
      </div>
    </Modal>
  );
}
