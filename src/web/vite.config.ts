import { defineConfig } from "vite";

export default defineConfig({
  base: "/workspace/",
  build: { target: "es2022", outDir: "dist", emptyOutDir: true },
  server: { proxy: { "/auth": "http://127.0.0.1:8000", "/me": "http://127.0.0.1:8000", "/capabilities": "http://127.0.0.1:8000", "/knowledge": "http://127.0.0.1:8000", "/knowledge-bases": "http://127.0.0.1:8000", "/document-feedback": "http://127.0.0.1:8000", "/documents": "http://127.0.0.1:8000", "/parse-runs": "http://127.0.0.1:8000", "/tasks": "http://127.0.0.1:8000", "/admin": "http://127.0.0.1:8000" } }
});
