(function () {
  const installed = navigator.standalone === true || window.matchMedia('(display-mode: standalone)').matches
  const current = document.querySelector('meta[name="talitrade-app-version"]')?.content
  const notice = document.getElementById('appUpdateNotice')
  if (!installed || !current || !notice) return

  let available = ''
  let dismissed = ''
  let checking = false

  async function checkForUpdate() {
    if (checking || document.visibilityState !== 'visible') return
    checking = true
    try {
      const response = await fetch('/talitrade-app-version.json', { cache: 'no-store' })
      if (!response.ok) return
      const { version } = await response.json()
      if (typeof version !== 'string' || !version) return

      available = version !== current ? version : ''
      notice.hidden = !available || available === dismissed
    } catch {
      // A temporary network failure should not affect the dashboard.
    } finally {
      checking = false
    }
  }

  notice.querySelector('[data-update-now]').addEventListener('click', () => window.location.reload())
  notice.querySelector('[data-update-later]').addEventListener('click', () => {
    dismissed = available
    notice.hidden = true
  })

  document.addEventListener('visibilitychange', checkForUpdate)
  window.addEventListener('pageshow', checkForUpdate)
  window.setInterval(checkForUpdate, 5 * 60 * 1000)
  checkForUpdate()
})()
