/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        primary: "#3b82f6",
        secondary: "#10b981",
        dark: "#0f172a",
      },
      backdropBlur: {
        xs: '2px',
      }
    },
  },
  plugins: [],
}
