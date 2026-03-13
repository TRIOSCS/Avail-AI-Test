/**
 * Vitest config for AVAIL AI frontend tests.
 * Run: npm run test:frontend
 * Note: intake_helpers and intake_intakeflow use node:test — run via test:frontend:unit / test:frontend:e2e
 */
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/frontend/utils.test.js"],
    exclude: ["**/node_modules/**", "**/dist/**", "**/*.unit.test.mjs", "**/*.e2e.test.mjs"],
    testTimeout: 10000,
    environment: "node",
    globals: false,
  },
});
