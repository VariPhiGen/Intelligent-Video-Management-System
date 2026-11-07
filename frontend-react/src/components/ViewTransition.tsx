import { useEffect, type ReactNode } from 'react';
import { motion, useAnimationControls, useReducedMotion } from 'motion/react';

/** A stable page surface. Changing sections animates opacity without adding a
 * key, delaying navigation, or remounting the current video/form subtree. */
export function ViewTransition({ view, children }: { view: string; children: ReactNode }) {
  const controls = useAnimationControls();
  const reduced = useReducedMotion();
  useEffect(() => {
    if (reduced) { controls.set({ opacity: 1 }); return; }
    void controls.start({ opacity: [0.82, 1], transition: { duration: 0.2, ease: [0.22, 1, 0.36, 1] } });
    return () => controls.stop();
  }, [view, reduced, controls]);
  return <motion.div className="motion-page" initial={false} animate={controls}>{children}</motion.div>;
}
