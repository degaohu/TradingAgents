import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  return {
    plugins: [react()],
    resolve: {
      alias: { "@": path.resolve(__dirname, "src") },
    },
    server: {
      proxy: {
        // Proxy /api and /public to the local wrangler dev of the API worker
        // (`cd cf/workers/api && npm run dev`) — default port 8787.
        "/api": { target: env.VITE_API_TARGET || "http://127.0.0.1:8787", changeOrigin: true },
        "/public": { target: env.VITE_API_TARGET || "http://127.0.0.1:8787", changeOrigin: true },
      },
    },
    build: { sourcemap: true },
  };
});
