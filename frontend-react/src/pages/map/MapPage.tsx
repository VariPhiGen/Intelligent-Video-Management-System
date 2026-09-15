/**
 * MapPage.tsx — the Map tab: a site plan with live camera dots, computed zone
 * boxes, pan/zoom, and a detail panel. Sitemap CRUD (upload/rename/delete) is
 * gated on camera_manage.
 *
 * This file is the orchestrator only: it owns the selection and edit state,
 * and the mutations that persist placement. The parts live next door —
 *
 *   usePanZoom.ts   the transform, its bounds, and pointer→plan coordinates
 *   useMapData.ts   sitemaps, plan image, motion events, camera snapshot
 *   MapToolbar.tsx  plan picker, layer toggle, manage actions
 *   MapMarkers.tsx  what's drawn inside the transformed layer
 *   MapOverlays.tsx chrome floating on the plan, outside the transform
 *   MapPanels.tsx   the right column — trays, detail panels, georeference card
 *   MapModals.tsx   upload / rename dialogs
 *
 * The `view` toggle carries all four options the design calls for: Cameras
 * (live dots), HA Devices (peripherals pinned to this plan), Heatmap (real
 * counts off `/motion/events`), and Geofences, a disabled chip until that
 * surface exists.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { apiFetch, apiUpload } from '@/lib/api';
import { useAuth } from '@/lib/auth';
import { useCameras } from '@/lib/cameras';
import { useToast } from '@/components/Toast';
import type { Camera, SitemapMeta } from '@/lib/types';

import { resolveDots, unplacedOn, zoneBoxes } from './mapHelpers';
import { usePeripherals } from '../peripherals/peripheralsData';
import {
  HaLayer, HeatmapLayer, Legend, HEATMAP_WINDOWS, eventsInWindow, type HaPin,
} from './MockLayers';
import { DRAG_SLOP_PX, type View } from './mapConstants';
import { usePanZoom } from './usePanZoom';
import { useCameraSnapshot, useMotionEvents, useSitemapImage, useSitemaps } from './useMapData';
import { CalibrationPins, CameraDots, ZoneBoxes, type DotDragHandlers } from './MapMarkers';
import { EditHint, HeatCaption, HeatEmpty, ZoomControls } from './MapOverlays';
import {
  CalibrateCard, CameraDetailPanel, CameraTray, HaDetailPanel, HaTray,
} from './MapPanels';
import { RenameModal, UploadModal } from './MapModals';
import { MapToolbar } from './MapToolbar';

export function MapPage() {
  const nav = useNavigate();
  const toast = useToast();
  const { me, isAdmin } = useAuth();
  const canManage = isAdmin || me?.permissions?.camera_manage === true;
  const { cameras, motion, refresh } = useCameras();

  const { sitemaps, sitemapErr, currentId, setCurrentId, currentMap, loadSitemaps } = useSitemaps();
  const { imgUrl, imgLoading, imgError } = useSitemapImage(currentId);
  const motionEvents = useMotionEvents(currentId);

  const [view, setView] = useState<View>('cameras');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedHaId, setSelectedHaId] = useState<string | null>(null);
  const [labels, setLabels] = useState(true);
  const [busy, setBusy] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  // Which span the heatmap counts. Persisted nowhere on purpose — it's a way of
  // looking, not a setting, and it resets with the page like the zoom does.
  const [heatWindow, setHeatWindow] = useState('24h');

  const snapUrl = useCameraSnapshot(selectedId);
  const { devices: peripheralDevices } = usePeripherals();

  const {
    imgRef, tf, panning, imgAspect, panMoved,
    onImgLoad, zoomBy, resetView, attachSurface,
    onMouseDown, onMouseMove, endDrag, pointToNorm, resetForNewMap,
  } = usePanZoom();

  // Selection is exclusive per view — switching views clears the other view's
  // selection instead of leaving a stale highlight/panel around.
  useEffect(() => {
    if (view === 'ha') setSelectedId(null);
    else setSelectedHaId(null);
  }, [view]);

  const selected = useMemo(
    () => (selectedId ? cameras.find(c => c.id === selectedId) || null : null),
    [cameras, selectedId],
  );
  const selectedHaDevice = useMemo(
    () => (selectedHaId ? peripheralDevices.find(d => d.id === selectedHaId) || null : null),
    [peripheralDevices, selectedHaId],
  );

  // ── Edit mode ──────────────────────────────────────────────────────────────
  // Drag-to-place/reposition plus the trays. Gated on canManage at render time;
  // the state itself is harmless either way.
  const [editMode, setEditMode] = useState(false);
  const [calibrating, setCalibrating] = useState(false);
  const [calPoints, setCalPoints] = useState<{ x: number; y: number; lat: string; lng: string }[]>([]);
  // Camera armed via the tray's "Place" button — the next click on the plan
  // (not on an existing dot) persists that camera's coordinates.
  const [placingId, setPlacingId] = useState<string | null>(null);
  const [placingHaId, setPlacingHaId] = useState<string | null>(null);
  // Live position of the dot under the pointer.
  const [dragPos, setDragPos] = useState<{ id: string; x: number; y: number } | null>(null);
  // Per-camera position set at pointerup so the dot doesn't flicker back to
  // stale data while the PUT and the camera refresh are in flight. Cleared by
  // the self-healing effect below, or immediately on failure (honest snap-back).
  const [optimistic, setOptimistic] = useState<Record<string, { x: number; y: number }>>({});
  const dragCamRef = useRef<Camera | null>(null);
  const dragOriginRef = useRef<{ x: number; y: number } | null>(null);
  const draggedRef = useRef(false);

  // ── HA device placement ────────────────────────────────────────────────────
  // Stored per sitemap (`sitemaps.ha_devices`, migration 029) and edited the
  // same way cameras are — arm from the tray, click the plan, drag, remove.
  const haPins: HaPin[] = useMemo(() => currentMap?.ha_devices ?? [], [currentMap]);
  const [haDragPos, setHaDragPos] = useState<HaPin | null>(null);
  const haDragRef = useRef<string | null>(null);
  const haDragOrigin = useRef<{ x: number; y: number } | null>(null);
  const haDragged = useRef(false);

  const haPinsShown: HaPin[] = useMemo(
    () => (haDragPos ? haPins.map(p => (p.id === haDragPos.id ? haDragPos : p)) : haPins),
    [haPins, haDragPos],
  );
  const haUnplaced = useMemo(() => {
    const placed = new Set(haPins.map(p => p.id));
    return peripheralDevices.filter(d => !placed.has(d.id));
  }, [peripheralDevices, haPins]);

  // Everything transient resets when the plan changes.
  useEffect(() => {
    resetForNewMap();
    setSelectedId(null);
    setSelectedHaId(null);
    setPlacingId(null);
    setPlacingHaId(null);
    setDragPos(null);
    setHaDragPos(null);
    dragCamRef.current = null;
    haDragRef.current = null;
    setOptimistic({});
    setCalibrating(false);
    setCalPoints([]);
  }, [currentId, resetForNewMap]);

  // ── Derived geometry ───────────────────────────────────────────────────────
  // A manual placement on this map wins; otherwise, if the map is calibrated, a
  // camera's GPS auto-places it. `auto` flags the GPS-derived dots so they
  // render distinctly and drop out of the unplaced tray.
  const dots = useMemo(
    () => currentId != null ? resolveDots(cameras, currentId, currentMap?.calibration) : [],
    [cameras, currentId, currentMap],
  );
  const autoIds = useMemo(() => new Set(dots.filter(d => d.auto).map(d => d.cam.id)), [dots]);
  const boxes = useMemo(() => zoneBoxes(dots), [dots]);

  const heatEvents = useMemo(
    () => eventsInWindow(motionEvents, HEATMAP_WINDOWS.find(w => w.key === heatWindow)?.hours ?? null),
    [motionEvents, heatWindow],
  );
  // Events the heatmap can actually draw: those from a camera placed here.
  const heatOnMap = useMemo(() => {
    const slugs = new Set(dots.map(d => d.cam.slug));
    return heatEvents.filter(e => slugs.has(e.camera)).length;
  }, [heatEvents, dots]);

  const unplaced = useMemo(
    // Assigned-but-uncoordinated cameras, minus any the calibration already
    // auto-placed by GPS — those don't need a manual "Place".
    () => currentId != null ? unplacedOn(cameras, currentId).filter(c => !autoIds.has(c.id)) : [],
    [cameras, currentId, autoIds],
  );
  const unassigned = useMemo(() => {
    const knownMapIds = new Set((sitemaps || []).map(s => s.id));
    return cameras.filter(c => {
      const sm = (c.metadata || {}).sitemap as { id?: number } | undefined;
      // No ref at all, or a ghost ref pointing at a map that no longer exists —
      // both count as unassigned so the camera stays reachable from the picker.
      return !sm || sm.id == null || !knownMapIds.has(sm.id);
    });
  }, [cameras, sitemaps]);

  // Self-heal: once the live `cameras` data reflects an optimistic placement
  // (within a small epsilon — floats round-trip through JSON), drop the
  // override so the dot is driven by real data again.
  useEffect(() => {
    const ids = Object.keys(optimistic);
    if (ids.length === 0) return;
    const EPS = 1e-6;
    let changed = false;
    const next = { ...optimistic };
    for (const id of ids) {
      const cam = cameras.find(c => c.id === id);
      const sm = cam?.metadata?.sitemap as { id?: number; x?: number; y?: number } | undefined;
      if (sm && typeof sm.x === 'number' && typeof sm.y === 'number'
        && Math.abs(sm.x - next[id].x) < EPS && Math.abs(sm.y - next[id].y) < EPS) {
        delete next[id];
        changed = true;
      }
    }
    if (changed) setOptimistic(next);
  }, [cameras, optimistic]);

  // ── Mutations ──────────────────────────────────────────────────────────────
  // Mirrors CamerasPage's bulkAssignZone update: PUT /cameras/{id} with a
  // `metadata` envelope that's a full merge over the camera's existing
  // metadata, never a replace. Returns whether the PUT succeeded so callers
  // holding an optimistic override can snap it back honestly on failure.
  const persistPlacement = useCallback(async (cam: Camera, x: number | null, y: number | null) => {
    if (!currentMap) return false;
    const sitemap = x == null || y == null ? { id: currentMap.id } : { id: currentMap.id, x, y };
    const metadata = { ...(cam.metadata || {}), sitemap };
    try {
      await apiFetch(`/cameras/${cam.id}`, { method: 'PUT', body: JSON.stringify({ metadata }) });
      refresh();
      // The picker's per-map count comes off /sitemaps, which `refresh()`
      // doesn't touch — without this it stayed at its page-load value, so a
      // camera you just placed still read "dining · 0 cameras" until reload.
      void loadSitemaps(currentMap.id);
      return true;
    } catch (e: any) {
      toast(e.message, 'err');
      return false;
    }
  }, [currentMap, refresh, loadSitemaps, toast]);

  async function removeFromMap(cam: Camera) {
    const metadata = { ...(cam.metadata || {}) };
    delete metadata.sitemap;
    try {
      await apiFetch(`/cameras/${cam.id}`, { method: 'PUT', body: JSON.stringify({ metadata }) });
      toast(`Removed "${cam.name}" from the map`, 'ok');
      setSelectedId(null);
      refresh();
      if (currentId != null) void loadSitemaps(currentId);
    } catch (e: any) {
      toast(e.message, 'err');
    }
  }

  async function persistGps(cam: Camera, value: string) {
    const metadata = { ...(cam.metadata || {}) };
    const v = value.trim();
    if (v) metadata.gps = v; else delete metadata.gps;
    try {
      await apiFetch(`/cameras/${cam.id}`, { method: 'PUT', body: JSON.stringify({ metadata }) });
      toast('GPS updated', 'ok');
      refresh();
    } catch (e: any) { toast(e.message, 'err'); }
  }

  /** Replace this map's whole HA placement list. Whole-list because that's what
   * the endpoint takes — the page always holds the full set, and a replace
   * can't leave the plan half-saved. */
  const persistHaPins = useCallback(async (next: HaPin[]) => {
    if (!currentMap) return false;
    try {
      await apiFetch(`/sitemaps/${currentMap.id}/ha-devices`, {
        method: 'PUT', body: JSON.stringify({ devices: next }),
      });
      await loadSitemaps(currentMap.id);
      return true;
    } catch (e: any) {
      toast(e.message, 'err');
      return false;
    }
  }, [currentMap, loadSitemaps, toast]);

  async function placeHaDevice(id: string, x: number, y: number) {
    const name = peripheralDevices.find(d => d.id === id)?.name ?? 'Device';
    const ok = await persistHaPins([...haPins.filter(p => p.id !== id), { id, x, y }]);
    if (ok) { toast(`Placed "${name}" on ${currentMap?.name ?? 'the map'}`, 'ok'); setSelectedHaId(id); }
  }

  async function removeHaDevice(id: string) {
    const name = peripheralDevices.find(d => d.id === id)?.name ?? 'Device';
    const ok = await persistHaPins(haPins.filter(p => p.id !== id));
    if (ok) { toast(`Removed "${name}" from the map`, 'ok'); setSelectedHaId(null); }
  }

  // ── Calibration ────────────────────────────────────────────────────────────
  function startCalibrating() {
    setView('cameras');
    setEditMode(false);
    setPlacingId(null);
    const existing = currentMap?.calibration || [];
    setCalPoints(existing.map(p => ({ x: p.x, y: p.y, lat: String(p.lat), lng: String(p.lng) })));
    setCalibrating(true);
  }

  async function saveCalibration() {
    if (!currentMap) return;
    if (calPoints.length !== 2 && calPoints.length !== 3) {
      toast('Mark exactly 2 or 3 control points', 'err'); return;
    }
    const points = calPoints.map(p => ({ x: p.x, y: p.y, lat: parseFloat(p.lat), lng: parseFloat(p.lng) }));
    const bad = points.some(p =>
      !Number.isFinite(p.lat) || !Number.isFinite(p.lng)
      || p.lat < -90 || p.lat > 90 || p.lng < -180 || p.lng > 180);
    if (bad) { toast('Each point needs a valid latitude and longitude', 'err'); return; }
    setBusy(true);
    try {
      await apiFetch(`/sitemaps/${currentMap.id}/calibration`, {
        method: 'PATCH', body: JSON.stringify({ points }),
      });
      toast('Map georeferenced — cameras with GPS now auto-place', 'ok');
      setCalibrating(false);
      await loadSitemaps(currentMap.id);
    } catch (e: any) { toast(e.message, 'err'); }
    finally { setBusy(false); }
  }

  async function clearCalibration() {
    if (!currentMap) return;
    setBusy(true);
    try {
      await apiFetch(`/sitemaps/${currentMap.id}/calibration`, {
        method: 'PATCH', body: JSON.stringify({ points: [] }),
      });
      toast('Calibration cleared', 'ok');
      setCalibrating(false);
      setCalPoints([]);
      await loadSitemaps(currentMap.id);
    } catch (e: any) { toast(e.message, 'err'); }
    finally { setBusy(false); }
  }

  // ── Sitemap CRUD ───────────────────────────────────────────────────────────
  async function handleUpload(name: string, file: File) {
    setBusy(true);
    try {
      const form = new FormData();
      form.append('file', file);
      form.append('name', name);
      const created = await apiUpload<SitemapMeta>('/sitemaps', form);
      toast(`Uploaded "${created.name}"`, 'ok');
      setUploadOpen(false);
      await loadSitemaps(created.id);
    } catch (e: any) {
      toast(e.message, 'err');
    } finally { setBusy(false); }
  }

  async function handleRename(name: string) {
    if (!currentMap) return;
    setBusy(true);
    try {
      await apiFetch<SitemapMeta>(`/sitemaps/${currentMap.id}`, {
        method: 'PATCH', body: JSON.stringify({ name }),
      });
      toast('Sitemap renamed', 'ok');
      setRenameOpen(false);
      await loadSitemaps(currentMap.id);
    } catch (e: any) {
      toast(e.message, 'err');
    } finally { setBusy(false); }
  }

  async function handleDelete() {
    if (!currentMap) return;
    const assigned = currentMap.cameras_assigned;
    if (!confirm(
      `Delete "${currentMap.name}"?` +
      (assigned > 0 ? ` ${assigned} camera${assigned === 1 ? '' : 's'} on it will be unassigned.` : ''),
    )) return;
    setBusy(true);
    try {
      const r = await apiFetch<{ deleted: number; cameras_unplaced: number }>(
        `/sitemaps/${currentMap.id}`, { method: 'DELETE' },
      );
      toast(`Deleted "${currentMap.name}" — ${r.cameras_unplaced} camera${r.cameras_unplaced === 1 ? '' : 's'} unplaced`, 'ok');
      await loadSitemaps();
    } catch (e: any) {
      toast(e.message, 'err');
    } finally { setBusy(false); }
  }

  // ── Plan interaction ───────────────────────────────────────────────────────
  // Click-to-place: only fires when a tray marker is armed (or a calibration
  // point is being marked), and only when the click didn't land on a marker
  // (those stopPropagation on their own click).
  function onSurfaceClick(e: React.MouseEvent) {
    // A click that ended a pan is not a placement.
    if (panMoved.current) { panMoved.current = false; return; }
    if (calibrating) {
      if (calPoints.length >= 3) { toast('Up to 3 control points', 'err'); return; }
      const p = pointToNorm(e.clientX, e.clientY, false);
      if (!p) { toast('Click inside the plan to mark a control point', 'err'); return; }
      setCalPoints(pts => [...pts, { x: p.x, y: p.y, lat: '', lng: '' }]);
      return;
    }
    if (placingHaId) {
      // A click off the plan is rejected and leaves the device armed, so the
      // next real click still places it.
      const p = pointToNorm(e.clientX, e.clientY, false);
      if (!p) { toast('Click inside the plan to place this device', 'err'); return; }
      const id = placingHaId;
      setPlacingHaId(null);
      void placeHaDevice(id, p.x, p.y);
      return;
    }
    if (!placingId) return;
    const p = pointToNorm(e.clientX, e.clientY, false);
    if (!p) { toast('Click inside the plan to place this camera', 'err'); return; }
    const cam = cameras.find(c => c.id === placingId);
    setPlacingId(null);
    if (cam) persistPlacement(cam, p.x, p.y);
  }

  const dotDrag: DotDragHandlers = {
    dragPos,
    optimistic,
    onPointerDown: (e, cam) => {
      if (!editMode) return;
      e.stopPropagation();
      (e.target as HTMLElement).setPointerCapture(e.pointerId);
      dragCamRef.current = cam;
      dragOriginRef.current = { x: e.clientX, y: e.clientY };
      draggedRef.current = false;
      setSelectedId(cam.id);
    },
    onPointerMove: (e, cam) => {
      if (!editMode || dragCamRef.current?.id !== cam.id) return;
      // Ignore the sub-pixel jitter a plain click produces — only past the slop
      // is this a drag worth persisting.
      const o = dragOriginRef.current;
      if (!draggedRef.current) {
        if (o && Math.hypot(e.clientX - o.x, e.clientY - o.y) < DRAG_SLOP_PX) return;
        draggedRef.current = true;
      }
      const p = pointToNorm(e.clientX, e.clientY);
      if (p) setDragPos({ id: cam.id, x: p.x, y: p.y });
    },
    onPointerUp: async (e, cam) => {
      if (!editMode || dragCamRef.current?.id !== cam.id) return;
      const camId = dragCamRef.current.id;
      const fallbackCam = dragCamRef.current;
      dragCamRef.current = null;
      dragOriginRef.current = null;
      const moved = draggedRef.current;
      draggedRef.current = false;
      const p = pointToNorm(e.clientX, e.clientY);
      setDragPos(null);
      // A click that never moved only selects (onClick already did that) —
      // persisting here would rewrite the dot to the cursor and drop its
      // auto-placed-from-GPS status.
      if (!moved || !p) return;
      setOptimistic(o => ({ ...o, [camId]: { x: p.x, y: p.y } }));
      // Look the camera up FRESH from live `cameras` — a 15s poll or concurrent
      // edit during a long drag shouldn't be clobbered by the pointerdown-time
      // snapshot. `fallbackCam` is only identity if it vanished mid-drag.
      const fresh = cameras.find(c => c.id === camId) || fallbackCam;
      if (!fresh) { setOptimistic(o => { const n = { ...o }; delete n[camId]; return n; }); return; }
      const ok = await persistPlacement(fresh, p.x, p.y);
      if (!ok) {
        // persistPlacement already toasted; honest snap-back instead of holding
        // a position that never saved.
        setOptimistic(o => { const n = { ...o }; delete n[camId]; return n; });
      }
    },
    onPointerAbort: cam => {
      if (dragCamRef.current?.id !== cam.id) return;
      dragCamRef.current = null;
      dragOriginRef.current = null;
      draggedRef.current = false;
      setDragPos(null);
    },
  };

  // The transformed layer every view shares: same box, same transform, so all
  // three agree on where a normalized coordinate lands.
  const layerStyle: React.CSSProperties = {
    // Absolute + inset:0 gives this layer a definite box equal to the surface —
    // which equals the plan. Left as a plain block it stretched to the column
    // width while the image inside it did not, so every dot drifted.
    position: 'absolute', inset: 0,
    transform: `translate(${tf.tx}px, ${tf.ty}px) scale(${tf.scale})`,
    transformOrigin: '0 0',
  };
  const planImg = (
    <img ref={imgRef} src={imgUrl ?? undefined} onLoad={onImgLoad}
      style={{ display: 'block', width: '100%', height: '100%' }} draggable={false} />
  );

  if (sitemaps === null) {
    return <div className="empty"><span className="spinner lg" /></div>;
  }

  if (sitemaps.length === 0) {
    return (
      <>
        <div className="emptystate">
          <div className="glyph">⬡</div>
          <h4>No site maps yet</h4>
          <p>{sitemapErr || (canManage
            ? 'Upload a site plan to get started — place cameras on it from the camera wizard.'
            : 'Upload a site plan to get started — an administrator can upload one.')}</p>
          {canManage && <button className="btn-primary btn-sm" onClick={() => setUploadOpen(true)}>＋ Upload site plan</button>}
        </div>
        {canManage && (
          <UploadModal open={uploadOpen} busy={busy} onClose={() => setUploadOpen(false)} onSubmit={handleUpload} />
        )}
      </>
    );
  }

  return (
    <div>
      <MapToolbar
        sitemaps={sitemaps}
        currentId={currentId}
        currentMap={currentMap}
        onSelectMap={setCurrentId}
        view={view}
        onView={setView}
        canManage={canManage}
        editMode={editMode}
        calibrating={calibrating}
        busy={busy}
        onToggleEdit={() => {
          setEditMode(m => !m); setPlacingId(null); setPlacingHaId(null); setCalibrating(false);
        }}
        onToggleCalibrate={() => calibrating ? setCalibrating(false) : startCalibrating()}
        onRename={() => setRenameOpen(true)}
        onDelete={handleDelete}
        onUpload={() => setUploadOpen(true)}
      />

      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 300px', gap: 'var(--s5)', alignItems: 'start' }}>
        {/* One grid cell: the plan and the key that explains it. */}
        <div style={{ minWidth: 0 }}>
          <div
            ref={attachSurface}
            onMouseDown={onMouseDown}
            onMouseMove={onMouseMove}
            onMouseUp={endDrag}
            onMouseLeave={endDrag}
            onClick={onSurfaceClick}
            onDoubleClick={resetView}
            style={{
              position: 'relative', overflow: 'hidden', width: '100%',
              // maxWidth pins the surface to what the height cap allows, so
              // `aspectRatio` is always honoured exactly and the surface box IS
              // the plan's box. That's what lets the layers below fill the
              // surface and have their 0–1 coordinates land on the right pixels.
              ...(imgAspect
                ? {
                    aspectRatio: String(imgAspect),
                    maxHeight: 'calc(100vh - 230px)',
                    maxWidth: `calc((100vh - 230px) * ${imgAspect})`,
                    margin: '0 auto',
                  }
                : { height: 'calc(100vh - 230px)', minHeight: 420 }),
              background: 'var(--surface2)', border: '1px solid var(--border)',
              borderRadius: 'var(--r-lg)',
              cursor: (placingId || calibrating) ? 'crosshair' : (panning ? 'grabbing' : 'grab'),
            }}>
            {imgLoading && (
              <div className="empty" style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                <span className="spinner lg" />
              </div>
            )}
            {imgError && !imgLoading && (
              <div className="empty" style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                Could not load this map's image.
              </div>
            )}

            {imgUrl && view === 'cameras' && (
              <div style={layerStyle}>
                {planImg}
                <ZoneBoxes boxes={boxes} />
                <CameraDots
                  dots={dots} motion={motion} scale={tf.scale} labels={labels}
                  selectedId={selectedId} onSelect={setSelectedId}
                  drag={dotDrag}
                />
                {calibrating && <CalibrationPins points={calPoints} scale={tf.scale} />}
              </div>
            )}

            {imgUrl && view === 'ha' && (
              <div style={layerStyle}>
                {planImg}
                <ZoneBoxes boxes={boxes} />
                {/* HA Devices shows peripherals INSTEAD of camera dots — matches
                    the toggle's semantics (exclusive layers, not additive). */}
                <HaLayer
                  devices={peripheralDevices}
                  pins={haPinsShown}
                  selectedId={selectedHaId}
                  onSelect={setSelectedHaId}
                  scale={tf.scale}
                  labels={labels}
                  editMode={editMode && canManage}
                  onPinPointerDown={(e, id) => {
                    if (!editMode || !canManage) return;
                    e.stopPropagation();
                    (e.target as HTMLElement).setPointerCapture(e.pointerId);
                    haDragRef.current = id;
                    haDragOrigin.current = { x: e.clientX, y: e.clientY };
                    haDragged.current = false;
                    setSelectedHaId(id);
                  }}
                  onPinPointerMove={(e, id) => {
                    if (haDragRef.current !== id) return;
                    // Same slop as the camera dots: below it this is a click
                    // that selects, not a drag that saves.
                    const o = haDragOrigin.current;
                    if (!haDragged.current) {
                      if (o && Math.hypot(e.clientX - o.x, e.clientY - o.y) < DRAG_SLOP_PX) return;
                      haDragged.current = true;
                    }
                    const p = pointToNorm(e.clientX, e.clientY);
                    if (p) setHaDragPos({ id, x: p.x, y: p.y });
                  }}
                  onPinPointerUp={(e, id) => {
                    if (haDragRef.current !== id) return;
                    haDragRef.current = null;
                    haDragOrigin.current = null;
                    const moved = haDragged.current;
                    haDragged.current = false;
                    const p = pointToNorm(e.clientX, e.clientY);
                    if (!moved || !p) { setHaDragPos(null); return; }
                    // Hold the dropped position until the save round-trips,
                    // then clear — persistHaPins reloads the real list.
                    setHaDragPos({ id, x: p.x, y: p.y });
                    void persistHaPins([...haPins.filter(q => q.id !== id), { id, x: p.x, y: p.y }])
                      .then(() => setHaDragPos(null));
                  }}
                />
              </div>
            )}

            {imgUrl && view === 'heatmap' && (
              <div style={layerStyle}>
                {planImg}
                <ZoneBoxes boxes={boxes} />
                {/* Blobs first (pointerEvents: none) so the dots painted after
                    them stay on top and clickable — same panning surface as
                    Cameras, but click-to-select only; no drag/edit here. */}
                <HeatmapLayer dots={dots} events={heatEvents} />
                <CameraDots
                  dots={dots} motion={motion} scale={tf.scale} labels={labels}
                  selectedId={selectedId} onSelect={setSelectedId}
                />
              </div>
            )}

            <ZoomControls
              view={view} labels={labels}
              onZoomIn={() => zoomBy(1.25)} onZoomOut={() => zoomBy(1 / 1.25)}
              onReset={resetView} onToggleLabels={() => setLabels(v => !v)}
            />
            {editMode && (view === 'cameras' || view === 'ha') && (
              <EditHint
                view={view}
                isPlacing={!!(placingId || placingHaId)}
                armedName={view === 'ha'
                  ? (peripheralDevices.find(d => d.id === placingHaId)?.name ?? null)
                  : (cameras.find(c => c.id === placingId)?.name ?? null)}
              />
            )}
            {view === 'heatmap' && (
              <HeatCaption heatWindow={heatWindow} onWindow={setHeatWindow} eventsOnMap={heatOnMap} />
            )}
            {view === 'heatmap' && imgUrl && heatOnMap === 0 && (
              <HeatEmpty dotCount={dots.length} totalEvents={motionEvents.length}
                eventsInWindow={heatEvents.length} />
            )}
          </div>
          <Legend />
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s5)' }}>
          {calibrating && currentMap && (
            <CalibrateCard
              points={calPoints}
              onChange={setCalPoints}
              onSave={saveCalibration}
              onClear={clearCalibration}
              onCancel={() => setCalibrating(false)}
              hasExisting={!!currentMap.calibration}
              busy={busy}
            />
          )}
          {editMode && currentMap && view === 'cameras' && (
            <CameraTray
              unplaced={unplaced}
              unassigned={unassigned}
              placingId={placingId}
              onArm={id => setPlacingId(cur => cur === id ? null : id)}
              onAdd={id => {
                const cam = cameras.find(c => c.id === id);
                if (cam) persistPlacement(cam, null, null);
              }}
            />
          )}
          {editMode && currentMap && view === 'ha' && (
            <HaTray
              placedCount={haPins.length}
              unplaced={haUnplaced}
              placingHaId={placingHaId}
              onArm={id => { setPlacingHaId(cur => cur === id ? null : id); setPlacingId(null); }}
            />
          )}

          {view === 'ha' ? (
            <HaDetailPanel
              device={selectedHaDevice}
              pins={haPins}
              canManage={canManage}
              editMode={editMode}
              onRemove={removeHaDevice}
            />
          ) : (
            <CameraDetailPanel
              camera={selected}
              snapUrl={snapUrl}
              motionState={selected ? motion[selected.slug] : undefined}
              autoPlaced={!!selected && autoIds.has(selected.id)}
              editMode={editMode}
              onSaveGps={persistGps}
              onCopyGps={text => navigator.clipboard?.writeText(text).then(() => toast('Copied', 'ok'))}
              onOpenLive={() => nav('/live')}
              onOpenPlayback={slug => nav(`/playback?cam=${slug}`)}
              onRemove={removeFromMap}
            />
          )}
        </div>
      </div>

      {canManage && (
        <UploadModal open={uploadOpen} busy={busy} onClose={() => setUploadOpen(false)} onSubmit={handleUpload} />
      )}
      {canManage && currentMap && (
        <RenameModal open={renameOpen} busy={busy} initialName={currentMap.name}
          onClose={() => setRenameOpen(false)} onSubmit={handleRename} />
      )}
    </div>
  );
}
