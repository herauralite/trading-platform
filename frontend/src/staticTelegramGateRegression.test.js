import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const STATIC_GATE_FILES = ['app.html', 'public/app.html']

for (const relPath of STATIC_GATE_FILES) {
  test(`static telegram gate keeps token-first + fallback contract in ${relPath}`, async () => {
    const html = await readFile(new URL(`../${relPath}`, import.meta.url), 'utf8')

    assert.equal(html.includes("const token = String(data.access_token || '')"), true)
    assert.equal(html.includes("if (!token) throw new Error('missing_session_token')"), true)

    assert.equal(
      html.includes('normalizeTaliUserShape(data.user) || normalizeTaliUserShape(normalizeWidgetUserShape(widgetUser))')
        || html.includes('normalizeTaliUserShape(data.user) || normalizeTaliUserShape(normalizeWidgetUserShape(tgUser))'),
      true,
    )

    assert.equal(html.includes('/auth/me'), true)
    assert.equal(html.includes("if (!user) throw new Error('missing_or_invalid_user')"), true)
    assert.equal(html.includes("setGateTelegramError('Authentication failed. Please try again.')"), true)
  })
}
