import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
const app = fs.readFileSync(new URL('../public/app.html', import.meta.url), 'utf8')
test('inline scripts parse and read-only dashboard exposes no order ticket or synthetic depth', () => {
  for (const m of app.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)) {
    if (!m[1].includes('src=') && !m[1].includes('application/')) new Function(m[2])
  }
  assert.doesNotMatch(app, /id="(?:orderPanel|depthCard|execBtn|tabBuy|tabSell)"|function updateDepth/)
})
