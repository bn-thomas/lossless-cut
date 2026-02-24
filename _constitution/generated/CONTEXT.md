<!-- DO NOT EDIT — Auto-generated from workspace.yaml -->

# LosslessCut — Workspace Context

## Current State

- **Phase:** idle
- **Health:** green
- **Active Item:** (none)
- **Parent:** restai (depth 1)

## Modules

- **governance**: _constitution/, README.md
- **frontend**: src/, public/

## Work Queue

1. [ ] **lc-restai-control-layer** — RestAI control layer for LosslessCut [M]
   Depends: none | Progress: 0%
2. [ ] **lc-video-intelligence-service** — Video intelligence service (FFprobe + audio analysis) [L]
   Depends: lc-restai-control-layer | Progress: 0%
3. [ ] **lc-ai-segment-generation** — AI segment generation (video → EDL/CSV) [M]
   Depends: lc-video-intelligence-service | Progress: 0%
4. [ ] **lc-pipeline-watch-folder** — Watch folder pipeline (end-to-end automation) [M]
   Depends: lc-ai-segment-generation | Progress: 0%
5. [ ] **lc-ui-ai-panel** — RestAI AI panel in LosslessCut UI [L]
   Depends: lc-restai-control-layer | Progress: 0%

## Recently Completed

- ~~explore-codebase~~ — Explore LosslessCut codebase
- ~~run-existing-tests~~ — Run existing test suite
- ~~assess-code-quality~~ — Assess code quality and improvement opportunities

## Summary

- Queue: 5 items
- Completed: 3 items
- Total: 8 items
