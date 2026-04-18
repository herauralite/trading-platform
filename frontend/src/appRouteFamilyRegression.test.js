import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

async function readFrontendFile(relPath) {
  return readFile(new URL(`../${relPath}`, import.meta.url), 'utf8')
}

test('canonical route ownership keeps homepage at / and app shell on /app family', async () => {
  const indexHtml = await readFrontendFile('index.html')
  const appEntryHtml = await readFrontendFile('app.html')
  const appSource = await readFrontendFile('src/App.jsx')
  const appLandingSource = await readFrontendFile('src/pages/AppLandingPage.jsx')
  const accountsSource = await readFrontendFile('src/pages/AccountsOverviewPage.jsx')
  const connectionsSource = await readFrontendFile('src/pages/ConnectionsPage.jsx')
  const rewrites = JSON.parse(await readFrontendFile('vercel.json')).rewrites
  const viteConfig = await readFrontendFile('vite.config.js')

  assert.equal(indexHtml.includes('class="hero"'), true)
  assert.equal(appEntryHtml.includes('class="hero"'), false)
  assert.equal(appEntryHtml.includes('data-app-shell="canonical"'), true)

  assert.deepEqual(rewrites, [
    { source: '/app', destination: '/app.html' },
    { source: '/app/:path*', destination: '/app.html' },
  ])

  const redirects = JSON.parse(await readFrontendFile('vercel.json')).redirects
  assert.deepEqual(redirects, [
    { source: '/accounts', destination: '/app/accounts', permanent: false },
    { source: '/connections', destination: '/app/connections', permanent: false },
  ])

  assert.equal(viteConfig.includes('input: {'), true)
  assert.equal(viteConfig.includes("app: 'app.html'"), true)

  assert.equal(appSource.includes('path="/app"'), true)
  assert.equal(appSource.includes('path="/app/accounts"'), true)
  assert.equal(appSource.includes('path="/app/connections"'), true)
  assert.equal(appSource.includes('className="app-nav-link add-account-nav-link"'), true)

  assert.equal(appSource.includes('TaliTrade Command Center'), false)
  assert.equal(appSource.includes('<h2>Session</h2>'), false)
  assert.equal(appSource.includes('Loading Telegram secure sign-in widget…'), false)

  assert.equal(indexHtml.includes('The platform built for <span class="accent">prop firm</span> traders.'), true)
  assert.equal(indexHtml.includes('Join the Beta'), true)

  assert.equal(appSource.includes('TaliTrade Platform'), false)
  assert.equal(appSource.includes('Primary login path: Telegram authenticated session.'), false)
  assert.equal(appSource.includes('Preparing secure Telegram sign-in…'), false)
  assert.equal(appSource.includes('TaliTrade Premium Workspace'), false)
  assert.equal(appSource.includes('Secure Telegram Sign-In'), false)
  assert.equal(appSource.includes("className={`app ${signedIn ? 'app-authenticated' : 'app-unauthenticated'}`}"), true)
  assert.equal(appSource.includes('{!signedIn ? ('), true)
  assert.equal(appSource.includes('<Routes>'), true)
  assert.equal(appSource.includes('signedIn && hasZeroConnectedAccounts'), true)

  assert.equal(appLandingSource.includes('Workspace Dashboard'), true)
  assert.equal(accountsSource.includes('<h2>Accounts</h2>'), true)
  assert.equal(connectionsSource.includes('<h2>Connections</h2>'), true)
})
