/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './app/templates/**/*.html',
    './app/static/styles.css',
    './app/static/htmx_app.js',
    './app/**/*.py',
  ],
  safelist: [
    // All color shades used in the app — prevents purge issues when
    // adding new shades in templates between deploys.
    {
      pattern: /^(bg|text|border)-(slate|gray|brand|accent|amber|emerald|rose|blue|violet|sky)-(50|100|200|300|400|500|600|700|800|900)$/,
      variants: ['hover'],
    },
    // Design-system shadow tiers — keep available even before the page
    // sweeps reference them directly in templates.
    'shadow-card',
    'shadow-float',
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Aptos', 'Segoe UI', 'system-ui', '-apple-system', 'sans-serif'],
      },
      // Two-tier shadow language: `card` for resting surfaces (≈ the old
      // shadow-sm, brand-tinted), `float` for modals/dropdowns/action rails.
      // Replaces the five ad-hoc shadow levels that had drifted across pages.
      boxShadow: {
        card: '0 1px 2px 0 rgb(28 33 48 / 0.06), 0 1px 3px 0 rgb(28 33 48 / 0.10)',
        float: '0 4px 16px rgb(28 33 48 / 0.12)',
      },
      colors: {
        brand: {
          50: '#F8F9FA',
          100: '#F0F1F4',
          200: '#D8DBE2',
          300: '#ADB3BF',
          400: '#838B9B',
          500: '#5F6878',
          600: '#4B5463',
          700: '#3A4252',
          800: '#2A3040',
          900: '#1C2130',
        },
        // Trio brand azure (trioscs.com) — the single interactive accent.
        // Mirrors --accent* in styles.css; 500 is the AA-safe base used for
        // primary buttons / active nav / key figures.
        accent: {
          50: '#E7F3FA',
          100: '#C9E6F4',
          200: '#9BD0EC',
          300: '#66B5E0',
          400: '#2E97D2',
          500: '#007DBD',
          600: '#0B6699',
          700: '#095A85',
          800: '#094C6E',
          900: '#0A3F5A',
        }
      }
    }
  },
  plugins: [],
}
