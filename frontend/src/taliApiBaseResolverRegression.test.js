import test from 'node:test'
import assert from 'node:assert/strict'
import vm from 'node:vm'
import { readFile } from 'node:fs/promises'

async function loadResolverWithConfig(config = null) {
  const source = await readFile(new URL('../public/tali-api-base.js', import.meta.url), 'utf8')
  const context = {
    window: {
      __TALI_CONFIG__: config,
    },
  }
  vm.createContext(context)
  vm.runInContext(source, context)
  return context.window.TaliApiBase
}

test('api base resolution honors runtime, build-time, then known-good production fallback', async () => {
  const resolver = await loadResolverWithConfig({ apiBase: 'https://runtime.example.com/' })
  assert.equal(resolver.resolveApiBase(), 'https://runtime.example.com')

  const buildResolver = await loadResolverWithConfig({})
  assert.equal(buildResolver.resolveApiBase({ buildEnvApiBase: 'https://build.example.com/' }), 'https://build.example.com')

  const fallbackResolver = await loadResolverWithConfig({})
  assert.equal(
    fallbackResolver.resolveApiBase(),
    'https://trading-platform-production-0614.up.railway.app',
  )
  assert.notEqual(fallbackResolver.resolveApiBase(), 'https://api.talitrade.com')
})

test('api base resolver only returns blank when same-origin proxy is explicitly enabled', async () => {
  const resolver = await loadResolverWithConfig({ sameOriginApiProxy: true })
  assert.equal(resolver.resolveApiBase(), '')
})

test('homepage and app auth/demo flows use shared TaliApiBase backend strategy', async () => {
  const indexHtml = await readFile(new URL('../index.html', import.meta.url), 'utf8')
  const appHtml = await readFile(new URL('../app.html', import.meta.url), 'utf8')

  assert.equal(indexHtml.includes("window.TaliApiBase.buildApiUrl('/auth/telegram/config')"), true)
  assert.equal(indexHtml.includes("window.TaliApiBase.buildApiUrl('/demo/leaderboard')"), true)

  assert.equal(appHtml.includes('data-app-shell="canonical"'), true)
  assert.equal(appHtml.includes('src="/src/main.jsx"'), true)
})

test('shared resolver builds auth + leaderboard URLs against the same corrected backend origin', async () => {
  const resolver = await loadResolverWithConfig({})
  const authConfigUrl = resolver.buildApiUrl('/auth/telegram/config')
  const leaderboardUrl = resolver.buildApiUrl('/demo/leaderboard')

  assert.equal(authConfigUrl.startsWith('https://trading-platform-production-0614.up.railway.app/'), true)
  assert.equal(leaderboardUrl.startsWith('https://trading-platform-production-0614.up.railway.app/'), true)
})
