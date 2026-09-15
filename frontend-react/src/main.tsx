import React from 'react';
import ReactDOM from 'react-dom/client';
import { HashRouter, Navigate, Route, Routes } from 'react-router-dom';
import { AuthProvider } from '@/lib/auth';
import { ToastProvider } from '@/components/Toast';
import { Shell } from '@/layout/Shell';
import { CamerasPage } from '@/pages/cameras/CamerasPage';
import { WizardPage } from '@/pages/wizard/WizardPage';
import { ConfigPage } from '@/pages/config/ConfigPage';
import { LivePage } from '@/pages/live/LivePage';
import { PlaybackPage } from '@/pages/playback/PlaybackPage';
import { AdminPage } from '@/pages/admin/AdminPage';
import { AnalyticsOverviewPage } from '@/pages/analytics/AnalyticsOverviewPage';
import { PeripheralsPage } from '@/pages/peripherals/PeripheralsPage';
import { SmartSearchPage } from '@/pages/smartsearch/SmartSearchPage';
import { MapPage } from '@/pages/map/MapPage';
import { extensionRoutes } from '@/extensions';
import '@/styles/global.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <AuthProvider>
      <ToastProvider>
        <HashRouter>
          <Routes>
            <Route element={<Shell />}>
              {/* Land on Live — every default role has live view; Cameras is
                  Supervisor/Admin-gated so it's a poor universal home. */}
              <Route path="/" element={<Navigate to="/live" replace />} />
              <Route path="/cameras" element={<CamerasPage />} />
              <Route path="/cameras/add" element={<WizardPage />} />
              <Route path="/cameras/config" element={<ConfigPage />} />
              <Route path="/live" element={<LivePage />} />
              <Route path="/playback" element={<PlaybackPage />} />
              <Route path="/admin" element={<AdminPage />} />
              <Route path="/smartsearch" element={<SmartSearchPage />} />
              <Route path="/map" element={<MapPage />} />
              <Route path="/peripherals" element={<PeripheralsPage />} />
              <Route path="/ai" element={<AnalyticsOverviewPage />} />
              {/* Routes contributed by optional extensions, if any are present.
                  Before the catch-all, or they would never match. */}
              {extensionRoutes.map(r => (
                <Route key={r.path} path={r.path} element={r.element} />
              ))}
              <Route path="*" element={<Navigate to="/live" replace />} />
            </Route>
          </Routes>
        </HashRouter>
      </ToastProvider>
    </AuthProvider>
  </React.StrictMode>,
);
