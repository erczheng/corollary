/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: '127.0.0.1',
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        // **Not redundant — `/api/ws` does not work without it.** Vite
        // forwards websocket upgrades only on a proxy entry that opts in,
        // and `liveSocketUrl()` correctly takes its host from the page,
        // which in development is this dev server rather than the API. Drop
        // this flag and the socket mount still "lands": it just never
        // connects, and the only symptom is a reconnect loop nobody is
        // watching.
        //
        // Development only, and that is the point — the built bundle is
        // served from the API's own origin, so production never passes
        // through here at all. The HMR socket is unaffected: it runs on its
        // own path, not under `/api`.
        ws: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
  },
})
