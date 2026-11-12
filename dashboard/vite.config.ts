import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const API = "http://localhost:8000";

// Dev proxy: the SPA talks to the PACT API on the same origin in production (served by FastAPI).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/admin": API,
      "/merchant": API,
      "/payments": API,
      "/sim": API,
      "/health": API,
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
