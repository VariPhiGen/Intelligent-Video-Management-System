/// <reference types="vite/client" />
// Declares asset imports (`import logo from './logo.svg'` → URL string) for SVG,
// PNG, JPG, etc. Swap the brand logo to any of these formats without extra typing.

// Build-time brand/licence injection. Declared here so `import.meta.env.VITE_*`
// is typed rather than `any`; the VALUES are resolved in vite.config.ts (see
// its `define` block), not read from a .env file at runtime.
interface ImportMetaEnv {
  readonly VITE_BRAND_NAME: string;
  readonly VITE_BRAND_SITE: string;
  readonly VITE_SOURCE_URL: string;
}
interface ImportMeta {
  readonly env: ImportMetaEnv;
}
