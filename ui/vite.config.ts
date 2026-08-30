import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// `npm run build` emits into ui/dist, which `nsemom serve` serves directly.
// `npm run dev` proxies /api to the Python server so hot reload works.
export default defineConfig({
  plugins: [react()],
  build: { outDir: 'dist', emptyOutDir: true, target: 'es2020' },
  server: {
    port: 5173,
    proxy: { '/api': { target: 'http://127.0.0.1:8787', changeOrigin: true } },
  },
})
