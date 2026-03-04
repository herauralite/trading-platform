const BACKEND_URL = 'https://trading-platform-production-70e0.up.railway.app';

function extractData() {
  const text = document.body.innerText;
  
  const profitMatch = text.match(/Profit\s*([\d\.\-]+)\s*USD/);
  const profit = profitMatch ? parseFloat(profitMatch[1]) : 0;

  const accountMatch = text.match(/\b(1\d{6,7})\b/);
  const accountId = accountMatch ? accountMatch[1] : null;

  const hasPositions = !text.includes("You don't have any open positions");
  
  const positions = [];
  if (hasPositions) {
    const rows = document.querySelectorAll('[class*="position"], [class*="row"]');
    rows.forEach(row => {
      const rowText = row.innerText;
      if (rowText.includes('BUY') || rowText.includes('SELL')) {
        positions.push(rowText.trim());
      }
    });
  }

  return {
    profit,
    accountId,
    hasPositions,
    positions,
    timestamp: new Date().toISOString(),
    url: window.location.href
  };
}

async function sendData(data) {
  try {
    await fetch(`${BACKEND_URL}/extension/data`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data)
    });
  } catch (e) {
    console.log('TaliTrade: failed to send data', e);
  }
}

setInterval(() => {
  const data = extractData();
  console.log('TaliTrade data:', data);
  sendData(data);
}, 5000);

const data = extractData();
sendData(data);
 
