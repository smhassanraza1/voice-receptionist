import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// The Python server (python -m receptionist.web) owns /api and /healthz. In dev, Vite serves the
// UI on :5173 and proxies those paths there; in production the server hosts web/dist itself.
const API_TARGET = process.env.RECEPTIONIST_API_URL ?? 'http://127.0.0.1:8000';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: API_TARGET, changeOrigin: false },
      '/healthz': { target: API_TARGET },
    },
  },
  preview: {
    proxy: {
      '/api': { target: API_TARGET },
      '/healthz': { target: API_TARGET },
    },
  },
  build: {
    target: 'es2022',
    // The server hosts web/dist publicly, so no .map files go in it.
    sourcemap: false,
    // livekit-client is ~630 kB minified on its own; keep it in a cacheable vendor chunk rather
    // than letting it land in whichever app chunk imported it first.
    chunkSizeWarningLimit: 700,
    rolldownOptions: {
      output: {
        codeSplitting: {
          // React outranks LiveKit, so React's own module never lands in the LiveKit chunk (the
          // components package imports it) and pages without a call never download the SDK.
          groups: [
            { name: 'react', test: /node_modules[\\/](react|react-dom|scheduler)[\\/]/, priority: 2 },
            { name: 'livekit', test: /node_modules[\\/](livekit-client|@livekit)[\\/]/, priority: 1 },
          ],
        },
      },
    },
  },
});
