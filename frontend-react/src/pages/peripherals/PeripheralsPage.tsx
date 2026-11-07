/**
 * PeripheralsPage — physical peripheral control, in three tabs: Status panel
 * (the device wall), Rule builder (VMS event → peripheral action), and Trigger
 * log (the alerts those rules raised). Deep-linkable via ?tab=, same pattern as
 * /admin and /ai.
 *
 * ALL THREE HONESTY CHIPS ARE GONE, and that is the point of this revision.
 * Rules persist to `alert_rules` and the engine evaluates them against every
 * detection; alerts are real rows an operator can acknowledge and resolve; and
 * the device wall reports state a bridge actually reported. The status chip was
 * kept in P1 with the note "drop it the day a bridge ships, not before" — P2 is
 * that day. The wall now reports its own bridge state in its header, which is
 * both more precise than a page-level badge and where an operator looks.
 *
 * The arm banner sits above all three tabs rather than on the rules tab alone.
 * A disarmed engine is a property of the appliance, and an operator looking at
 * the trigger log wondering why nothing is acting needs to see it there too.
 */
import { ViewTransition } from '@/components/ViewTransition';
import { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';

import { useAuth } from '@/lib/auth';
import { armBannerText, armState, type ArmState } from '@/lib/rules';
import { usePeripherals } from './peripheralsData';
import { StatusPanel } from './StatusPanel';
import { RuleBuilder } from './RuleBuilder';
import { TriggerLog } from './TriggerLog';
import { HomeAssistantPanel } from './HomeAssistantPanel';

const TABS = [
  ['status', 'Status panel'],
  ['rules', 'Rule builder'],
  ['log', 'Trigger log'],
  // Shown to whoever can do something there: connect (admin) or import
  // (peripheral_manage). An operator who can only watch the wall has no use
  // for it.
  ['ha', 'Home Assistant'],
] as const;
type TabKey = typeof TABS[number][0];

export function PeripheralsPage() {
  const [params, setParams] = useSearchParams();
  const { me, isAdmin } = useAuth();
  const canManage = isAdmin || me?.permissions?.peripheral_manage === true;
  const tabs = TABS.filter(([k]) => k !== 'ha' || canManage);
  const raw = params.get('tab');
  const tab: TabKey = raw === 'rules' ? 'rules' : raw === 'log' ? 'log'
    : raw === 'ha' && canManage ? 'ha' : 'status';
  const setTab = (k: TabKey) => {
    const next = new URLSearchParams(params);
    next.set('tab', k);
    setParams(next, { replace: true });
  };

  const data = usePeripherals();

  const [arm, setArm] = useState<ArmState | null>(null);
  const loadArm = useCallback(async () => {
    // A failed read is not a disarmed engine, and must not be rendered as one:
    // the banner stays silent rather than announcing an outage that may not
    // exist. The API's own `degraded` flag is the one that means "unknown".
    try { setArm(await armState()); } catch { setArm(null); }
  }, []);
  useEffect(() => { loadArm(); }, [loadArm]);

  const banner = arm ? armBannerText(arm) : '';

  return (
    <ViewTransition view={tab}>
      {banner && (
        <div className="panel ss-offline" style={{ marginBottom: 'var(--s3)' }}>
          <div className="panel-body">
            <b>{arm?.degraded ? 'Automation state unknown' : 'Automation disarmed'}</b>
            <div className="hint">{banner}</div>
          </div>
        </div>
      )}

      {/* Title lives in the topbar — tabs lead the page. */}
      <div className="tabrow">
        <div className="tabs page-tabs">
          {tabs.map(([k, label]) => (
            <button type="button" key={k} aria-pressed={tab === k} className={`tab${tab === k ? ' active' : ''}`} onClick={() => setTab(k)}>{label}</button>
          ))}
        </div>
        {/* NO CHIP. All three are gone now: rules persist and fire, the log
            holds real alerts, and the device wall reports state from the
            bridge. The wall says what its bridge is doing in its own header,
            where the fact belongs — a blanket page-level badge could only
            repeat it less precisely. */}
      </div>

      {tab === 'status' ? <StatusPanel data={data} onImport={canManage ? () => setTab('ha') : undefined} />
        : tab === 'rules' ? <RuleBuilder data={data} />
        : tab === 'ha' ? <HomeAssistantPanel onImported={() => { void data.reloadDevices(); }} />
        : <TriggerLog />}
    </ViewTransition>
  );
}
