/**
 * AnalyticsOverviewPage — AI Analytics, in three tabs: AI Detections (the
 * person/vehicle detections the CLIP pipeline is producing — the operational
 * dashboard), Events (the AI activity events each camera's configured activities
 * raise), and Configuration (what each camera is set to watch — the CMM view). Deep-linkable via ?tab=, same pattern as /admin.
 */
import { useSearchParams } from 'react-router-dom';
import { AiDetectionsDashboard } from './AiDetectionsDashboard';
import { EventsTab } from './EventsTab';
import { ConfigurationTab } from './ConfigurationTab';

const TABS = [
  ['detections', 'AI Detections'],
  ['events', 'Events'],
  ['config', 'Configuration'],
] as const;
type TabKey = typeof TABS[number][0];

export function AnalyticsOverviewPage() {
  const [params, setParams] = useSearchParams();
  const raw = params.get('tab');
  const tab: TabKey = raw === 'events' ? 'events' : raw === 'config' ? 'config' : 'detections';
  const setTab = (k: TabKey) => {
    const next = new URLSearchParams(params);
    next.set('tab', k);
    setParams(next, { replace: true });
  };

  return (
    <div className="fade">
      {/* Title lives in the topbar (like Live View) — tabs lead the page. */}
      <div className="tabs page-tabs">
        {TABS.map(([k, label]) => (
          <div key={k} className={`tab${tab === k ? ' active' : ''}`} onClick={() => setTab(k)}>{label}</div>
        ))}
      </div>
      {tab === 'detections' ? (
        <AiDetectionsDashboard />
      ) : tab === 'events' ? (
        <EventsTab onOpenConfig={() => setTab('config')} />
      ) : (
        <ConfigurationTab />
      )}
    </div>
  );
}
