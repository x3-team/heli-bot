import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    fs: { allow: [path.resolve(__dirname, "../..")] },
    proxy: { "/v1": "http://127.0.0.1:8000" },
  },
  preview: { host: "0.0.0.0", port: 5173 },
  resolve: {
    alias: [
      {
        find: "@heli/ui/tokens.css",
        replacement: path.resolve(__dirname, "../../packages/ui/src/tokens.css"),
      },
      {
        find: "@heli/ui",
        replacement: path.resolve(__dirname, "../../packages/ui/src/index.ts"),
      },
    ],
  },
});
