// The Playwright print suite (plan §5, §11): `npm --prefix web run test:print` prints the A4
// sheets of a real demo server to PDF in Chromium. npm runs `pretest:print` (`npm run build`)
// first, so the bundle is built once; then `webServer` starts
// `uv run checklist-care serve --demo --port 8452` from the checkout root, which serves that
// bundle, and stops it when the run ends. On Windows Playwright ends the whole process tree
// (`taskkill /T /F`), and tests/test_web_suites.py checks that the port is free again
// afterwards. The demo data lives in a fresh temp directory, so the operator's data is never
// touched.
// This file and e2e/ are type-checked by `npm run typecheck` through tsconfig.e2e.json, never by
// the production build (tsconfig.json): `serve --build` must work in a checkout whose
// node_modules predates @playwright/test.
import { defineConfig } from "@playwright/test";

const loopback = "127.0.0.1";
const port = 8452;
const baseURL = `http://${loopback}:${port}`;

export default defineConfig({
  testDir: "e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: true,
  reporter: [["list"]],
  timeout: 60_000,
  use: {
    baseURL,
    browserName: "chromium",
    headless: true,
    viewport: { width: 1280, height: 900 },
  },
  webServer: {
    command: `uv run checklist-care serve --demo --port ${port}`,
    cwd: "..",
    url: `${baseURL}/api/health`,
    timeout: 120_000,
    reuseExistingServer: false,
    stdout: "ignore",
    stderr: "pipe",
  },
});
