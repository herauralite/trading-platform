import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'path'
import { randomUUID } from 'node:crypto'
import { readFile, writeFile } from 'node:fs/promises'

function dashboardVersion() {
  const marker = 'name="talitrade-app-version" content=""'
  return {
    name: 'talitrade-dashboard-version',
    apply: 'build',
    async writeBundle({ dir }) {
      const dashboardPath = resolve(dir, 'app.html')
      const html = await readFile(dashboardPath, 'utf8')
      if (!html.includes(marker)) throw new Error('Dashboard version marker is missing')

      const version = randomUUID()
      await writeFile(dashboardPath, html.replace(marker, `name="talitrade-app-version" content="${version}"`))
      await writeFile(resolve(dir, 'talitrade-app-version.json'), JSON.stringify({ version }))
    },
  }
}

export default defineConfig({
  plugins: [react(), dashboardVersion()],
  build: {
    rollupOptions: {
      input: resolve(__dirname, 'src/index.html'),
      output: {
        dir: resolve(__dirname, 'dist')
      }
    },
    outDir: 'dist'
  }
})