import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: { host: "127.0.0.1", port: 1420, strictPort: true },
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
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
