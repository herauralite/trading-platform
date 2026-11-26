import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

// Line endings are normalised so the assertions hold on a Windows checkout (Git's default there is CRLF).
async function readFrontendFile(relPath) {
  return (await readFile(new URL(`../${relPath}`, import.meta.url), 'utf8')).replace(/\r\n/g, '\n')
}

test('react app bootstrap preserves telegram config + token persistence recovery contract', async () => {
  const appSource = await readFrontendFile('src/App.jsx')

  assert.equal(appSource.includes('void loadTelegramAuthConfig()'), true)
  assert.equal(appSource.includes('if (sessionToken) {\n      void bootstrapSession(sessionToken)'), true)

  assert.equal(appSource.includes("const [sessionToken, setSessionToken] = useState(localStorage.getItem(SESSION_STORAGE_KEY) || '')"), true)
  assert.equal(appSource.includes("const [sessionUser, setSessionUser] = useState(normalizeSessionUser(parseStoredUser(localStorage.getItem(USER_STORAGE_KEY))))"), true)

  assert.equal(appSource.includes("const meRes = await axios.get(buildApiUrl('/auth/me'), { headers: buildAuthHeaders(token) })"), true)
  assert.equal(appSource.includes('if (!canHydrateSession(meRes.data)) throw new Error(\'No authenticated user found in session\')'), true)

  assert.equal(appSource.includes('commitSession(token, user)'), true)
  assert.equal(appSource.includes('localStorage.setItem(SESSION_STORAGE_KEY, token)'), true)
  assert.equal(appSource.includes('localStorage.setItem(USER_STORAGE_KEY, JSON.stringify(normalizedUser))'), true)
  assert.equal(appSource.includes('const signedIn = Boolean(sessionToken && sessionUser?.telegram_user_id)'), true)
})

test('react app keeps /app gate behavior and deep-link routes stable', async () => {
  const appSource = await readFrontendFile('src/App.jsx')

  assert.equal(appSource.includes('{!signedIn ? ('), true)
  assert.equal(appSource.includes('path="/app"'), true)
  assert.equal(appSource.includes('<AppLandingPage'), true)
  assert.equal(appSource.includes('path="/app/accounts"'), true)
  assert.equal(appSource.includes('path="/app/connections"'), true)
  assert.equal(appSource.includes('<Route path="*" element={<Navigate to="/app/accounts" replace />} />'), true)
})

test('react app resolves account-presence from workspace inventory once hydrated (including zero rows)', async () => {
  const appSource = await readFrontendFile('src/App.jsx')

  assert.equal(appSource.includes('const [workspaceApiHydrated, setWorkspaceApiHydrated] = useState(false)'), true)
  assert.equal(appSource.includes('if (USE_ACCOUNT_WORKSPACES_API && workspaceApiHydrated) return workspaceApiAccounts'), true)
  assert.equal(appSource.includes('setWorkspaceApiHydrated(true)'), true)
  assert.equal(appSource.includes('setWorkspaceApiHydrated(false)'), true)
})

test('homepage sends open-beta users directly to app sign-in', async () => {
  const landingHtml = await readFrontendFile('public/landing.html')

  assert.ok(landingHtml.includes('Join the open beta'))
  assert.ok(landingHtml.includes('href="/app">Join the open beta'))
  assert.doesNotMatch(landingHtml, /checkPassword|homepageAuthGate|requireHomepageAuth|onHomepageTelegramAuth|waitlist\/verified|beta invite/i)
})

test('vercel rewrites preserve direct loads for /app, /app/accounts, and /app/connections', async () => {
  const raw = await readFrontendFile('vercel.json')
  const config = JSON.parse(raw)
  const rewrites = Array.isArray(config.rewrites) ? config.rewrites : []

  assert.deepEqual(rewrites, [
    { source: '/', destination: '/landing.html' },                  // the homepage
    { source: '/privacy', destination: '/privacy.html' },
    { source: '/app', destination: '/app.html' },
    { source: '/app/:path*', destination: '/app.html' },
  ])
})
