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
