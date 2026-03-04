import httpx
from dataclasses import dataclass

TIMEOUT = 10.0
PARTNER_ID = "1"


@dataclass
class AccountInfo:
    balance: float
    equity: float
    margin: float
    free_margin: float
    currency: str


@dataclass
class Position:
    ticket: int
    symbol: str
    side: str
    volume: float
    open_price: float
    current_price: float
    profit: float
    swap: float


class MatchTraderClient:
    def __init__(self, server_url: str, email: str, password: str):
        self.server_url = server_url.rstrip("/")
        self.email = email
        self.password = password
        self.trading_api_token = None
        self.trading_account_token = None

    async def authenticate(self) -> bool:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                f"{self.server_url}/mtr-core-edge/login",
                json={
                    "email": self.email,
                    "password": self.password,
                    "partnerId": PARTNER_ID,
                }
            )
            if resp.status_code == 200:
                data = resp.json()
                self.trading_api_token = data.get("tradingApiToken")
                account_token = data.get("tradingAccountToken", {})
                self.trading_account_token = account_token.get("token")
                return self.trading_api_token is not None
            return False

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.trading_api_token}",
            "trading-account-token": self.trading_account_token,
            "Content-Type": "application/json",
        }

    async def get_account(self) -> AccountInfo | None:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(
                f"{self.server_url}/mtr-core-edge/account",
                headers=self._headers()
            )
            if resp.status_code == 200:
                d = resp.json()
                return AccountInfo(
                    balance=d.get("balance", 0),
                    equity=d.get("equity", 0),
                    margin=d.get("margin", 0),
                    free_margin=d.get("freeMargin", 0),
                    currency=d.get("currency", "USD"),
                )
            return None

    async def get_positions(self) -> list[Position]:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(
                f"{self.server_url}/mtr-core-edge/positions",
                headers=self._headers()
            )
            if resp.status_code == 200:
                data = resp.json()
                positions = []
                items = data.get("positions", data if isinstance(data, list) else [])
                for p in items:
                    positions.append(Position(
                        ticket=p.get("ticket", 0),
                        symbol=p.get("symbol", ""),
                        side=p.get("side", p.get("type", "")),
                        volume=p.get("volume", 0),
                        open_price=p.get("openPrice", 0),
                        current_price=p.get("currentPrice", 0),
                        profit=p.get("profit", 0),
                        swap=p.get("swap", 0),
                    ))
                return positions
            return []