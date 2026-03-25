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
        }
      }
    }
  },
  plugins: [],
}
