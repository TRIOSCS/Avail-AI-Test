// ESLint flat config for AvailAI frontend JavaScript.
// Covers Alpine.js components and HTMX app code.
// Called by: npm run lint
// Depends on: eslint, @eslint/js

import js from '@eslint/js';

export default [
  js.configs.recommended,
  {
    files: ['app/static/**/*.js'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: {
        // Browser globals
        window: 'readonly',
        document: 'readonly',
        console: 'readonly',
        fetch: 'readonly',
        setTimeout: 'readonly',
        setInterval: 'readonly',
        clearTimeout: 'readonly',
        clearInterval: 'readonly',
        requestAnimationFrame: 'readonly',
        URL: 'readonly',
        URLSearchParams: 'readonly',
        CustomEvent: 'readonly',
        Event: 'readonly',
        FormData: 'readonly',
        AbortController: 'readonly',
        EventSource: 'readonly',
        localStorage: 'readonly',
        sessionStorage: 'readonly',
        history: 'readonly',
        location: 'readonly',
        navigator: 'readonly',
        HTMLElement: 'readonly',
        MutationObserver: 'readonly',
        ResizeObserver: 'readonly',
        IntersectionObserver: 'readonly',
        // Alpine.js
        Alpine: 'readonly',
        // HTMX
        htmx: 'readonly',
      },
    },
    rules: {
      'no-unused-vars': ['warn', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
      'no-console': 'off',  // Console is used for dev/debug
      'no-undef': 'error',
      'no-redeclare': 'error',
      'no-constant-condition': 'warn',
      'no-empty': ['warn', { allowEmptyCatch: true }],
      'prefer-const': 'warn',
      'eqeqeq': ['warn', 'smart'],
    },
  },
  {
    // Service worker has its own globals
    files: ['app/static/**/sw.js', 'app/static/public/sw.js'],
    languageOptions: {
      globals: {
        self: 'readonly',
        caches: 'readonly',
        clients: 'readonly',
        skipWaiting: 'readonly',
      },
    },
  },
  {
    // Ignore build output and vendor files
    ignores: [
      'app/static/dist/**',
      'app/static/vendor/**',
      'node_modules/**',
    ],
  },
];
