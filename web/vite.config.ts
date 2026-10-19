import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Same-origin in production (nginx proxies /v1 → api); in dev, vite plays
// the proxy so `npm run dev` against a locally running api needs no CORS.
export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      '/v1': { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
})
