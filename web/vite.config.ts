// Dev server and preview both listen on loopback only and proxy /api to the daily
// checklist-care server (`uv run checklist-care serve`, 127.0.0.1:8450). The built app is
// served by that server from web/dist, so production needs no proxy. Loopback form from
// yarvis/web/vite.config.ts.
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const loopback = "127.0.0.1";
const devPort = 5450;
const apiTarget = `http://${loopback}:8450`;
const proxy = { "/api": { target: apiTarget, changeOrigin: false } };

export default defineConfig({
  plugins: [react()],
  server: { host: loopback, port: devPort, strictPort: true, proxy },
  preview: { host: loopback, port: devPort, strictPort: true, proxy },
  build: { outDir: "dist", emptyOutDir: true },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    testTimeout: 30_000,
  },
});
