/**
 * ChangePasswordModal — the representative user-interaction test.
 *
 * Chosen because it is the smallest surface in the product that does the whole
 * loop: a user types, client-side rules reject some of what they typed, a real
 * API call goes out, and the answer comes back as either a toast or an inline
 * error. Everything a component test needs to be able to express is in one
 * ~120-line file.
 *
 * It also carries a genuine authorisation behaviour worth pinning: the dev
 * bypass and the internal service key have no Keycloak account behind them, so
 * there is no password to change. The component says so instead of offering a
 * form that would 400 on submit.
 *
 * QUERYING NOTE. The fields are queried by `autocomplete`, not by label text.
 * The markup is `<label>Current password</label>` followed by a sibling
 * `<input>` with no `htmlFor`/`id` pairing, so they are not programmatically
 * associated and `getByLabelText` cannot find them. That is an accessibility
 * defect in the component rather than in the test — a screen-reader user gets
 * three unlabelled password boxes — but fixing markup is outside what this
 * file should change, so it is recorded here and queried around.
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mockApi } from '@/test/api';

let currentMe: any = { kind: 'user', subject: 'ana', roles: ['viewer'], permissions: {} };

vi.mock('@/lib/auth', () => ({
  useAuth: () => ({ me: currentMe, isAdmin: false, ready: true, error: null, logout: () => {} }),
  authHeaders: async () => ({}),
}));

const toasts: string[] = [];
vi.mock('@/components/Toast', () => ({
  useToast: () => (msg: string) => { toasts.push(msg); },
}));

import { ChangePasswordModal } from './ChangePasswordModal';

beforeEach(() => {
  currentMe = { kind: 'user', subject: 'ana', roles: ['viewer'], permissions: {} };
  toasts.length = 0;
});

function open(onClose = vi.fn()) {
  const utils = render(<ChangePasswordModal open onClose={onClose} />);
  const field = (kind: 'current-password' | 'new-password', nth = 0) =>
    utils.container.querySelectorAll<HTMLInputElement>(
      `input[autocomplete="${kind}"]`,
    )[nth];
  return {
    ...utils,
    onClose,
    current: () => field('current-password'),
    next: () => field('new-password', 0),
    confirm: () => field('new-password', 1),
    submit: () => screen.getByRole('button', { name: 'Change password' }),
  };
}

describe('ChangePasswordModal — what it renders', () => {
  it('stays closed when it is not open', () => {
    const { container } = render(<ChangePasswordModal open={false} onClose={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('names the account being changed', () => {
    open();
    expect(screen.getByText('ana')).toBeInTheDocument();
  });

  it('offers three password fields', () => {
    const ui = open();
    expect(ui.current()).toBeInTheDocument();
    expect(ui.next()).toBeInTheDocument();
    expect(ui.confirm()).toBeInTheDocument();
  });
});

describe('ChangePasswordModal — principals with no password', () => {
  it('tells a dev-bypass caller there is nothing to change', () => {
    currentMe = { kind: 'dev', subject: 'dev', roles: [], permissions: {} };
    open();
    expect(screen.getByText(/developer bypass/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Change password' })).not.toBeInTheDocument();
  });

  it('tells a service principal the same', () => {
    currentMe = { kind: 'service', subject: 'internal', roles: [], permissions: {} };
    open();
    expect(screen.getByText(/internal service key/)).toBeInTheDocument();
  });

  it('offers the form to a real user account', () => {
    open();
    expect(screen.getByRole('button', { name: 'Change password' })).toBeInTheDocument();
  });
});

describe('ChangePasswordModal — the rules, before anything is sent', () => {
  it('refuses an empty form without calling the API', async () => {
    const api = mockApi({});
    const ui = open();
    await userEvent.click(ui.submit());

    expect(await screen.findByText('Fill in every field')).toBeInTheDocument();
    expect(api.called('PUT /api/me/password')).toBe(false);
  });

  it('refuses a new password that is too short', async () => {
    const api = mockApi({});
    const ui = open();
    await userEvent.type(ui.current(), 'oldpassword');
    await userEvent.type(ui.next(), 'short');
    await userEvent.type(ui.confirm(), 'short');
    await userEvent.click(ui.submit());

    expect(await screen.findByText(/at least 8 characters/)).toBeInTheDocument();
    expect(api.called('PUT /api/me/password')).toBe(false);
  });

  it('refuses a mistyped confirmation', async () => {
    const api = mockApi({});
    const ui = open();
    await userEvent.type(ui.current(), 'oldpassword');
    await userEvent.type(ui.next(), 'a-good-new-password');
    await userEvent.type(ui.confirm(), 'a-good-new-passwordX');
    await userEvent.click(ui.submit());

    expect(await screen.findByText('The new passwords do not match')).toBeInTheDocument();
    expect(api.called('PUT /api/me/password')).toBe(false);
  });

  it('refuses a new password identical to the current one', async () => {
    const api = mockApi({});
    const ui = open();
    await userEvent.type(ui.current(), 'same-password');
    await userEvent.type(ui.next(), 'same-password');
    await userEvent.type(ui.confirm(), 'same-password');
    await userEvent.click(ui.submit());

    expect(await screen.findByText(/must differ from the current one/)).toBeInTheDocument();
    expect(api.called('PUT /api/me/password')).toBe(false);
  });
});

describe('ChangePasswordModal — talking to the API', () => {
  it('sends what the user typed and closes on success', async () => {
    const api = mockApi({ 'PUT /api/me/password': { status: 204 } });
    const ui = open();
    await userEvent.type(ui.current(), 'old-password');
    await userEvent.type(ui.next(), 'a-good-new-password');
    await userEvent.type(ui.confirm(), 'a-good-new-password');
    await userEvent.click(ui.submit());

    await waitFor(() => expect(ui.onClose).toHaveBeenCalled());
    expect(api.bodyOf('PUT /api/me/password')).toEqual({
      current_password: 'old-password',
      new_password: 'a-good-new-password',
    });
    expect(toasts).toContain('Password changed');
  });

  it('shows the backend refusal verbatim and stays open', async () => {
    // The API answers a wrong current password with a sentence meant for the
    // user. Replacing it with a generic message would hide the one thing they
    // need to know.
    mockApi({
      'PUT /api/me/password': {
        status: 403, body: { detail: 'Current password is incorrect' },
      },
    });
    const ui = open();
    await userEvent.type(ui.current(), 'wrong-password');
    await userEvent.type(ui.next(), 'a-good-new-password');
    await userEvent.type(ui.confirm(), 'a-good-new-password');
    await userEvent.click(ui.submit());

    expect(await screen.findByText('Current password is incorrect')).toBeInTheDocument();
    expect(ui.onClose).not.toHaveBeenCalled();
  });

  it('re-enables the button after a failure so the user can retry', async () => {
    // `setBusy(false)` lives only in the catch. Losing it strands the user on
    // a permanently disabled form with an error they cannot act on.
    mockApi({
      'PUT /api/me/password': { status: 403, body: { detail: 'Current password is incorrect' } },
    });
    const ui = open();
    await userEvent.type(ui.current(), 'wrong-password');
    await userEvent.type(ui.next(), 'a-good-new-password');
    await userEvent.type(ui.confirm(), 'a-good-new-password');
    await userEvent.click(ui.submit());

    await screen.findByText('Current password is incorrect');
    expect(ui.submit()).not.toBeDisabled();
  });

  it('does not leave a stale error on screen from the previous attempt', async () => {
    const api = mockApi({
      'PUT /api/me/password': { status: 403, body: { detail: 'Current password is incorrect' } },
    });
    const ui = open();
    await userEvent.type(ui.current(), 'wrong-password');
    await userEvent.type(ui.next(), 'a-good-new-password');
    await userEvent.type(ui.confirm(), 'a-good-new-password');
    await userEvent.click(ui.submit());
    await screen.findByText('Current password is incorrect');

    api.set('PUT /api/me/password', { status: 204 });
    await userEvent.click(ui.submit());

    await waitFor(() => expect(ui.onClose).toHaveBeenCalled());
    expect(screen.queryByText('Current password is incorrect')).not.toBeInTheDocument();
  });
});
