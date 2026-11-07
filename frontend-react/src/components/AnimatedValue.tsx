import { motion, useReducedMotion } from 'motion/react';
import type { ReactNode } from 'react';

/** Show the authoritative value immediately; animate its emphasis, not a
 * sequence of invented intermediate operational readings. */
export function AnimatedValue({ value }: { value: ReactNode }) {
  const reduced = useReducedMotion();
  const key = typeof value === 'number' || typeof value === 'string' ? String(value) : 'content';
  return <motion.span key={key} initial={reduced ? false : { opacity: 0.55 }}
    animate={{ opacity: 1 }} transition={{ duration: 0.24 }}>{value}</motion.span>;
}
