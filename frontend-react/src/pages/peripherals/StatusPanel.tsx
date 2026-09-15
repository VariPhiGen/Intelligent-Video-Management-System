/**
 * StatusPanel — the peripherals wall: every device in the inventory as a tile,
 * grouped by category.
 *
 * The inventory is real (migration 029): tiles come from /api/peripherals and
 * "Add device" writes a row. What is NOT real is device *state* — no
 * Home-Assistant bridge reports whether a light is on — so every tile reads
 * UNKNOWN in dim grey and the header says the bridge is offline. That is the
 * honest render: the previous version showed a confident ON / LOCKED from a
 * hardcoded array and claimed the bridge was connected.
 */
import { useState } from 'react';
import { Modal } from '@/components/Modal';
import { useAuth } from '@/lib/auth';
import { useToast } from '@/components/Toast';
import {
  CATEGORY_ORDER, toneColor, usePeripherals,
  type Device, type DeviceInput,
} from './peripheralsData';

function DeviceTile({ d, canManage, onEdit }: {
  d: Device; canManage: boolean; onEdit: (d: Device) => void;
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
      {d.placements > 0 && (
        <div style={{ fontSize: 10.5, color: 'var(--dim)', marginTop: 4 }}>
          on {d.placements} plan{d.placements === 1 ? '' : 's'}
        </div>
      )}
    </div>
  );
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
            The Home-Assistant entity id, MQTT topic or relay address this device will bind to.
            Recorded now so the integration has something to match on; nothing reads it yet.
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

export function StatusPanel({ data }: { data: ReturnType<typeof usePeripherals> }) {
  const toast = useToast();
  const { me, isAdmin } = useAuth();
  const canManage = isAdmin || me?.permissions?.peripheral_manage === true;
  const {
    devices, deviceStats: s, bridgeConnected, devicesLoading, devicesError,
    createDevice, updateDevice, deleteDevice,
  } = data;

  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<Device | null>(null);
  const [busy, setBusy] = useState(false);

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
          <span className="dotlabel" title="No Home-Assistant bridge is configured, so no device reports its state and none can be switched">
            <span className="d" style={{ background: bridgeConnected ? 'var(--green)' : 'var(--red)' }} />
            {bridgeConnected ? 'Home Assistant bridge connected' : 'Home Assistant bridge offline — no device state'}
          </span>
          {canManage && (
            <button className="btn-primary" onClick={openAdd}>＋ Add device</button>
          )}
        </div>
      </div>

      {devicesError && (
        <div className="banner banner-warn" style={{ marginBottom: 'var(--s4)' }}>{devicesError}</div>
      )}

      {devicesLoading ? (
        <div className="empty"><span className="spinner lg" /></div>
      ) : devices.length === 0 ? (
        <div className="emptystate">
          <div className="glyph">⚡</div>
          <h4>No peripherals yet</h4>
          <p>
            {canManage
              ? 'Add the lights, locks and sirens on this site. They become placeable on the Map tab straight away; their live state waits on the Home-Assistant bridge.'
              : 'An administrator can add the site’s lights, locks and sirens here.'}
          </p>
          {canManage && <button className="btn-primary btn-sm" onClick={openAdd}>＋ Add device</button>}
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
                  <DeviceTile key={d.id} d={d} canManage={canManage} onEdit={openEdit} />
                ))}
              </div>
            </div>
          );
        })
      )}

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
