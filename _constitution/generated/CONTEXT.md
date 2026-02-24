# LosslessCut — Context

> Auto-generated workspace context. Source: https://github.com/mifi/lossless-cut

## Overview
The swiss army knife of lossless video/audio editing. Cross-platform Electron desktop app built with React, TypeScript, and FFmpeg for instant, lossless trimming and cutting of video/audio files.

## Source Repository
- **URL:** https://github.com/mifi/lossless-cut
- **License:** GPL-2.0
- **Primary Language:** TypeScript (98%)
- **Stars:** 38,322
- **Onboarded:** 2026-02-17
- **Explored:** 2026-02-23

## Upstream
Git remote `upstream` points to the source repository for reference.
Fresh repo — upstream history was not imported. Use upstream remote for cherry-picks or diffs against source.

---

## Architecture

### Process Model (Electron)
```
Electron Main (Node.js)          Electron Renderer (Chromium)
  src/main/index.ts                src/renderer/src/App.tsx
  src/main/ffmpeg.ts               src/renderer/src/hooks/
  src/main/httpServer.ts     IPC   src/renderer/src/components/
  src/main/menu.ts          <---> src/renderer/src/Timeline.tsx
  src/main/configStore.ts          src/renderer/src/SegmentList.tsx
  src/preload/index.ts       ^     src/renderer/src/BottomBar.tsx
                             |     src/renderer/src/TopMenu.tsx
                         preload bridge (contextBridge)
```

### Key Patterns
- **IPC flow:** HTTP API → main `onKeyboardAction()` → IPC to renderer → React state update → FFmpeg op
- **State management:** React hooks + Immer (immutable state)
- **FFmpeg:** Called as subprocess in main process via `execa`
- **Config persistence:** `electron-store` (JSON config file)
- **i18n:** i18next with 48 language locales in `locales/`

### Tech Stack
| Layer | Technology |
|-------|-----------|
| Desktop | Electron 38.7.2 |
| UI | React 18.3.1 + TypeScript 5.9.3 |
| Build | electron-vite 5.0.0 + Vite 7 |
| Styling | CSS Modules + SASS + Radix UI |
| Media | FFmpeg/FFprobe (bundled platform binaries) |
| i18n | i18next + react-i18next (48 locales) |
| Testing | Vitest 4.0.16 |
| Package | Yarn 4.11.0 |
| Deps | execa 9, express 5, electron-store 5, zod 4, immer 11 |

---

## Directory Structure
```
src/
  main/                     # Electron main process
    index.ts                # Entry point — app lifecycle, IPC handlers
    ffmpeg.ts               # FFmpeg/FFprobe subprocess wrappers (26 KB)
    httpServer.ts           # HTTP API server (Express, 56 lines)
    menu.ts                 # Native menu + keyboard action registry (15 KB)
    configStore.ts          # electron-store config persistence (14 KB)
    compatPlayer.ts         # HTML5 playback compat layer
    i18n.ts / i18nCommon.ts # i18n setup for main process
    logger.ts               # Winston logger
    updateChecker.ts        # GitHub release update check
    progress.ts             # FFmpeg progress parsing
    util.ts                 # Main process utilities
  renderer/
    src/                    # React renderer
      App.tsx               # Root component (~1400 lines, monolithic)
      Timeline.tsx          # Video timeline with waveform/thumbnails
      SegmentList.tsx       # Left panel — cut segments list
      TopMenu.tsx           # Top navigation bar
      BottomBar.tsx         # Playback controls
      StreamsSelector.tsx   # Track/stream selector
      MediaSourcePlayer.tsx # HTML5 video player
      NoFileLoaded.tsx      # Empty state view
      hooks/                # 22 custom hooks (state, FFmpeg, keyboard, etc.)
      components/           # 58 UI components (dialogs, forms, etc.)
      util/                 # Shared renderer utilities
      edlFormats.ts         # EDL format parsers/serializers
      edlStore.ts           # Project file load/save
      segments.ts           # Segment data model
      ffmpeg.ts             # Renderer-side FFmpeg IPC calls
      types.ts              # Shared TypeScript types
  preload/
    index.ts                # contextBridge IPC bridge
  common/
    constants.ts            # Shared constants (homepage URL, etc.)
locales/                    # 48 language translation files
docs/                       # User documentation (api.md, cli.md, batch.md, etc.)
script/                     # Build scripts (icon gen, docs gen, license check)
```

---

## HTTP API (RestAI Integration Entry Point)

**Enable:** `LosslessCut --http-api [port]` (default 8080)

**Endpoint:** `POST /api/action/:action` with optional JSON body

**Key actions:**
| Action | Description |
|--------|-------------|
| `export` | Export current segments (waits for completion) |
| `importEdlFile` | Import EDL/CSV/SRT/YouTube/FCPXML segments |
| `exportEdlFile` | Export current segments to file |
| `goToTimecodeDirect` | Seek to timecode `{"time": "HH:MM:SS"}` |
| `closeCurrentFile` | Close current video file |
| `openFilesDialog` | Open file picker |
| `html5ify` | Convert to browser-playable format |
| `deselectAllSegments` | Deselect all segments |
| `selectSegmentsByExpr` | Select segments by JS expression |

**Integration mechanism:**
1. RestAI ADL agent generates segment CSV/EDL from video analysis
2. ADL calls `POST /api/action/importEdlFile` → LosslessCut loads segments
3. ADL calls `POST /api/action/export` → LosslessCut executes lossless cut
4. ADL calls `POST /api/action/closeCurrentFile` → ready for next

**Implementation:** `src/main/httpServer.ts` (56 lines, Express 5)
**Action registry:** `src/main/menu.ts` — all keyboard actions defined here

---

## Test Suite
- **Framework:** Vitest 4.0.16
- **Command:** `npm test` (or `yarn test`)
- **Total files:** 9 test files
- **Location:** Co-located with source (`*.test.ts`)

| File | Tests |
|------|-------|
| `src/main/ffmpegUtil.test.ts` | FFmpeg utility functions |
| `src/main/pathToFileURL.test.ts` | URL path conversion |
| `src/main/progress.test.ts` | FFmpeg progress parsing |
| `src/renderer/src/edl.test.ts` | EDL data model |
| `src/renderer/src/edlFormats.test.ts` | Format parsers (CSV, YouTube, etc.) |
| `src/renderer/src/segments.test.ts` | Segment operations |
| `src/renderer/src/util/duration.test.ts` | Timecode/duration formatting |
| `src/renderer/src/util/rate-calculator.test.ts` | Bitrate calculations |
| `src/renderer/src/util/streams.test.ts` | Stream/track utilities |

---

## Pre-existing Dev Environment Fixes

Changes made to get the app running under Electron 38 + Node 22 + VS Code terminal:

| File | Change | Reason |
|------|--------|--------|
| `electron.vite.config.ts` | Added `fixElectronImports` Vite plugin | ESM/CJS named import conflict with Electron module |
| `package.json` | `dev` script prefixed with `env -u ELECTRON_RUN_AS_NODE` | VS Code sets this env var, breaking Electron startup |
| `src/main/i18n.ts` | Removed top-level `await` on i18n init | CJS output format doesn't support top-level await |
| `src/main/index.ts` | `electronUnhandled` changed to dynamic import | ESM-only package can't be statically imported in CJS context |
| `src/main/electron-api.cjs` | CJS shim (unused, kept for reference) | Exploration artifact |

**To run the app:** `yarn dev` (uses `env -u ELECTRON_RUN_AS_NODE` automatically)

---

## RestAI Integration Roadmap

Planned items in workspace queue:

1. **lc-restai-control-layer** — RestAI service to start/stop/control LosslessCut subprocess + HTTP API
2. **lc-video-intelligence-service** — FFprobe + audio energy + silence detection in RestAI backend
3. **lc-ai-segment-generation** — ADL agent: analyze video → generate EDL/CSV → push to LosslessCut
4. **lc-pipeline-watch-folder** — Watch folder + automatic pipeline trigger via ADL scheduler
5. **lc-ui-ai-panel** — Add RestAI sidebar panel to LosslessCut renderer (Electron UI fork)

**Goal:** Turn LosslessCut into an AI-assisted video clip factory for gaming/streaming content.
Pipeline: raw VOD → audio energy hype detection → AI segment plan → lossless clip export.
