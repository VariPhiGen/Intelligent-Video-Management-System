/**
 * roles.ts — display names for the product's realm roles.
 *
 * Core, not an extension: the roles exist in every build (policy.py enforces
 * them from DEFAULT_POLICY even where nothing in-product edits them), and an
 * extension importing a label from a page module would close an import cycle
 * through `@/extensions`, which that page also imports.
 */
export const ROLE_LABELS: Record<string, string> = {
  admin: 'Administrator',
  supervisor: 'Supervisor',
  operator: 'Operator',
  viewer: 'Viewer',
  dpo: 'DPO',
};
