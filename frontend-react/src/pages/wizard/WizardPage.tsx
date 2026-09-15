/**
 * WizardPage.tsx — the "Add cameras" onboarding wizard (route /cameras/add).
 * Five sequential steps (Discover → Credentials → Assign → Privacy mask →
 * Confirm) driving the real discovery machinery, ported from the legacy SPA's
 * page-discovery section (wzGo / wzGate / wzStepClick / wzReset).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiFetch } from '@/lib/api';
import { useAuth } from '@/lib/auth';
import { todayLocal } from '@/lib/format';
import { useCameras } from '@/lib/cameras';
import { useToast } from '@/components/Toast';
import type { Camera } from '@/lib/types';
import { CamerasTabs } from '@/pages/cameras/CamerasPage';
import { Stepper, type WizardStep } from './Stepper';
import { StepDiscover } from './StepDiscover';
import { StepCredentials } from './StepCredentials';
import { StepAssign } from './StepAssign';
import { StepMasks } from './StepMasks';
import type { MaskEditorHandle } from '@/pages/config/tabs/PrivacyTab';
import { StepConfirm } from './StepConfirm';
import { EMPTY_ASSIGN, useDiscovery, type AddedCam, type AssignState, type Method } from './useDiscovery';

export function WizardPage() {
  const toast = useToast();
  const { me } = useAuth();
  const canManage = !!me?.permissions?.camera_manage;
  const { cameras, refresh } = useCameras();
  const onError = useCallback((msg: string) => toast(msg, 'err'), [toast]);
  const disc = useDiscovery(onError);

  const [step, setStep] = useState(1);
  const [added, setAdded] = useState<AddedCam[]>([]);
  const [method, setMethod] = useState<Method>('onvif');
  const [cidr, setCidr] = useState('');
  // Install date defaults to today — you're adding the camera now. Still editable.
  const [assign, setAssign] = useState<AssignState>(() => ({ ...EMPTY_ASSIGN, installed: todayLocal() }));

  const onAdded = useCallback((name: string, id: string) => {
    setAdded(a => (a.some(x => x.id === id) ? a : [...a, { name, id }]));
  }, []);

  // CSV bulk import registers cameras directly and only knows their slugs;
  // resolve those to {name, id} from the registry so the later steps (masks,
  // confirm) can show them like any other camera added this session.
  const onDirectImport = useCallback(async (slugs: string[]) => {
    refresh();
    if (!slugs.length) return;
    try {
      const list = await apiFetch<Camera[]>('/cameras?limit=1000');
      const bySlug = new Map(list.map(c => [c.slug, c]));
      setAdded(a => {
        const have = new Set(a.map(x => x.id));
        const fresh = slugs
          .map(s => bySlug.get(s))
          .filter((c): c is Camera => !!c && !have.has(c.id))
          .map(c => ({ name: c.name, id: c.id }));
        return fresh.length ? [...a, ...fresh] : a;
      });
    } catch { /* cameras are still registered; they just won't list here */ }
  }, [refresh]);

  // Direct-add methods (manual RTSP, CSV) register the camera immediately —
  // there is no ONVIF credentials or relay-assignment step to run, so the flow
  // is Discover → Privacy mask → Confirm. ONVIF scan / IP-range keep all five.
  const directAdd = method === 'manual' || method === 'csv';
  const FLOW: WizardStep[] = directAdd
    ? [{ n: 1, label: 'Discover' }, { n: 4, label: 'Privacy mask' }, { n: 5, label: 'Confirm' }]
    : [{ n: 1, label: 'Discover' }, { n: 2, label: 'Credentials' }, { n: 3, label: 'Assign' },
       { n: 4, label: 'Privacy mask' }, { n: 5, label: 'Confirm' }];
  const flowIdx = Math.max(0, FLOW.findIndex(s => s.n === step));

  // Unsaved-mask state of the step-4 editor. Leaving that step — Continue,
  // Back, or a stepper click — auto-saves drawn masks: the step has its own
  // Save button, but navigation must never silently discard drawn masks
  // (users reasonably expect Continue to keep their work).
  const maskHandle = useRef<MaskEditorHandle | null>(null);

  const go = async (n: number) => {
    if (step === 4 && n !== 4) {
      const h = maskHandle.current;
      if (h && (h.dirty || h.pendingShape)) {
        if (!(await h.save())) return;   // save() surfaces its own error toast
      }
    }
    setStep(Math.max(1, Math.min(5, n)));
  };
  const goNext = () => go(FLOW[Math.min(flowIdx + 1, FLOW.length - 1)].n);
  const goPrev = () => go(FLOW[Math.max(flowIdx - 1, 0)].n);
  const reset = () => { setAdded([]); go(1); };

  // Zone suggestions from the existing fleet.
  const zones = useMemo(
    () => [...new Set(cameras.map(c => (c.metadata || {}).zone).filter(Boolean))] as string[],
    [cameras],
  );

  // Continue is GATED per step (legacy wzGate): each step must have produced
  // its output before the flow can advance.
  let gateOk = true, gateMsg = '';
  if (step === 1) {
    gateOk = disc.devices.length > 0 || added.length > 0;
    gateMsg = 'Run a scan — or add a camera manually — to continue';
  } else if (step === 2) {
    gateOk = added.length > 0
      || disc.devices.some(d => d.status === 'verified' || d.status === 'added');
    gateMsg = 'Verify at least one device (Test all, or per-row Creds) to continue';
  } else if (step === 3) {
    gateOk = added.length > 0;
    gateMsg = 'Add at least one camera to the relay to continue';
  }

  // Onboarding is gated on camera_manage server-side; a role without it that
  // deep-links here gets a clear notice, not a wizard that 403s on submit.
  if (me && !canManage) {
    return (
      <div className="fade">
        <CamerasTabs active="add" />
        <div className="emptystate" style={{ marginTop: 'var(--s5)' }}>
          <div className="glyph">🔒</div>
          <h4>Adding cameras needs the “Add &amp; manage cameras” permission</h4>
          <p>Ask an administrator to grant it to your role.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="fade">
      {/* Title lives in the topbar (like Live View) — tabs lead the page. */}
      <CamerasTabs active="add" />

      <div className="wz-wrap">
        <Stepper steps={FLOW} current={step} onStepClick={go} />

        <div className="wz-frame">
          {step === 1 && (
            <StepDiscover method={method} setMethod={setMethod} cidr={cidr} setCidr={setCidr}
              disc={disc} assign={assign} setAssign={setAssign} zones={zones}
              onAdded={onAdded} refreshCameras={refresh}
              onDirectImport={onDirectImport} onProceed={goNext} />
          )}
          {step === 2 && (
            <StepCredentials devices={disc.devices} loadDevices={disc.loadDevices} />
          )}
          {step === 3 && (
            <StepAssign devices={disc.devices} loadDevices={disc.loadDevices}
              assign={assign} setAssign={setAssign} zones={zones}
              onAdded={onAdded} refreshCameras={refresh} />
          )}
          {step === 4 && (
            <StepMasks added={added} cameras={cameras} refresh={refresh}
              expose={h => { maskHandle.current = h; }} />
          )}
          {step === 5 && (
            <StepConfirm added={added} cameras={cameras} assign={assign} onReset={reset} />
          )}
        </div>

        {/* Wizard footer */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 14, marginTop: 18 }}>
          <button className="btn-ghost" style={{ padding: '11px 20px', visibility: step === 1 ? 'hidden' : 'visible' }}
            onClick={goPrev}>
            ← Back
          </button>
          <span style={{ marginLeft: 'auto', color: 'var(--dim)', fontSize: 12 }}>{gateOk ? '' : gateMsg}</span>
          <button className="btn-primary" style={{ padding: '11px 24px', visibility: step === 5 ? 'hidden' : 'visible' }}
            disabled={!gateOk} onClick={goNext}>
            Continue →
          </button>
        </div>
      </div>
    </div>
  );
}
