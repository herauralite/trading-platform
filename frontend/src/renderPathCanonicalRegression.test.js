import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

test('/app route family resolves through canonical React shell entrypoint', async () => {
  const vercelRaw = await readFile(new URL('../vercel.json', import.meta.url), 'utf8')
  const appEntryHtml = await readFile(new URL('../app.html', import.meta.url), 'utf8')
  const mainSource = await readFile(new URL('./main.jsx', import.meta.url), 'utf8')
  const appSource = await readFile(new URL('./App.jsx', import.meta.url), 'utf8')

  const config = JSON.parse(vercelRaw)
  const rewrites = config.rewrites
  const redirects = config.redirects
  assert.deepEqual(rewrites, [
    { source: '/app', destination: '/app.html' },
    { source: '/app/:path*', destination: '/app.html' },
  ])

  assert.equal(appEntryHtml.includes('data-app-shell="canonical"'), true)
  assert.equal(appEntryHtml.includes('src="/src/main.jsx"'), true)

  assert.equal(mainSource.includes('<BrowserRouter>'), true)
  assert.equal(mainSource.includes('<App />'), true)

  assert.equal(appSource.includes('path="/app"'), true)
  assert.equal(appSource.includes('path="/app/accounts"'), true)
  assert.equal(appSource.includes('path="/app/connections"'), true)
  assert.equal(appSource.includes('className="app-nav-link add-account-nav-link"'), true)
  assert.equal(appSource.includes('to="/app">Dashboard</NavLink>'), true)
  assert.equal(appSource.includes("className={`app ${signedIn ? 'app-authenticated' : 'app-unauthenticated'}`}"), true)
  assert.equal(appSource.includes('<Routes>'), true)
  assert.equal(appSource.includes('Sign in with Telegram'), true)
  assert.equal(appSource.includes('TaliTrade Premium Workspace'), false)
  assert.equal(appSource.includes('TaliTrade Platform'), false)
  assert.deepEqual(redirects, [
    { source: '/accounts', destination: '/app/accounts', permanent: false },
    { source: '/connections', destination: '/app/connections', permanent: false },
  ])
})
