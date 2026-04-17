export function formatSyncRunDiagnostics(run) {
  const detail = run?.result_detail && typeof run.result_detail === 'object' ? run.result_detail : {}
  const counts = detail?.counts && typeof detail.counts === 'object' ? detail.counts : {}
  const statusDetail = detail?.status_detail || null
  const errorCode = detail?.error_code || null
  const errorCategory = detail?.error_category || null
  const resultCategory = detail?.result_category || null
  const isTransient = typeof detail?.is_transient === 'boolean' ? detail.is_transient : null

  const summaryParts = []
  if (statusDetail) summaryParts.push(statusDetail)
  if (typeof counts.accounts_total === 'number') summaryParts.push(`accounts ${counts.accounts_fresh || 0}/${counts.accounts_total} fresh`)
  if (typeof counts.open_positions === 'number') summaryParts.push(`open positions ${counts.open_positions}`)
  if (typeof counts.trades_24h === 'number') summaryParts.push(`trades (24h) ${counts.trades_24h}`)

  const summary = summaryParts.join(' · ')

  return {
    summary,
    resultCategory,
    errorCode,
    errorCategory,
    isTransient,
  }
}
