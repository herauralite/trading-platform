import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

test('legacy public/app.html stays a shim redirect into canonical /app shell', async () => {
  const html = await readFile(new URL('../public/app.html', import.meta.url), 'utf8')

  assert.equal(html.includes('redirectToCanonicalAppShell'), true)
  assert.equal(html.includes("window.location.replace(target)"), true)
  assert.equal(html.includes("var target = '/app' + window.location.search + window.location.hash;"), true)
  assert.equal(html.includes('<a href="/app">Continue</a>'), true)
})
