import { useEffect, useState } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { MotionConfig } from 'motion/react';
import { ViewTransition } from './ViewTransition';
import { LoadingSkeleton } from './LoadingSkeleton';

it('keeps child state and DOM identity when the visual section changes', () => {
  const mounted = vi.fn();
  const unmounted = vi.fn();
  function PlayerForm() {
    const [value, setValue] = useState('');
    useEffect(() => { mounted(); return unmounted; }, []);
    return <input aria-label="Unsaved note" value={value} onChange={e => setValue(e.target.value)} />;
  }
  const page = (view: string) => <MotionConfig reducedMotion="always"><ViewTransition view={view}><PlayerForm /></ViewTransition></MotionConfig>;
  const { rerender } = render(page('first'));
  const input = screen.getByLabelText('Unsaved note');
  fireEvent.change(input, { target: { value: 'Keep this note' } });
  rerender(page('second'));
  expect(screen.getByLabelText('Unsaved note')).toBe(input);
  expect(input).toHaveValue('Keep this note');
  expect(mounted).toHaveBeenCalledTimes(1);
  expect(unmounted).not.toHaveBeenCalled();
});

it('announces one loading status while keeping skeleton shapes decorative', () => {
  const { container } = render(<LoadingSkeleton label="Loading peripherals…" variant="cards" count={3} />);
  expect(screen.getByRole('status', { name: 'Loading peripherals…' })).toBeInTheDocument();
  expect(container.querySelector('.loading-skeleton-shapes')).toHaveAttribute('aria-hidden', 'true');
});
