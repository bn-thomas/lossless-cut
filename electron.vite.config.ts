import { defineConfig } from 'electron-vite';
import react from '@vitejs/plugin-react';
import type { Plugin } from 'vite';

// Electron 38 + Node 22.21.1 + VS Code terminal workaround:
// VS Code sets ELECTRON_RUN_AS_NODE=1 (it's an Electron app itself), which causes lossless-cut's
// electron binary to run as plain Node.js instead of a proper Electron main process. This means
// `require('electron')` returns the npm package path string instead of the Electron API.
//
// Fix: Run `env -u ELECTRON_RUN_AS_NODE yarn dev` from the terminal (or open an external terminal).
//
// Additionally, Electron's ESM virtual 'electron' module provides exports on the CJS default object.
// We use dynamic import to avoid static link-time named-export checking, then unwrap the default.

const fixElectronImports = (): Plugin => ({
  name: 'fix-electron-imports',
  generateBundle(_opts, bundle) {
    for (const chunk of Object.values(bundle)) {
      if (chunk.type !== 'chunk') continue;
      if (!chunk.code.includes('"electron"') && !chunk.code.includes("'electron'")) continue;
      // Match: import electronDefault, { a as b, c } from "electron";
      chunk.code = chunk.code.replace(
        /^import\s+([\w$]+\s*,\s*)?\{([^}]+)\}\s+from\s+"electron";/m,
        (_match, defaultPart, namedPart) => {
          const defaultName = defaultPart ? defaultPart.trim().replace(/,$/, '').trim() : '_electronApi';
          // Convert import rename syntax `name as alias` → destructuring syntax `name: alias`
          const namedItems = namedPart.split(',').map((s: string) => s.trim().replace(/\s+as\s+/g, ': ')).filter(Boolean);
          const destructure = namedItems.join(', ');
          return [
            `const _electronNs = await import('electron');`,
            // Electron provides the API on the CJS-wrapped default, but may also provide named exports
            `const ${defaultName} = (_electronNs.app != null) ? _electronNs : (_electronNs.default ?? _electronNs);`,
            `const { ${destructure} } = ${defaultName};`,
          ].join('\n');
        },
      );
      // Handle bare default-only: import electron from "electron";
      chunk.code = chunk.code.replace(
        /^import\s+([\w$]+)\s+from\s+"electron";/m,
        (_match, defaultName) => [
          `const _electronNs = await import('electron');`,
          `const ${defaultName} = (_electronNs.app != null) ? _electronNs : (_electronNs.default ?? _electronNs);`,
        ].join('\n'),
      );
    }
  },
});

export default defineConfig({
  main: {
    build: {
      // https://electron-vite.org/guide/dev#dependencies-vs-devdependencies
      // For the main process and preload, the best practice is to externalize dependencies and only bundle our own code.
      externalizeDeps: true,
      target: 'node22.18',
      sourcemap: true,
      rollupOptions: {
        plugins: [fixElectronImports()],
      },
    },
  },
  preload: {
    build: {
      externalizeDeps: true,
      target: 'node22.18',
      sourcemap: true,
      rollupOptions: {
        plugins: [fixElectronImports()],
      },
    },
  },
  renderer: {
    plugins: [react()],
    build: {
      target: 'chrome140',
      sourcemap: true,
      chunkSizeWarningLimit: 3e6,
    },
    server: {
      port: 3001,
      host: '127.0.0.1',
    },
  },
});
