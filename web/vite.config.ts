import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes("/highlight.js/") || id.includes("/lowlight/"))
            return "highlight";
          if (
            /\/(?:react-markdown|remark-|rehype-|micromark|mdast-|hast-|unist-|vfile|decode-named-character-reference|character-entities)/.test(
              id,
            )
          )
            return "markdown";
          if (/\/node_modules\/(?:react|react-dom|scheduler)\//.test(id))
            return "react";
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:5000", changeOrigin: true },
      "/ws": {
        target: "ws://127.0.0.1:5000",
        ws: true,
        changeOrigin: true,
      },
    },
  },
});
