import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { AppErrorBoundary, autoReload } from './AppErrorBoundary';

describe('AppErrorBoundary — never a white page, never a reload storm', () => {
  beforeEach(() => {
    sessionStorage.clear();
    Object.defineProperty(window, 'location', { value: { ...window.location, reload: vi.fn() }, writable: true });
  });

  it('reloads by itself at most 3 times in 10 minutes', () => {
    expect([autoReload('a'), autoReload('b'), autoReload('c'), autoReload('d')]).toEqual([true, true, true, false]);
    expect(window.location.reload).toHaveBeenCalledTimes(3);
  });

  it('shows a dark recovery screen instead of unmounting to blank', () => {
    const Boom = () => { throw new Error('player blew up'); };
    vi.spyOn(console, 'error').mockImplementation(() => {});
    render(<AppErrorBoundary><Boom /></AppErrorBoundary>);
    expect(screen.getByText('Something went wrong on this screen')).toBeTruthy();
    expect(screen.getByText('player blew up')).toBeTruthy();
    expect(screen.getByText(/Reloading in/)).toBeTruthy();
  });
});
