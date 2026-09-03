import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The FastAPI app (main.py) serves the build output from frontend/dist:
//   index.html at the mount root, hashed files under "assets".
// base:"./" keeps asset URLs relative, so they resolve correctly at the instance root or behind
// a path-prefixed reverse proxy. An absolute base could escape that prefix and 404.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    target: ["es2020", "chrome87", "edge88", "firefox78", "safari14"],
    cssTarget: ["chrome87", "edge88", "firefox78", "safari14"],
    outDir: "dist",
    emptyOutDir: true,
  },
});
