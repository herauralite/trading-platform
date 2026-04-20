import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'node:path'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      input: {
        main: resolve(__dirname, 'index.html'),
        legacyAddAccountBridgeEntry: resolve(__dirname, 'src/legacyAddAccountBridgeEntry.jsx'),
      },
      output: {
        entryFileNames: (chunkInfo) => {
          if (chunkInfo.name === 'legacyAddAccountBridgeEntry') return 'assets/legacyAddAccountBridgeEntry.js'
          return 'assets/[name]-[hash].js'
        },
      },
    },
  },
})
