import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const STATIC_SHIM_FILES = ['app.html', 'public/app.html']

for (const relPath of STATIC_SHIM_FILES) {
  test(`legacy app entrypoint delegates to canonical SPA shell in ${relPath}`, async () => {
    const html = await readFile(new URL(`../${relPath}`, import.meta.url), 'utf8')

    assert.equal(html.includes('redirectToCanonicalAppShell'), true)
    assert.equal(html.includes("window.location.replace(target)"), true)
    assert.equal(html.includes("var target = '/app' + window.location.search + window.location.hash;"), true)
    assert.equal(html.includes('<a href="/app">Continue</a>'), true)
  })
}
