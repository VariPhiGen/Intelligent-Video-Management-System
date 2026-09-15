/**
 * extensions — optional UI packages, discovered by presence.
 *
 * The SPA mirror of backend/extensions. Some pages belong to a commercially
 * licensed tier and ship from a different repository; the open-core build must
 * compile and run with those files simply absent from the tree.
 *
 * WHY A GLOB AND NOT A LIST
 * -------------------------
 * A static `import { EvidencePage } from '@/pages/evidence/EvidencePage'` makes
 * deleting that file a BUILD FAILURE, not a smaller build. Until 2026-08-28 that
 * is exactly what main.tsx, Shell.tsx, AdminPage.tsx and AuditTab.tsx did, so an
 * open-core build could be described but never actually produced.
 *
 * `import.meta.glob` is resolved by Vite at build time against whatever is on
 * disk. No directory, no match, empty registry, successful build. That is the
 * whole mechanism, and it is why the rule below can be enforced rather than
 * merely intended:
 *
 *     NOTHING IN src/ OUTSIDE THIS DIRECTORY MAY IMPORT AN EXTENSION.
 *
 * Core code asks this module what exists. It never names anything.
 */
import type { ComponentType, ReactElement } from 'react';

/** A nav entry, placed under an existing section of the rail. */
export interface ExtensionNavItem {
  to: string;
  glyph: string;
  label: string;
  /** Capability that reveals it. Absent = visible to everyone. */
  cap?: string;
  /**
   * Section header to appear under, e.g. 'Govern'. Appended to that group.
   *
   * NOT named `section`: Shell.tsx discriminates rail headers from links with
   * `'section' in item`, so a nav item carrying that key is rendered as a
   * header and then dropped by the empty-section filter — it vanishes silently.
   * Found by loading the page; every build and bundle check passed.
   */
  underSection: string;
}

export interface ExtensionRoute {
  path: string;
  element: ReactElement;
}

/**
 * A tab on the Administration page. `cap` does double duty: it gates the tab,
 * AND it grants its holder access to /admin at all — so a principal who is not
 * an administrator but holds this capability reaches Administration and sees
 * only this tab. That relationship used to be spelled out in core code by the
 * capability's name, in both Shell.tsx and AdminPage.tsx; expressing it here
 * means the core states the rule and the extension supplies the value.
 */
export interface ExtensionAdminTab {
  key: string;
  label: string;
  render: () => ReactElement;
  cap?: string;
  /**
   * Place the tab BEFORE the core tabs instead of after them. For tabs that
   * were core once and moved out — the row should read the same with the
   * extension present as it did before the move.
   */
  leading?: boolean;
  /** A primary action shown at the end of the tab row while this tab is open. */
  action?: () => ReactElement;
}

export interface ExtensionManifest {
  name: string;
  routes?: ExtensionRoute[];
  nav?: ExtensionNavItem[];
  adminTabs?: ExtensionAdminTab[];
  /**
   * Components rendered into named holes in core pages — the UI equivalent of
   * the backend's registration points. A core page renders
   * `<ExtensionSlot name="audit.toolbar" />` without knowing whether anything
   * will fill it.
   */
  slots?: Record<string, ComponentType<any>>;
}

const modules = import.meta.glob<{ default: ExtensionManifest }>(
  './*/index.tsx',
  { eager: true },
);

export const EXTENSIONS: ExtensionManifest[] = Object.values(modules)
  .map(m => m?.default)
  .filter((m): m is ExtensionManifest => Boolean(m));

export const extensionRoutes: ExtensionRoute[] =
  EXTENSIONS.flatMap(e => e.routes ?? []);

export const extensionNav: ExtensionNavItem[] =
  EXTENSIONS.flatMap(e => e.nav ?? []);

export const extensionAdminTabs: ExtensionAdminTab[] =
  EXTENSIONS.flatMap(e => e.adminTabs ?? []);

/**
 * Capabilities that, on their own, grant access to Administration — because an
 * extension contributed a tab gated by them. See ExtensionAdminTab.
 */
export const adminAccessCaps: string[] =
  extensionAdminTabs.map(t => t.cap).filter((c): c is string => Boolean(c));

/** Render whatever extensions have registered for a named slot; nothing if none. */
export function ExtensionSlot({ name, ...props }: { name: string } & Record<string, unknown>) {
  const filled = EXTENSIONS
    .map(e => e.slots?.[name])
    .filter((C): C is ComponentType<any> => Boolean(C));
  return (
    <>
      {filled.map((C, i) => <C key={i} {...props} />)}
    </>
  );
}
