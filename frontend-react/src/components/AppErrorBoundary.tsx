/**
 * AppErrorBoundary — never leave a screen white.
 *
 * React 18 unmounts the WHOLE root on an uncaught render error, leaving an empty <div id="root">: on the light theme
 * that's a blank white page with no way back except someone walking up to the screen. On an unattended video wall
 * (Android, kiosk) that's exactly the "white page after some run" report. This catches it, says so, and reloads the
 * page by itself after a few seconds — the session survives the reload (Keycloak SSO cookie; "Remember this
 * workstation" keeps it for days), so the wall comes back on its own.
 *
 * Reload storms are capped: more than 3 automatic reloads within 10 minutes stops auto-reloading and leaves a button,
 * so a page that crashes on load can't spin forever.
 */
import { Component, type ErrorInfo, type ReactNode } from 'react';

const KEY = 'vms-auto-reloads';
const WINDOW_MS = 10 * 60 * 1000;
const MAX_RELOADS = 3;
const DELAY_S = 8;

function recentReloads(): number[] {
  try {
    const now = Date.now();
    return (JSON.parse(sessionStorage.getItem(KEY) || '[]') as number[]).filter((t) => now - t < WINDOW_MS);
  } catch {
    return [];
  }
}

export function autoReload(reason: string): boolean {
  const recent = recentReloads();
  if (recent.length >= MAX_RELOADS) return false;
  try { sessionStorage.setItem(KEY, JSON.stringify([...recent, Date.now()])); } catch { /* storage off */ }
  console.warn(`[vms] reloading: ${reason}`);
  location.reload();
  return true;
}

interface State { error: Error | null; left: number; capped: boolean }

export class AppErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { error: null, left: DELAY_S, capped: false };
  private timer: number | undefined;

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('[vms] render error', error, info.componentStack);
    const capped = recentReloads().length >= MAX_RELOADS;
    this.setState({ capped });
    if (capped) return;
    this.timer = window.setInterval(() => {
      this.setState((s) => ({ left: s.left - 1 }));
      if (this.state.left <= 0) {
        window.clearInterval(this.timer);
        if (!autoReload(String(error?.message || error))) this.setState({ capped: true });
      }
    }, 1000);
  }

  componentWillUnmount() {
    window.clearInterval(this.timer);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div style={{ position: 'fixed', inset: 0, background: '#0b0f15', color: '#e2e8f0', display: 'flex',
                    alignItems: 'center', justifyContent: 'center', flexDirection: 'column', gap: 12,
                    fontFamily: 'system-ui, sans-serif', textAlign: 'center', padding: 24 }}>
        <div style={{ fontSize: 20, fontWeight: 600 }}>Something went wrong on this screen</div>
        <div style={{ color: '#94a3b8', maxWidth: 520, fontSize: 14 }}>{String(this.state.error.message || this.state.error)}</div>
        {this.state.capped
          ? <button onClick={() => location.reload()} style={{ padding: '8px 18px', borderRadius: 8, border: 0, background: '#f97316', color: '#fff', fontSize: 15 }}>Reload</button>
          : <div style={{ color: '#94a3b8' }}>Reloading in {Math.max(0, this.state.left)} s…</div>}
      </div>
    );
  }
}
