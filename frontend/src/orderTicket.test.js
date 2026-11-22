import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { JSDOM } from 'jsdom'
const app = fs.readFileSync(new URL('../public/app.html', import.meta.url), 'utf8')
test('inline scripts parse and ticket review cannot claim or submit a real order', () => {
  for (const m of app.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)) {
    if (!m[1].includes('src=') && !m[1].includes('application/')) new Function(m[2])
  }
  const start = app.indexOf("document.getElementById('tabBuy').addEventListener")
  const end = app.indexOf('function updateDepth()', start)
  const ticket = app.slice(start,end)
  const dom = new JSDOM(app, {runScripts:'outside-only'})
  dom.window.eval("let orderSide='buy',lotSize=1,slPrice=0,tpPrice=0,currentPrice=0,orderTypeIdx=0; const orderTypes=['Market']; const fmtP=String;" + ticket)
  dom.window.document.getElementById('tabSell').click()
  const review = dom.window.document.getElementById('execBtn')
  assert.equal(review.textContent,'Review SELL')
  review.click()
  assert.match(dom.window.document.getElementById('orderAccessStatus').textContent,/no order was sent/)
  assert.doesNotMatch(ticket,/Order Placed|fetch\(|XMLHttpRequest/)
  dom.window.close()
})
