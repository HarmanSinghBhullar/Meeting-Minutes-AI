import { copyFileSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import react from '@vitejs/plugin-react';
import { defineConfig, type Plugin } from 'vite';

/**
 * An extension is several independent entry points, not one app: the service
 * worker, the offscreen recorder, the content script, and the popup all load in
 * different contexts with different rules.
 *
 * Two of those rules shape this config:
 *
 * - `manifest.json` must sit at the root of `dist`, and its paths must match the
 *   built filenames exactly. Chrome fails to load the extension if they drift,
 *   so `ENTRY_PATHS` below is the single place they are declared.
 *
 * - The content script is loaded as a **classic script**, not a module. If Rollup
 *   splits shared code out of it, the resulting `import` statement is a syntax
 *   error at load time and the content script silently never runs — taking speaker
 *   attribution *and* the record panel with it. There is no per-entry
 *   `inlineDynamicImports` in a multi-entry build, so the rule is upheld by
 *   discipline — the content script may import only content-local and type-only
 *   modules — and enforced by `assertContentSelfContained` below, which fails the
 *   build the moment that rule is broken rather than letting it crash in a live
 *   meeting. (That is exactly how it once shipped: an innocent shared import.)
 */
const ENTRY_PATHS: Record<string, string> = {
  'service-worker': 'src/background/service-worker.js',
  content: 'src/content/content.js',
};

/** Copies the manifest into dist, which Vite does not do for us. */
function copyManifest(): Plugin {
  return {
    name: 'copy-manifest',
    closeBundle() {
      copyFileSync(resolve(__dirname, 'manifest.json'), resolve(__dirname, 'dist/manifest.json'));
    },
  };
}

/**
 * Fail the build if the content script was split into shared chunks.
 *
 * A self-contained content script has no imports; the moment it shares a runtime
 * module with another entry point, Rollup hoists that module into `src/chunks/` and
 * leaves an `import … from '../chunks/…'` at the top of `content.js`. Chrome loads
 * the content script as a classic script and throws "Cannot use import statement
 * outside a module" on line 1 — the whole script, panel and attribution included,
 * never runs. That is a silent production failure; this makes it a loud build one.
 */
function assertContentSelfContained(): Plugin {
  return {
    name: 'assert-content-self-contained',
    closeBundle() {
      const file = resolve(__dirname, 'dist', ENTRY_PATHS.content);
      const code = readFileSync(file, 'utf8');
      if (code.includes('/chunks/')) {
        throw new Error(
          `${ENTRY_PATHS.content} references a shared chunk, so Chrome will load it as a ` +
            'classic script and throw "Cannot use import statement outside a module". The ' +
            'content script must import only content-local and type-only modules — move the ' +
            'offending shared import behind a message to the service worker instead ' +
            '(see GET_SETTINGS in src/content/index.ts).',
        );
      }
    },
  };
}

export default defineConfig({
  plugins: [react(), copyManifest(), assertContentSelfContained()],
  resolve: {
    alias: { '@': resolve(__dirname, 'src') },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        'service-worker': resolve(__dirname, 'src/background/service-worker.ts'),
        content: resolve(__dirname, 'src/content/index.ts'),
        offscreen: resolve(__dirname, 'src/offscreen/offscreen.html'),
        popup: resolve(__dirname, 'src/popup/index.html'),
        permission: resolve(__dirname, 'src/permission/index.html'),
        meeting: resolve(__dirname, 'src/meeting/index.html'),
      },
      output: {
        entryFileNames: (chunk) => ENTRY_PATHS[chunk.name] ?? 'src/[name]/[name].js',
        chunkFileNames: 'src/chunks/[name].js',
        assetFileNames: 'src/assets/[name].[ext]',
      },
    },
  },
});
