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
  // Production fallback is the API domain (fronted by the Cloudflare proxy), not a provider-specific host.
  assert.equal(fallbackResolver.resolveApiBase(), 'https://api.talitrade.com')
  assert.equal(/railway\.app/.test(fallbackResolver.resolveApiBase()), false)
})

test('api base resolver only returns blank when same-origin proxy is explicitly enabled', async () => {
  const resolver = await loadResolverWithConfig({ sameOriginApiProxy: true })
  assert.equal(resolver.resolveApiBase(), '')
})

test('app auth flow uses shared TaliApiBase backend strategy', async () => {
  const appHtml = await readFile(new URL('../app.html', import.meta.url), 'utf8')
  const publicAppHtml = await readFile(new URL('../public/app.html', import.meta.url), 'utf8')


  assert.equal(appHtml.includes("window.TaliApiBase.buildApiUrl('/auth/telegram/config')"), true)
  assert.equal(appHtml.includes('return window.TaliApiBase.resolveApiBase();'), true)

  assert.equal(publicAppHtml.includes("window.TaliApiBase.buildApiUrl('/auth/telegram/config')"), true)
  assert.equal(publicAppHtml.includes('return window.TaliApiBase.resolveApiBase();'), true)
})

test('shared resolver builds auth + waitlist URLs against the same backend origin', async () => {
  const resolver = await loadResolverWithConfig({})
  assert.equal(resolver.buildApiUrl('/auth/telegram/config'), 'https://api.talitrade.com/auth/telegram/config')
  assert.equal(resolver.buildApiUrl('/waitlist'), 'https://api.talitrade.com/waitlist')
})
