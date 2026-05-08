import { defineConfig } from 'vitest/config';
import { resolve } from 'path';

export default defineConfig({
  test: {
    environment: 'jsdom',
    include: ['tests/frontend/**/*.test.ts'],
    globals: true,
    // Fail loudly if the include glob matches no files. Without this an
    // accidentally-empty tests/frontend/ would silently pass CI and re-introduce
    // the "frontend tests don't run" regression that PR #109 was created to fix.
    passWithNoTests: false,
  },
  resolve: {
    alias: {
      '@static': resolve(__dirname, 'app/static'),
    },
  },
});
