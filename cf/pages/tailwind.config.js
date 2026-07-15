/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["-apple-system", "BlinkMacSystemFont", "SF Pro Text", "Inter", "PingFang SC", "sans-serif"],
        display: ["-apple-system", "BlinkMacSystemFont", "SF Pro Display", "Inter Display", "PingFang SC", "sans-serif"],
        mono: ["SF Mono", "JetBrains Mono", "ui-monospace", "Menlo", "monospace"],
      },
      colors: {
        // Match the OwnDance mockup palette we already validated visually.
        ink: { DEFAULT: "#14151A", 2: "#4A4D55", 3: "#83868E", 4: "#B4B6BC" },
        line: { DEFAULT: "#ECEAE1", 2: "#DCD8CB" },
        paper: { DEFAULT: "#FAF9F5", 2: "#F5F3EC" },
        amber: { DEFAULT: "#B98029", ink: "#7A5518", tint: "#F4E9D3" },
        pos: { DEFAULT: "#2F7D5A", tint: "#E4EFE7" },
        neg: { DEFAULT: "#A73434", tint: "#F3E3E1" },
      },
    },
  },
  plugins: [],
};
