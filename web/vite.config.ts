import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The web desktop talks to the aios control plane through a same-origin proxy:
// in dev the Vite server (port 5173) forwards /v1 (REST + WebSocket) to the
// API server on 8000, so the browser never needs cross-origin credentials.
// In production the built assets are served by `aios serve` itself, where
// /v1 is already same-origin.
const API_TARGET = process.env.AIOS_API_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/v1": {
        target: API_TARGET,
        changeOrigin: true,
        ws: true, // WebSocket upgrade for /v1/ws/feed and /v1/agents/{pid}/ws/console
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});