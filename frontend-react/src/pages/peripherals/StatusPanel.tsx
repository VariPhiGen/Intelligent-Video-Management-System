/**
 * StatusPanel — the peripherals wall: every device in the inventory as a tile,
 * grouped by category.
 *
 * The inventory is real (migration 029) and since P2 so is device STATE: tiles
 * come from /api/peripherals, whose `last_state` has exactly one writer — the
 * elected bridge subscriber. The API itself still refuses that column, which is
 * what keeps this wall honest by construction rather than by convention.
 *
 * THREE THINGS THIS SCREEN MUST NOT DO, each of which it used to:
 *   · claim a bridge is "offline" when none was ever configured — different
 *     facts, and the second is a supported deployment;
 *   · show a reading from half an hour ago as though it were current;
 *   · disable a control without saying which precondition is missing.
 * All three live in src/lib/bridge.ts so they are testable and shared with the
 * map's device popover.
 */
import { LoadingSkeleton } from '@/components/LoadingSkeleton';
import { useState } from 'react';
import { Modal } from '@/components/Modal';
import { useAuth } from '@/lib/auth';
import { bridgeLabel, bridgeTone, canControl, sendCommand } from '@/lib/bridge';
import { healthOf } from '@/lib/deviceGateway';

import { BindingModal } from './BindingModal';
import { useToast } from '@/components/Toast';
import {
  CATEGORY_ORDER, haDomain, toneColor, usePeripherals,
  type Device, type DeviceInput,
} from './peripheralsData';

function DeviceTile({ d, canManage, onEdit, control, onBind }: {
  d: Device; canManage: boolean; onEdit: (d: Device) => void;
  control: { pair: Override[]; allowed: { ok: boolean; reason?: string };
             busy: boolean; onPress: (command: string) => void } | null;
  onBind?: (d: Device) => void;
}) {
  const col = toneColor(d.tone);
  return (
    <div className="stat pk-tile" style={{ cursor: canManage ? 'pointer' : 'default', position: 'relative' }}
      title={canManage ? `Edit ${d.name}` : d.name}
      onClick={() => canManage && onEdit(d)}>
      <div className="pk-tile-top">
        <span className="pk-ico" style={{ color: col, background: `color-mix(in srgb, ${col} 12%, transparent)` }}>{d.glyph}</span>
        <span className="pk-state" style={{ color: col }}>{d.state}</span>
      </div>
      <div className="pk-tile-name">{d.name}</div>
      <div className="pk-tile-loc">{d.fault || d.location}</div>
      {/* The address is what an operator wiring a device needs to read off the
          wall — the topic the bridge publishes to, and the one it listens on.
          It was only ever in a hover title before, and only for managers;
          the API hands it to every reader, so the tile shows it to every
          reader. Unbound devices say so rather than showing nothing. */}
      <div className="pk-tile-addr" title={d.binding
          ? [`${d.binding.driver}: ${d.binding.address}`,
             d.binding.state_topic ? `state: ${d.binding.state_topic}` : null]
              .filter(Boolean).join('\n')
          : 'No bridge binding yet'}>
        {d.binding
          ? <><span className="pk-tile-drv">{d.binding.driver}</span> {d.binding.address}</>
          : <span style={{ color: 'var(--dim)' }}>not bound</span>}
      </div>
      {(() => {
        // Device health, for bindings the bridge can actually ask (device
        // gateway). Others say plainly that only the address was checked.
        const h = healthOf(d.binding);
        if (!h) return null;
        return (
          <div className={`pk-health pk-health-${h.tone}`} title={h.detail ?? h.label}>
            <span className="pk-health-dot" aria-hidden="true" />
            <span className="pk-health-label">{h.label}</span>
            {h.detail && <span className="pk-health-detail">{h.detail}</span>}
          </div>
        );
      })()}
      {d.placements > 0 && (
        <div style={{ fontSize: 10.5, color: 'var(--dim)', marginTop: 4 }}>
          on {d.placements} plan{d.placements === 1 ? '' : 's'}
        </div>
      )}
      {/* A device nobody has bound is the normal state of a fresh peripheral,
          not a fault — so this is an invitation to finish the job rather than
          an error. Without it the tile says "not verified" and offers nothing
          that changes that, which reads as a broken product. */}
      {canManage && onBind && (
        <button className="btn-subtle btn-sm" style={{ width: '100%', marginTop: 8 }}
                title={d.binding
                  ? `${d.binding.driver} · ${d.binding.address}`
                  : 'Tell the bridge where this device lives'}
                onClick={e => { e.stopPropagation(); onBind(d); }}>
          {!d.binding ? '⚙ Set address'
            : d.bindingVerified ? '⚙ Address' : '⚠ Address not verified'}
        </button>
      )}
      {control && (
        // MANUAL OVERRIDE: both halves, always. One button that guessed the
        // next command from the state offered only "Open" for a relay that
        // reports on/off. The half matching the current state is marked, and
        // either may be pressed — re-sending is how an operator forces it.
        // The TITLE carries the refusal; a disabled button with no reason is
        // how an operator concludes the product is broken.
        <div className="pk-override" onClick={e => e.stopPropagation()}>
          {control.pair.map(o => (
            <button key={o.command}
                    className={'btn btn-sm' + (o.active ? ' pk-override-on' : '')}
                    disabled={!control.allowed.ok || control.busy}
                    aria-pressed={o.active}
                    title={control.allowed.ok ? `${o.label} ${d.name}` : control.allowed.reason}
                    onClick={() => control.onPress(o.command)}>
              {control.busy ? '…' : o.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** The one button a category gets, and the command behind it.
 *
 *  Deliberately ONE action per tile rather than a menu: the wall is for
 *  glancing at, and a dropdown of commands beside a live camera feed is an
 *  invitation to press the wrong one. The full vocabulary is the rule
 *  builder's business.
 */
export interface Override { label: string; command: string; active: boolean }

/** The two manual-override commands a tile offers, and which matches now.
 *  A relay channel behind the device gateway reports `on`/`off` whatever is
 *  wired; open/unlocked/playing are what other drivers report for the same. */
export function overridesFor(d: Device): Override[] {
  const st = (d.lastState || '').toLowerCase();
  const known = d.state !== 'UNKNOWN' && !!st;
  const isOn = ['on', 'open', 'unlocked', 'playing'].includes(st);
  // Something you RUN (an HA scene, script or button) has no on/off to be in:
  // one button, never marked.
  const domain = haDomain(d);
  if ((domain && ['scene', 'script', 'button'].includes(domain))
      || (!domain && d.category === 'Automation')) {
    const run = domain === 'button' ? { label: 'Press', command: 'press' }
                                    : { label: 'Run', command: 'run' };
    return [{ ...run, active: false }];
  }
  const pair = domain === 'lock'
    ? [{ label: 'Unlock', command: 'unlock' }, { label: 'Lock', command: 'lock' }]
    : (domain === 'cover' || domain === 'valve'
       || ((!domain || domain === 'switch') && d.category === 'Access control'))
    ? [{ label: 'Open', command: 'open' }, { label: 'Close', command: 'close' }]
    : [{ label: 'Turn on', command: 'on' }, { label: 'Turn off', command: 'off' }];
  return pair.map((p, i) => ({ ...p, active: known && (i === 0 ? isOn : !isOn) }));
}

const BLANK: DeviceInput = { name: '', category: 'Lighting', location: '', vendor: '', external_id: '', notes: '' };

/** Add / edit form. Every field here is something a person can actually know —
 *  there is deliberately no "state" input, because typing a status would put a
 *  value on the wall that nothing verified. */
function DeviceModal({ open, initial, busy, onClose, onSave, onDelete }: {
  open: boolean;
  initial: Device | null;
  busy: boolean;
  onClose: () => void;
  onSave: (input: DeviceInput) => void;
  onDelete?: () => void;
}) {
  const [form, setForm] = useState<DeviceInput>(BLANK);
  const [seed, setSeed] = useState<string | null>(null);
  const key = initial ? initial.id : '__new__';
  if (open && seed !== key) {
    setSeed(key);
    setForm(initial
      ? {
          name: initial.name,
          category: initial.category,
          location: initial.location === '—' ? '' : initial.location,
          vendor: initial.vendor ?? '',
          external_id: initial.externalId ?? '',
          notes: initial.notes ?? '',
          enabled: initial.enabled,
        }
      : BLANK);
  }
  if (!open && seed !== null) setSeed(null);

  const set = (k: keyof DeviceInput) => (e: any) =>
    setForm(f => ({ ...f, [k]: e.target.type === 'checkbox' ? e.target.checked : e.target.value }));

  return (
    <Modal open={open} title={initial ? `Edit ${initial.name}` : 'Add peripheral'} onClose={onClose} width={520}
      footer={
        <div style={{ display: 'flex', gap: 8, justifyContent: 'space-between' }}>
          <div>
            {initial && onDelete && (
              <button className="btn-danger btn-sm" disabled={busy} onClick={onDelete}>
                Delete
              </button>
            )}
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <button className="btn-subtle btn-sm" onClick={onClose} disabled={busy}>Cancel</button>
            <button className="btn-primary btn-sm" disabled={busy || !form.name.trim()}
              onClick={() => onSave(form)}>
              {busy ? 'Saving…' : initial ? 'Save changes' : 'Add device'}
            </button>
          </div>
        </div>
      }>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <label style={{ display: 'block' }}>
          <div className="lbl">Name</div>
          <input value={form.name} onChange={set('name')} style={{ width: '100%' }}
            placeholder="Gate 3 maglock" autoFocus />
        </label>
        <label style={{ display: 'block' }}>
          <div className="lbl">Category</div>
          <select value={form.category} onChange={set('category')} style={{ width: '100%' }}>
            {CATEGORY_ORDER.map(c => <option key={c} value={c}>{c}</option>)}
          </select>
        </label>
        <label style={{ display: 'block' }}>
          <div className="lbl">Location</div>
          <input value={form.location ?? ''} onChange={set('location')} style={{ width: '100%' }}
            placeholder="Main entry" />
        </label>
        <label style={{ display: 'block' }}>
          <div className="lbl">Vendor</div>
          <input value={form.vendor ?? ''} onChange={set('vendor')} style={{ width: '100%' }}
            placeholder="Hikvision" />
        </label>
        <label style={{ display: 'block' }}>
          <div className="lbl">Bridge address</div>
          <input value={form.external_id ?? ''} onChange={set('external_id')} style={{ width: '100%' }}
            placeholder="lock.gate_3_maglock" />
          <div style={{ fontSize: 11, color: 'var(--dim)', marginTop: 4 }}>
            A human-readable note of where this device lives. The address the BRIDGE
            uses is the binding (driver + topic or entity id), set separately — this
            field is inventory, not configuration.
          </div>
        </label>
        <label style={{ display: 'block' }}>
          <div className="lbl">Notes</div>
          <textarea value={form.notes ?? ''} onChange={set('notes')} rows={2} style={{ width: '100%' }} />
        </label>
        {initial && (
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
            <input type="checkbox" checked={form.enabled !== false} onChange={set('enabled')} />
            <span style={{ fontSize: 12.5 }}>In service</span>
          </label>
        )}
      </div>
    </Modal>
  );
}

export function StatusPanel({ data, onImport }: {
  data: ReturnType<typeof usePeripherals>;
  /** Go to the Home Assistant tab. Absent for someone who cannot import. */
  onImport?: () => void;
}) {
  const toast = useToast();
  const { me, isAdmin } = useAuth();
  const canManage = isAdmin || me?.permissions?.peripheral_manage === true;
  const {
    devices, deviceStats: s, bridge, devicesLoading, devicesError,
    createDevice, updateDevice, deleteDevice, reloadDevices,
  } = data;
  const caps = me?.permissions;
  // `operating`, not `busy`: the component already has a `busy` for the add /
  // edit modal, and one flag for both would grey out a form while a barrier
  // moves.
  const [operating, setOperating] = useState<string | null>(null);

  /** Operate a device. Refusals come back as a 503 whose detail is the
   *  bridge's own stable token, which is more use to an operator than a
   *  generic failure. */
  async function operate(device: Device, command: string) {
    setOperating(device.id);
    try {
      await sendCommand(device.id, command);
      toast(`${device.name}: ${command} sent`);
      // The new state arrives with the next health check (every few seconds),
      // so look again shortly rather than only now.
      await reloadDevices();
      window.setTimeout(() => { void reloadDevices(); }, 2500);
    } catch (e: any) {
      toast(`${device.name}: ${e.message}`, 'err');
    }
    setOperating(null);
  }

  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<Device | null>(null);
  const [busy, setBusy] = useState(false);
  // Separate from `editing`: the binding is a different authority and a
  // different screen, and sharing one piece of state would let closing either
  // modal clear the other's subject mid-save.
  const [binding, setBinding] = useState<Device | null>(null);

  const openAdd = () => { setEditing(null); setModalOpen(true); };
  const openEdit = (d: Device) => { setEditing(d); setModalOpen(true); };

  async function save(input: DeviceInput) {
    setBusy(true);
    try {
      if (editing) {
        await updateDevice(editing.id, input);
        toast(`Saved "${input.name}"`, 'ok');
      } else {
        await createDevice(input);
        toast(`Added "${input.name}"`, 'ok');
      }
      setModalOpen(false);
    } catch (e: any) {
      toast(e.message, 'err');
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!editing) return;
    const pins = editing.placements;
    if (!confirm(
      `Delete "${editing.name}"?` +
      (pins > 0 ? ` It is pinned to ${pins} site plan${pins === 1 ? '' : 's'}; those pins go with it.` : ''),
    )) return;
    setBusy(true);
    try {
      await deleteDevice(editing.id);
      toast(`Deleted "${editing.name}"`, 'ok');
      setModalOpen(false);
    } catch (e: any) {
      toast(e.message, 'err');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fade">
      <div className="pk-head">
        <div className="pk-head-l">
          <b>{s.total} device{s.total === 1 ? '' : 's'}</b>
          <span style={{ color: 'var(--muted)' }}> across {s.categories} categor{s.categories === 1 ? 'y' : 'ies'}</span>
          {s.total > s.enabled && (
            <span style={{ color: 'var(--muted)' }}> · {s.total - s.enabled} out of service</span>
          )}
          {s.unplaced > 0 && (
            <span style={{ color: 'var(--muted)' }}> · {s.unplaced} not on any plan</span>
          )}
        </div>
        <div className="pk-head-r">
          {/* THREE STATES, NOT A BOOLEAN. "No bridge configured" is a
              supported deployment and a different fact from "offline"; the
              old boolean could only say the second, and said it to every site
              that was never meant to have one. */}
          <span className="dotlabel" title={
            bridge === 'not_configured'
              ? 'No bridge is configured on this appliance, so no device reports its state and none can be switched. This is a supported deployment, not a fault.'
              : bridge === 'offline'
              ? 'A bridge is configured but not connected. Device state is stale or absent and nothing can be switched.'
              : bridge === 'degraded'
              ? 'Some drivers are connected and some are not — devices on the unreachable one cannot be switched.'
              : 'Devices report their state and can be switched.'}>
            <span className="d" style={{ background: `var(--${bridgeTone(bridge)})` }} />
            {bridgeLabel(bridge)}
          </span>
          {onImport && (
            <button className="btn-subtle" onClick={onImport}
                    title="Bring in lights, relays, locks, speakers… already added to Home Assistant">
              ⌂ Import from Home Assistant
            </button>
          )}
          {canManage && (
            <button className="btn-primary" onClick={openAdd}>＋ Add device</button>
          )}
        </div>
      </div>

      {devicesError && (
        <div className="banner banner-warn" style={{ marginBottom: 'var(--s4)' }}>{devicesError}</div>
      )}

      {devicesLoading ? (
        <LoadingSkeleton label="Loading peripherals…" variant="cards" count={3} />
      ) : devices.length === 0 ? (
        <div className="emptystate">
          <div className="glyph">⚡</div>
          <h4>No peripherals yet</h4>
          <p>
            {canManage
              ? 'Add the lights, locks and sirens on this site. They become placeable on the Map tab straight away; their live state waits on the Home-Assistant bridge.'
              : 'An administrator can add the site’s lights, locks and sirens here.'}
          </p>
          {canManage && (
            <div style={{ display: 'flex', gap: 8, justifyContent: 'center' }}>
              {onImport && (
                <button className="btn-subtle btn-sm" onClick={onImport}>⌂ Import from Home Assistant</button>
              )}
              <button className="btn-primary btn-sm" onClick={openAdd}>＋ Add device</button>
            </div>
          )}
        </div>
      ) : (
        CATEGORY_ORDER.map(cat => {
          const inCat = devices.filter(d => d.category === cat);
          if (!inCat.length) return null;
          return (
            <div key={cat} style={{ marginBottom: 'var(--s5)' }}>
              <div className="pk-cat">{cat}</div>
              <div className="pk-grid">
                {inCat.map(d => (
                  <DeviceTile key={d.id} d={d} canManage={canManage} onEdit={openEdit}
                    onBind={setBinding}
                    control={{
                      pair: overridesFor(d),
                      allowed: canControl(caps, d, bridge),
                      busy: operating === d.id,
                      onPress: command => operate(d, command),
                    }} />
                ))}
              </div>
            </div>
          );
        })
      )}

      <BindingModal
        open={binding !== null}
        device={binding}
        onClose={() => setBinding(null)}
        onSaved={reloadDevices}
      />
      <DeviceModal
        open={modalOpen}
        initial={editing}
        busy={busy}
        onClose={() => setModalOpen(false)}
        onSave={save}
        onDelete={editing ? remove : undefined}
      />
    </div>
  );
}
