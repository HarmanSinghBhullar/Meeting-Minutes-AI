import { copyFileSync } from 'node:fs';
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
 *   error at load time and the content script silently never runs — taking
 *   speaker attribution with it. Hence `inlineDynamicImports` for that entry.
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

export default defineConfig({
  plugins: [react(), copyManifest()],
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
