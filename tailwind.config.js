/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./app/templates/**/*.html'],
  theme: {
    extend: {
      colors: {
        brand: {
          50: '#eef5fb',
          100: '#d4e6f5',
          200: '#a9cde9',
          300: '#7ab0d9',
          400: '#4a93c8',
          500: '#1a7abf',
          600: '#156aa6',
          700: '#2d5f8a',
          800: '#1f4a6e',
          900: '#163755',
        }
      }
    }
  },
  plugins: [],
}
