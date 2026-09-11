import { defineConfig } from "vitest/config";

// The orchestrator's loopback address (config.py HOST/PORT defaults, ADR-017). `npm run dev`
// serves the page with hot reload and forwards its socket to a tutor started the usual way.
const BACKEND = "127.0.0.1:8000";

export default defineConfig({
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: { "/ws": { target: `ws://${BACKEND}`, ws: true } },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // The avatar, the cast and the preview audio are served from public/ by the orchestrator
    // itself (backend/app.py), so a rebuilt persona never needs a rebuilt page.
    copyPublicDir: false,
    target: "es2022",
  },
  test: {
    include: ["src/**/*.test.ts"],
    environment: "node",
  },
});
