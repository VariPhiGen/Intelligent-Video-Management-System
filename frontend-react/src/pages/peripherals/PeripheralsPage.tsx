/**
 * PeripheralsPage — physical peripheral control, in three tabs: Status panel
 * (the device wall), Rule builder (VMS event → Home-Assistant action), and
 * Trigger log (the audit trail). Deep-linkable via ?tab=, same pattern as
 * /admin and /ai.
 *
 * The Status panel is real as of migration 029 — the device inventory is rows
 * in Postgres, added and edited here, and referenced by the Map tab's pins
 * through a foreign key. Rules and the trigger log are still demo: both need a
 * bridge and a rule engine that do not exist, and a rule that cannot fire is
 * not more real for being stored. The chip is per-tab so it says which of the
 * two you are looking at — one blanket "Demo data" badge would now be wrong on
 * the wall and right on the other two.
 */
import { useSearchParams } from 'react-router-dom';
import { usePeripherals } from './peripheralsData';
import { StatusPanel } from './StatusPanel';
import { RuleBuilder } from './RuleBuilder';
import { TriggerLog } from './TriggerLog';

const TABS = [
  ['status', 'Status panel'],
  ['rules', 'Rule builder'],
  ['log', 'Trigger log'],
] as const;
type TabKey = typeof TABS[number][0];

export function PeripheralsPage() {
  const [params, setParams] = useSearchParams();
  const raw = params.get('tab');
  const tab: TabKey = raw === 'rules' ? 'rules' : raw === 'log' ? 'log' : 'status';
  const setTab = (k: TabKey) => {
    const next = new URLSearchParams(params);
    next.set('tab', k);
    setParams(next, { replace: true });
  };

  const data = usePeripherals();

  return (
    <div className="fade">
      {/* Title lives in the topbar — tabs lead the page. The chip is the honesty
          marker: this surface is demo data until the HA bridge is real. */}
      <div className="tabrow">
        <div className="tabs page-tabs">
          {TABS.map(([k, label]) => (
            <div key={k} className={`tab${tab === k ? ' active' : ''}`} onClick={() => setTab(k)}>{label}</div>
          ))}
        </div>
        {/* One chip per tab, each saying what is actually true of that tab. A
            blanket "Demo data" badge is now wrong on all three: the inventory
            is real, the builder's inputs are real but its rules don't persist,
            and the log holds nothing at all rather than something invented. */}
        {tab === 'status' ? (
          <span className="badge badge-green" title="Devices are stored in the VMS database. Their live state is not: no Home Assistant bridge is configured, so every device reads UNKNOWN and none can be switched.">
            <span className="badge-dot" />Inventory live · no device state
          </span>
        ) : tab === 'rules' ? (
          <span className="badge badge-blue" title="Events, targets and devices are real. The rule engine is not: a saved rule lives in this browser session and never fires.">
            <span className="badge-dot" />Rules don’t persist or fire
          </span>
        ) : (
          <span className="badge badge-blue" title="Nothing can fire a peripheral yet, so nothing is recorded here">
            <span className="badge-dot" />Not recording yet
          </span>
        )}
      </div>

      {tab === 'status' ? <StatusPanel data={data} />
        : tab === 'rules' ? <RuleBuilder data={data} />
        : <TriggerLog data={data} />}
    </div>
  );
}
