import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

test('/app route family resolves through canonical React shell entrypoint', async () => {
  const vercelRaw = await readFile(new URL('../vercel.json', import.meta.url), 'utf8')
  const mainSource = await readFile(new URL('./main.jsx', import.meta.url), 'utf8')
  const appSource = await readFile(new URL('./App.jsx', import.meta.url), 'utf8')

  const rewrites = JSON.parse(vercelRaw).rewrites
  assert.deepEqual(rewrites, [
    { source: '/app', destination: '/index.html' },
    { source: '/app/:path*', destination: '/index.html' },
  ])

  assert.equal(mainSource.includes('<BrowserRouter>'), true)
  assert.equal(mainSource.includes('<App />'), true)

  assert.equal(appSource.includes('path="/app"'), true)
  assert.equal(appSource.includes('path="/app/accounts"'), true)
  assert.equal(appSource.includes('path="/app/connections"'), true)
  assert.equal(appSource.includes('className="app-nav-link add-account-nav-link"'), true)
})
