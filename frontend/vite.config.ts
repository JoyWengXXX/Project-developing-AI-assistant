import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// 後端（FastAPI）位址。開發時瀏覽器只連 Vite（5173），
// 由 Vite 把 /api、/health、/ready 轉發到後端，因此後端不需要開放 CORS。
const BACKEND = 'http://127.0.0.1:8000'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': BACKEND,
      '/health': BACKEND,
      '/ready': BACKEND,
    },
  },
})
