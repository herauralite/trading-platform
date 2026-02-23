import { useState } from 'react'
import axios from 'axios'

const API = 'http://127.0.0.1:8000'

function App() {
  const [token, setToken] = useState(null)
  const [accounts, setAccounts] = useState([])
  const [status, setStatus] = useState('Not logged in')

  async function testLogin() {
    try {
      const res = await axios.post(`${API}/auth/telegram`, {
        telegram_id: 123456789,
        first_name: 'Test',
        username: 'testuser',
        auth_date: 1708000000,
        hash: 'testhash',
        query_string: 'test'
      })
      setToken(res.data.access_token)
      setStatus(`Logged in — user ${res.data.user_id}`)
    } catch (e) {
      setStatus('Login failed: ' + e.message)
    }
  }

  async function loadAccounts() {
    try {
      const res = await axios.get(`${API}/accounts/`, {
        headers: { Authorization: `Bearer ${token}` }
      })
      setAccounts(res.data)
      setStatus(`Loaded ${res.data.length} accounts`)
    } catch (e) {
      setStatus('Failed: ' + e.message)
    }
  }

  return (
    <div style={{ padding: 40, fontFamily: 'monospace' }}>
      <h1>Trading Platform</h1>
      <p>Status: {status}</p>
      <button onClick={testLogin}>Test Login</button>
      {token && <button onClick={loadAccounts} style={{ marginLeft: 10 }}>Load Accounts</button>}
      {accounts.length > 0 && (
        <ul>{accounts.map(a => <li key={a.id}>{a.account_login}</li>)}</ul>
      )}
    </div>
  )
}

export default App
