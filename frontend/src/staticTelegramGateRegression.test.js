import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

test('app entrypoint is the canonical React shell bootstrap', async () => {
  const html = await readFile(new URL('../app.html', import.meta.url), 'utf8')

  assert.equal(html.includes('data-app-shell="canonical"'), true)
  assert.equal(html.includes('<div id="root"></div>'), true)
  assert.equal(html.includes('src="/src/main.jsx"'), true)
})
