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
      // The PAXS backend binds PAXS_PORT (default 8000 — see run_paxs.py and
      // deploy/docker-compose.lb.yml). 8042 is Orthanc's default port and was a
      // stale copy from a template: proxying here pointed every /api call at a
      // dead port, so the dev study list could never load.
      '/api': 'http://localhost:8000',
      '/dicom-web': 'http://localhost:8000',
      // The study-list template loads the hand-maintained enhancement script
      // from /viewer/ (FastAPI serves it), which the dev server must forward too.
      '/viewer': 'http://localhost:8000',
    },
  },
  build: {
    outDir: 'dist',
    // C07: a stale hashed bundle keeps being served (and keeps shipping any
    // content it contains), so each build now replaces the output directory.
    emptyOutDir: true,
  },
})
