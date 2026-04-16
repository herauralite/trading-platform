import { useMemo, useState } from 'react'
import axios from 'axios'

const API = 'https://trading-platform-production-70e0.up.railway.app'

function App() {
  const [telegramUserId, setTelegramUserId] = useState('123456789')
  const [catalog, setCatalog] = useState([])
  const [connectors, setConnectors] = useState([])
  const [status, setStatus] = useState('Enter a Telegram user id and load connectors')
  const [manualAccount, setManualAccount] = useState({
    externalAccountId: '',
    brokerName: 'Manual',
    displayLabel: '',
    accountType: 'demo',
    accountSize: 10000
  })
  const [manualTrade, setManualTrade] = useState({
    externalAccountId: '',
    symbol: 'NAS100',
    side: 'buy',
    size: 0.1,
    entryPrice: 15000,
    exitPrice: 15025,
    pnl: 25
  })
  const [csvInput, setCsvInput] = useState('[{"symbol":"US30","side":"buy","open_time":"2026-04-16T10:00:00Z","close_time":"2026-04-16T10:10:00Z","pnl":18}]')
  const [csvAccount, setCsvAccount] = useState('csv-account-1')

  const allAccounts = useMemo(
    () => connectors.flatMap((connector) =>
      connector.accounts.map((account) => ({ ...account, connector_type: connector.connector_type }))
    ),
    [connectors]
  )

  const sourceLabel = (connectorType) => {
    if (connectorType === 'fundingpips_extension') return 'FundingPips Connector'
    if (connectorType === 'csv_import') return 'CSV Import'
    if (connectorType === 'manual') return 'Manual Journal'
    return connectorType
  }

  const formatDate = (dateText) => (dateText ? new Date(dateText).toLocaleString() : '—')

  async function loadConnectorData() {
    try {
      const [catalogRes, overviewRes] = await Promise.all([
        axios.get(`${API}/connectors/catalog`),
        axios.get(`${API}/connectors/overview`, { params: { telegram_user_id: telegramUserId } })
      ])
      setCatalog(catalogRes.data.connectors || [])
      setConnectors(overviewRes.data.connectors || [])
      setStatus(`Loaded ${overviewRes.data.count || 0} connected source(s)`)
    } catch (e) {
      setStatus(`Failed to load connectors: ${e.message}`)
    }
  }

  async function createManualAccount() {
    try {
      await axios.post(`${API}/ingest/accounts`, {
        user_id: telegramUserId,
        connector_type: 'manual',
        broker_name: manualAccount.brokerName,
        external_account_id: manualAccount.externalAccountId,
        display_label: manualAccount.displayLabel || `Manual ${manualAccount.externalAccountId}`,
        account_type: manualAccount.accountType,
        account_size: Number(manualAccount.accountSize) || null
      })
      setStatus('Manual account created')
      setManualTrade((prev) => ({ ...prev, externalAccountId: manualAccount.externalAccountId }))
      await loadConnectorData()
    } catch (e) {
      setStatus(`Manual account failed: ${e.message}`)
    }
  }

  async function createManualTrade() {
    try {
      await axios.post(`${API}/ingest/trades`, {
        user_id: telegramUserId,
        connector_type: 'manual',
        external_account_id: manualTrade.externalAccountId,
        symbol: manualTrade.symbol,
        side: manualTrade.side,
        size: Number(manualTrade.size),
        entry_price: Number(manualTrade.entryPrice),
        exit_price: Number(manualTrade.exitPrice),
        pnl: Number(manualTrade.pnl),
        import_provenance: { entry_mode: 'ui_manual' },
        source_metadata: { created_from: 'manual_journal_panel' }
      })
      setStatus('Manual trade recorded')
      await loadConnectorData()
    } catch (e) {
      setStatus(`Manual trade failed: ${e.message}`)
    }
  }

  async function importCsvTrades() {
    try {
      const rows = JSON.parse(csvInput)
      await axios.post(`${API}/ingest/csv/trades`, {
        user_id: telegramUserId,
        connector_type: 'csv_import',
        broker_name: 'csv',
        external_account_id: csvAccount,
        rows
      })
      setStatus(`Imported ${rows.length} CSV trade row(s)`)
      await loadConnectorData()
    } catch (e) {
      setStatus(`CSV import failed: ${e.message}`)
    }
  }

  return (
    <div className="app">
      <h1>TaliTrade Platform Console</h1>
      <p>Status: {status}</p>
      <section className="panel">
        <h2>Workspace</h2>
        <label>Telegram user id</label>
        <input value={telegramUserId} onChange={(e) => setTelegramUserId(e.target.value)} />
        <button onClick={loadConnectorData}>Load connectors</button>
      </section>

      <section className="panel">
        <h2>Connector Management</h2>
        <p>Available connectors: {catalog.map((c) => c.label).join(', ') || '—'}</p>
        {connectors.map((connector) => (
          <div key={connector.connector_type} className="card">
            <div className="row">
              <strong>{sourceLabel(connector.connector_type)}</strong>
              <span className="badge">{connector.status}</span>
            </div>
            <div className="meta">
              Accounts: {connector.account_count} · Last activity: {formatDate(connector.last_activity_at)} · Last sync: {formatDate(connector.last_sync_at)}
            </div>
            <ul>
              {connector.accounts.map((account) => (
                <li key={`${connector.connector_type}-${account.id}`}>
                  <span>{account.display_label || account.external_account_id}</span>
                  <span className="pill">{sourceLabel(connector.connector_type)}</span>
                  <span className="pill">{account.broker_name || 'Unknown broker'}</span>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </section>

      <section className="panel">
        <h2>Non-extension path: Manual Journal</h2>
        <div className="row">
          <input placeholder="External account id" value={manualAccount.externalAccountId} onChange={(e) => setManualAccount({ ...manualAccount, externalAccountId: e.target.value })} />
          <input placeholder="Display label" value={manualAccount.displayLabel} onChange={(e) => setManualAccount({ ...manualAccount, displayLabel: e.target.value })} />
          <button onClick={createManualAccount}>Create manual account</button>
        </div>
        <div className="row">
          <input placeholder="Manual account id" value={manualTrade.externalAccountId} onChange={(e) => setManualTrade({ ...manualTrade, externalAccountId: e.target.value })} />
          <input placeholder="Symbol" value={manualTrade.symbol} onChange={(e) => setManualTrade({ ...manualTrade, symbol: e.target.value })} />
          <select value={manualTrade.side} onChange={(e) => setManualTrade({ ...manualTrade, side: e.target.value })}>
            <option value="buy">buy</option>
            <option value="sell">sell</option>
          </select>
          <input type="number" placeholder="PnL" value={manualTrade.pnl} onChange={(e) => setManualTrade({ ...manualTrade, pnl: e.target.value })} />
          <button onClick={createManualTrade}>Record trade</button>
        </div>
      </section>

      <section className="panel">
        <h2>Alternative non-extension path: CSV Import</h2>
        <div className="row">
          <input value={csvAccount} onChange={(e) => setCsvAccount(e.target.value)} placeholder="CSV account id" />
          <button onClick={importCsvTrades}>Import JSON rows as CSV trades</button>
        </div>
        <textarea rows={5} value={csvInput} onChange={(e) => setCsvInput(e.target.value)} />
      </section>

      <section className="panel">
        <h2>All Accounts (source-aware)</h2>
        <ul>
          {allAccounts.map((account) => (
            <li key={`${account.connector_type}-${account.id}`}>
              {account.display_label || account.external_account_id}
              {' · '}
              <span className="pill">{sourceLabel(account.connector_type)}</span>
              <span className="pill">{account.broker_name || 'Unknown broker'}</span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}

export default App
