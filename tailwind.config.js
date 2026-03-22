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
          50: '#F4F8FC',
          100: '#E4EFF8',
          200: '#CADDEF',
          300: '#A3C4E0',
          400: '#7AAAD0',
          500: '#5B8FB8',
          600: '#4A7CB5',
          700: '#3D6895',
          800: '#2E5070',
          900: '#20384E',
        }
      }
    }
  },
  plugins: [],
}
