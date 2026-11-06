import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#0a0e14",
        panel: "#0f1620",
        raised: "#141d2a",
        line: "#1c2733",
        ink: "#c9d4e0",
        mute: "#6b7a8c",
        up: "#26d07c",
        down: "#ff5c6c",
        warn: "#f0b429",
        accent: "#3dd5f3",
      },
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "Liberation Mono", "monospace"],
      },
    },
  },
  plugins: [],
};

export default config;
