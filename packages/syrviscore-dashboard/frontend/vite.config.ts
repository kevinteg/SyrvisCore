import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The built SPA is copied into the Python package's static/ dir in the image and
// served by FastAPI. In dev, proxy API + auth calls to the uvicorn backend on :8000.
const backend = process.env.DASHBOARD_BACKEND ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: backend, changeOrigin: true },
      "/auth": { target: backend, changeOrigin: true },
      "/healthz": { target: backend, changeOrigin: true },
    },
  },
});
