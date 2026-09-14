import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

export default defineConfig({
  base: '/studies-ui/',
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, './src'),
    },
  },
  server: {
    port: 5174,
    proxy: {
      '/api': 'http://localhost:8042',
      '/dicom-web': 'http://localhost:8042',
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: false,
  },
})
