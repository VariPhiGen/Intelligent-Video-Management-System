/**
 * cameras.ts — shared camera data hooks: registry list (auto-refreshing),
 * motion states (ride along on the same refresh, best-effort), and the
 * one-call uptime summary that backs the sparkline columns.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { apiFetch } from './api';
import type { Camera, MotionCamera, UptimeSummary } from './types';

export function zoneOf(c: Camera): string {
  return (c.metadata || {}).zone || 'Unzoned';
}

export interface CameraData {
  cameras: Camera[];
  motion: Record<string, MotionCamera['state']>;
  uptime: UptimeSummary;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
}

export function useCameras(intervalMs = 15000): CameraData {
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [motion, setMotion] = useState<Record<string, string>>({});
  const [uptime, setUptime] = useState<UptimeSummary>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);

  const refresh = useCallback(async () => {
    try {
      const cams: Camera[] = await apiFetch('/cameras?limit=1000');
      if (!alive.current) return;
      setCameras(cams);
      setError(null);
      // Best-effort riders — the table must render with these down.
      apiFetch('/motion/cameras')
        .then(m => {
          if (!alive.current) return;
          const map: Record<string, string> = {};
          (m.cameras || []).forEach((x: MotionCamera) => { map[x.name] = x.state; });
          setMotion(map);
        })
        .catch(() => {});
      apiFetch('/cameras/uptime/summary?hours=168')
        .then(r => { if (alive.current) setUptime(r.cameras || {}); })
        .catch(() => {});
    } catch (e: any) {
      if (alive.current) setError(e.message);
    } finally {
      if (alive.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    refresh();
    const t = setInterval(refresh, intervalMs);
    return () => { alive.current = false; clearInterval(t); };
  }, [refresh, intervalMs]);

  return { cameras, motion, uptime, loading, error, refresh };
}
