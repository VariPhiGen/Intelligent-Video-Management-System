import { expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AdminPage } from './AdminPage';
import { useAuth } from '@/lib/auth';

vi.mock('@/lib/auth', () => ({ useAuth: vi.fn() }));
vi.mock('@/extensions', () => ({ extensionAdminTabs: [] }));
vi.mock('./AuditTab', () => ({ AuditTab: () => <p>audit body</p> }));
vi.mock('./HealthTab', () => ({ HealthTab: () => null }));
vi.mock('./StorageTab', () => ({ StorageTab: () => null }));
vi.mock('./ActivityTypesTab', () => ({ ActivityTypesTab: () => null }));
vi.mock('./LabelClassesTab', () => ({ LabelClassesTab: () => null }));
vi.mock('./QaSinkTab', () => ({ QaSinkTab: () => null }));
vi.mock('./QueryInterpretationTab', () => ({ QueryInterpretationTab: () => <p>planner body</p> }));

const at = (url: string) => render(<MemoryRouter initialEntries={[url]}><AdminPage /></MemoryRouter>);

it('offers query interpretation as an admin tab, reachable by deep link', () => {
  vi.mocked(useAuth).mockReturnValue({ me: {}, isAdmin: true } as never);
  at('/admin?tab=query-interpretation');
  expect(screen.getByText('Query interpretation')).toHaveClass('active');
  expect(screen.getByText('planner body')).toBeInTheDocument();
});

it('never shows the tab to a non-admin, even by deep link', () => {
  vi.mocked(useAuth).mockReturnValue({ me: {}, isAdmin: false } as never);
  at('/admin?tab=query-interpretation');
  expect(screen.queryByText('Query interpretation')).toBeNull();
  expect(screen.queryByText('planner body')).toBeNull();
});
