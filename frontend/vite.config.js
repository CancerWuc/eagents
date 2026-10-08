import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  server: {
    host: '0.0.0.0',
    port: 5174,
    watch: {
      // Docker Desktop on Windows can miss native filesystem events from bind mounts.
      usePolling: true,
      interval: 300,
    },
    proxy: {
      '/api': process.env.EAGENT_BACKEND || 'http://127.0.0.1:8189',
      '/a2a': process.env.EAGENT_BACKEND || 'http://127.0.0.1:8189',
    },
  },
})
