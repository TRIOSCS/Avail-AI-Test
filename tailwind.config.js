/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./app/templates/**/*.html', './app/static/styles.css'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['DM Sans', 'system-ui', '-apple-system', 'sans-serif'],
      },
      colors: {
        brand: {
          50: '#f0f4f8',
          100: '#dce4ed',
          200: '#b7c7d8',
          300: '#8b9daf',
          400: '#6a8bad',
          500: '#3d6895',
          600: '#345a82',
          700: '#2b4c6e',
          800: '#1e3a56',
          900: '#142a40',
        }
      }
    }
  },
  plugins: [],
}
