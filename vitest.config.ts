import { defineConfig } from 'vitest/config';
import { resolve } from 'path';

export default defineConfig({
  test: {
    environment: 'jsdom',
    include: ['tests/frontend/**/*.test.ts'],
    globals: true,
  },
  resolve: {
    alias: {
      '@static': resolve(__dirname, 'app/static'),
    },
  },
});
