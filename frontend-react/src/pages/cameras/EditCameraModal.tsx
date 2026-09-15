/** EditCameraModal.tsx — name / RTSP / recording / motion settings (diff-only PUT). */
import { useEffect, useState } from 'react';
import { apiFetch } from '@/lib/api';
import type { Camera } from '@/lib/types';
import { Modal } from '@/components/Modal';
import { useToast } from '@/components/Toast';

export function EditCameraModal({ camera, onClose, onSaved }: {
  camera: Camera | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [name, setName] = useState('');
  const [rtsp, setRtsp] = useState('');
  const [recording, setRecording] = useState(true);
  const [motion, setMotion] = useState(false);
  const [sens, setSens] = useState('medium');
  const [err, setErr] = useState('');

  useEffect(() => {
    if (!camera) return;
    setName(camera.name);
    setRtsp(camera.rtsp_url || '');
    setRecording(camera.recording !== false);
    setMotion(camera.motion_detection === true);
    setSens(camera.motion_sensitivity || 'medium');
    setErr('');
  }, [camera]);

  if (!camera) return null;

  async function save() {
    const c = camera!;
    const body: Record<string, unknown> = {};
    if (name && name !== c.name) body.name = name;
    if (rtsp && rtsp !== c.rtsp_url) body.rtsp_url = rtsp;
    if (recording !== (c.recording !== false)) body.recording = recording;
    if (motion !== (c.motion_detection === true)) body.motion_detection = motion;
    if (sens !== (c.motion_sensitivity || 'medium')) body.motion_sensitivity = sens;
    if (!Object.keys(body).length) { onClose(); return; }
    try {
      await apiFetch(`/cameras/${c.id}`, { method: 'PUT', body: JSON.stringify(body) });
      const notes: string[] = [];
      if (body.rtsp_url) notes.push('relay repointed');
      if ('recording' in body) notes.push(recording ? 'recording resumed' : 'recording stopped');
      if ('motion_detection' in body) notes.push(motion ? 'motion detection on' : 'motion detection off');
      toast('Camera updated' + (notes.length ? ' — ' + notes.join(', ') : ''));
      onSaved();
    } catch (e: any) { setErr(e.message); }
  }

  return (
    <Modal open title={`Edit — ${camera.name}`} onClose={onClose}>
      <div className="form-grid">
        <div className="form-group full">
          <label>Camera Name</label>
          <input value={name} onChange={e => setName(e.target.value)} />
        </div>
        <div className="form-group full">
          <label>RTSP URL (upstream camera)</label>
          <input value={rtsp} onChange={e => setRtsp(e.target.value)} placeholder="rtsp://admin:pass@192.168.1.10:554/stream1" />
          <span className="hint">Changing this repoints the relay — the local URL and recordings continue under the same slug.</span>
        </div>
        <div className="form-group">
          <label>Recording (NVR)</label>
          <select value={String(recording)} onChange={e => setRecording(e.target.value === 'true')}>
            <option value="true">On — record continuously</option>
            <option value="false">Off — live view only</option>
          </select>
        </div>
        <div className="form-group">
          <label>Slug (immutable)</label>
          <code style={{ color: 'var(--muted)', fontSize: 12 }}>{camera.slug}</code>
        </div>
        <div className="form-group">
          <label>Motion detection</label>
          <select value={String(motion)} onChange={e => setMotion(e.target.value === 'true')}>
            <option value="false">Off</option>
            <option value="true">On — analyze this camera</option>
          </select>
        </div>
        <div className="form-group">
          <label>Motion sensitivity</label>
          <select value={sens} onChange={e => setSens(e.target.value)}>
            <option value="low">Low — busy scenes, outdoor</option>
            <option value="medium">Medium (default)</option>
            <option value="high">High — quiet indoor scenes</option>
          </select>
        </div>
      </div>
      {err && <div style={{ color: 'var(--red)', marginTop: 12, fontSize: 13 }}>{err}</div>}
      <div className="form-actions">
        <button className="btn-primary" onClick={save}>Save</button>
        <button className="btn-ghost" onClick={onClose}>Cancel</button>
      </div>
    </Modal>
  );
}
