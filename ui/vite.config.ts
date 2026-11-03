import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  base: '/chat/',
  plugins: [react(), tailwindcss()],
  server: {
    proxy: { '/mobile': { target: 'http://127.0.0.1:7683', changeOrigin: false } },
  },
})
