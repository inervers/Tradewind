import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 构建产物进 frontend/dist，由 server.py 静态托管
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "dist",
    chunkSizeWarningLimit: 1500,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8101",
    },
  },
});
