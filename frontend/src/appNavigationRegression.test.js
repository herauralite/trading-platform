import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

test('authenticated app shell exposes visible routes to Accounts and Connections', async () => {
  const appSource = await readFile(new URL('./App.jsx', import.meta.url), 'utf8')
  const landingSource = await readFile(new URL('./pages/AppLandingPage.jsx', import.meta.url), 'utf8')

  assert.equal(appSource.includes('to="/app/accounts"'), true)
  assert.equal(appSource.includes('to="/app/connections"'), true)
  assert.equal(appSource.includes('path="/app"'), true)

  assert.equal(landingSource.includes('Go to Accounts'), true)
  assert.equal(landingSource.includes('Go to Connections'), true)
  assert.equal(landingSource.includes('Add Account'), true)
})

test('served dashboard links home and supplies branded home-screen icons', async () => {
  const publicRoot = new URL('../public/', import.meta.url)
  const dashboard = await readFile(new URL('app.html', publicRoot), 'utf8')
  const landing = await readFile(new URL('landing.html', publicRoot), 'utf8')
  const manifest = JSON.parse(await readFile(new URL('talitrade-app.webmanifest', publicRoot), 'utf8'))

  assert.match(dashboard, /<a id="tbBrandLanding"[^>]* href="\/"/)
  assert.doesNotMatch(dashboard, /landingOverlay|landingFrame/)
  assert.match(dashboard, /rel="manifest" href="\/talitrade-app\.webmanifest"/)
  assert.match(dashboard, /rel="apple-touch-icon"[^>]* href="\/talitrade-icon\.png"/)
  assert.match(landing, /href="\/talitrade-icon\.png"/)
  assert.equal(manifest.start_url, '/app')
  assert.equal(manifest.scope, '/app')
  assert.equal(manifest.display, 'standalone')

  for (const size of [192, 512]) {
    const icon = manifest.icons.find(({ sizes }) => sizes === `${size}x${size}`)
    assert.ok(icon, `Missing ${size}px app icon`)
    const png = await readFile(new URL(icon.src.slice(1), publicRoot))
    assert.equal(png.subarray(1, 4).toString(), 'PNG')
    assert.equal(png.readUInt32BE(16), size)
    assert.equal(png.readUInt32BE(20), size)
  }
})

test('installed dashboard offers a new build and respects Later until the version changes', async () => {
  const { JSDOM } = await import('jsdom')
  const code = await readFile(new URL('../public/tali-app-update.js', import.meta.url), 'utf8')
  const { window } = new JSDOM(
    '<meta name="talitrade-app-version" content="first"><aside id="appUpdateNotice" hidden><button data-update-now></button><button data-update-later></button></aside>',
    { runScripts: 'outside-only', pretendToBeVisual: true },
  )
  try {
    Object.defineProperty(window.navigator, 'standalone', { value: true })
    let version = 'first'
    let checks = 0
    window.fetch = async (path, options) => {
      assert.equal(path, '/talitrade-app-version.json')
      assert.equal(options.cache, 'no-store')
      checks++
      return { ok: true, json: async () => ({ version }) }
    }
    const settled = () => new Promise(resolve => setImmediate(resolve))
    const notice = window.document.getElementById('appUpdateNotice')
    const check = async () => {
      window.document.dispatchEvent(new window.Event('visibilitychange'))
      await settled()
    }

    window.eval(code)
    await settled()
    assert.ok(checks > 0)
    assert.equal(notice.hidden, true)

    version = 'second'
    await check()
    assert.equal(notice.hidden, false)
    notice.querySelector('[data-update-later]').click()
    await check()
    assert.equal(notice.hidden, true)

    version = 'third'
    await check()
    assert.equal(notice.hidden, false)
  } finally {
    window.close()
  }
})

test('the third import bar fills the full length of its track', async () => {
  const source = await readFile(new URL('../public/tali-account-setup.js', import.meta.url), 'utf8')
  assert.match(source, /class="su-track" d="M153 25h14m-14 7h14m-14 7h14"/)
  assert.match(source, /class="su-row su-row-third" d="M153 39h14"/)
})
