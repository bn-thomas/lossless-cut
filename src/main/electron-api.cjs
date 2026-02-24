'use strict';
// CJS shim (currently unused — kept for reference).
// NOTE: require('electron') from an ESM main process context is NOT intercepted by Electron 38.
// Use dynamic import('electron') in the main ESM bundle instead (see electron.vite.config.ts).
const electron = require('electron');
module.exports = electron;
