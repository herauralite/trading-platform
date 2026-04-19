import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

async function readFrontendFile(relPath) {
  return readFile(new URL(`./${relPath}`, import.meta.url), 'utf8')
}

test('connections focused workflow panel distinguishes selected account vs route-focused account truthfully', async () => {
  const source = await readFrontendFile('pages/ConnectionsPage.jsx')

  assert.equal(source.includes('connections-workflow-focus-panel'), true)
  assert.equal(source.includes('Route-focused account'), true)
  assert.equal(source.includes('Selected account'), true)
  assert.equal(source.includes('Provider-focused workflow'), true)
  assert.equal(source.includes('Truth note:'), true)
  assert.equal(source.includes('Route-focused account not found in current workspace records.'), true)
})

test('focused workflow CTA language remains state-truthful and supports guided add-account handoff', async () => {
  const source = await readFrontendFile('pages/ConnectionsPage.jsx')

  assert.equal(source.includes('View linked accounts'), true)
  assert.equal(source.includes('Refresh sync'), true)
  assert.equal(source.includes('Continue setup'), true)
  assert.equal(source.includes('Finish required configuration'), true)
  assert.equal(source.includes('Reconnect provider'), true)
  assert.equal(source.includes('Re-run sync after reconnect'), true)
  assert.equal(source.includes("onAddAccount(inboundIntent.provider)"), true)
})
