import { defineConfig } from 'vite';

export default defineConfig({
  cacheDir: '.vite-cache',
  optimizeDeps: {
    force: true,
  },
  server: {
    port: 8080,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
});
