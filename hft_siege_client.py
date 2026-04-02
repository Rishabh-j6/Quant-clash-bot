"""
HFT Siege Python SDK

Provides a WebSocket client for connecting bots to the HFT Siege competition
platform. Handles authentication, reconnection with exponential backoff,
message parsing, and a clean callback API.

Requirements:
    pip install websockets

Example usage:
    See example_bot.py
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Message types (Server → Client)
# ---------------------------------------------------------------------------

# not a real server msg; auth ack is implicit
MSG_AUTH_RESPONSE = "auth_response"
MSG_ORDER_RESPONSE = "order_response"
MSG_TRADE = "trade"
MSG_PRICE_UPDATE = "price_update"
MSG_LEADERBOARD = "leaderboard"
MSG_NEWS_FLASH = "news_flash"
MSG_FRAUD_ALERT = "fraud_alert"
MSG_ERROR = "error"
MSG_HEARTBEAT = "heartbeat"
MSG_ROUND_END = "round_end"
MSG_ROUND_STATUS = "round_status"
MSG_WALLET_UPDATE = "wallet_update"

# Message types (Client → Server)
MSG_AUTH = "auth"
MSG_ORDER_SUBMIT = "order_submit"
MSG_ORDER_CANCEL = "order_cancel"


# ---------------------------------------------------------------------------
# Data classes for parsed payloads
# ---------------------------------------------------------------------------

@dataclass
class PriceTick:
    ticker: str
    price: int        # integer cents, e.g. 18250 = $182.50
    timestamp_ms: int

    @property
    def price_dollars(self) -> float:
        return self.price / 100.0


@dataclass
class Trade:
    trade_id: int
    ticker: str
    price: int
    quantity: int
    buyer_id: str
    seller_id: str
    timestamp_ms: int

    @property
    def price_dollars(self) -> float:
        return self.price / 100.0


@dataclass
class OrderResponse:
    order_id: int
    success: bool
    message: str
    trades: list[dict] = field(default_factory=list)


@dataclass
class LeaderboardEntry:
    participant_id: str
    net_worth: int    # integer cents
    cash: int         # integer cents
    rank: int = 0

    @property
    def net_worth_dollars(self) -> float:
        return self.net_worth / 100.0


@dataclass
class NewsEvent:
    id: str
    headline: str
    ticker: str        # empty string means macro event (all tickers)
    sentiment_score: float   # [-1.0, +1.0]
    event_type: str    # "macro" or "micro"


@dataclass
class WalletUpdate:
    cash: int          # integer cents
    positions: dict[str, int]  # ticker → quantity
    net_worth: int     # integer cents

    @property
    def cash_dollars(self) -> float:
        return self.cash / 100.0


@dataclass
class RoundStatus:
    state: str         # "Lobby", "Active", "Paused", "Ended"
    remaining_seconds: int


# ---------------------------------------------------------------------------
# Client configuration
# ---------------------------------------------------------------------------

@dataclass
class ClientConfig:
    url: str                          # e.g. "ws://localhost:8081/ws"
    username: str
    password: str
    reconnect: bool = True
    reconnect_max_attempts: int = 10
    reconnect_base_delay_s: float = 1.0
    reconnect_max_delay_s: float = 30.0
    ping_interval_s: float = 20.0


# ---------------------------------------------------------------------------
# HFTSiegeClient
# ---------------------------------------------------------------------------

class HFTSiegeClient:
    """
    Async WebSocket client for the HFT Siege competition platform.

    Usage pattern:
        client = HFTSiegeClient(config)

        @client.on_price_update
        async def handle_price(tick: PriceTick):
            ...

        await client.connect()
    """

    def __init__(self, config: ClientConfig):
        self._cfg = config
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._connected = False
        self._send_lock = asyncio.Lock()

        # Callbacks — registered via decorators or set directly.
        self._on_price_update:  Optional[Callable] = None
        self._on_trade:         Optional[Callable] = None
        self._on_order_response: Optional[Callable] = None
        self._on_leaderboard:   Optional[Callable] = None
        self._on_news:          Optional[Callable] = None
        self._on_fraud_alert:   Optional[Callable] = None
        self._on_round_end:     Optional[Callable] = None
        self._on_round_status:  Optional[Callable] = None
        self._on_wallet_update: Optional[Callable] = None
        self._on_error:         Optional[Callable] = None
        self._on_connected:     Optional[Callable] = None
        self._on_disconnected:  Optional[Callable] = None

    # ------------------------------------------------------------------
    # Decorator-style callback registration
    # ------------------------------------------------------------------

    def on_price_update(self, fn: Callable) -> Callable:
        self._on_price_update = fn
        return fn

    def on_trade(self, fn: Callable) -> Callable:
        self._on_trade = fn
        return fn

    def on_order_response(self, fn: Callable) -> Callable:
        self._on_order_response = fn
        return fn

    def on_leaderboard(self, fn: Callable) -> Callable:
        self._on_leaderboard = fn
        return fn

    def on_news(self, fn: Callable) -> Callable:
        self._on_news = fn
        return fn

    def on_fraud_alert(self, fn: Callable) -> Callable:
        self._on_fraud_alert = fn
        return fn

    def on_round_end(self, fn: Callable) -> Callable:
        self._on_round_end = fn
        return fn

    def on_round_status(self, fn: Callable) -> Callable:
        self._on_round_status = fn
        return fn

    def on_wallet_update(self, fn: Callable) -> Callable:
        self._on_wallet_update = fn
        return fn

    def on_error(self, fn: Callable) -> Callable:
        self._on_error = fn
        return fn

    def on_connected(self, fn: Callable) -> Callable:
        self._on_connected = fn
        return fn

    def on_disconnected(self, fn: Callable) -> Callable:
        self._on_disconnected = fn
        return fn

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """
        Connect to the server and start the message loop.
        If reconnect=True, retries on disconnect with exponential backoff.
        Blocks until the connection is closed and reconnect is exhausted.
        """
        attempt = 0
        while True:
            try:
                logger.info("Connecting to %s (attempt %d)",
                            self._cfg.url, attempt + 1)
                async with websockets.connect(
                    self._cfg.url,
                    ping_interval=self._cfg.ping_interval_s,
                ) as ws:
                    self._ws = ws
                    attempt = 0  # reset on successful connection
                    await self._authenticate()
                    self._connected = True
                    if self._on_connected:
                        await self._invoke(self._on_connected)
                    await self._read_loop()

            except ConnectionClosed as exc:
                logger.warning("Connection closed: %s", exc)
            except OSError as exc:
                logger.warning("Connection failed: %s", exc)
            finally:
                self._connected = False
                self._ws = None
                if self._on_disconnected:
                    await self._invoke(self._on_disconnected)

            if not self._cfg.reconnect:
                break

            attempt += 1
            if attempt > self._cfg.reconnect_max_attempts:
                logger.error("Max reconnect attempts (%d) reached, giving up.",
                             self._cfg.reconnect_max_attempts)
                break

            delay = min(
                self._cfg.reconnect_base_delay_s * (2 ** (attempt - 1)),
                self._cfg.reconnect_max_delay_s,
            )
            logger.info("Reconnecting in %.1fs...", delay)
            await asyncio.sleep(delay)

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Order API
    # ------------------------------------------------------------------

    async def submit_limit_order(
        self,
        ticker: str,
        side: str,       # "BUY" or "SELL"
        price: int,      # integer cents
        quantity: int,
    ) -> None:
        """Submit a limit order. Non-blocking; result delivered via on_order_response."""
        await self._send(MSG_ORDER_SUBMIT, {
            "ticker": ticker,
            "side": side.upper(),
            "order_type": "LIMIT",
            "price": price,
            "quantity": quantity,
        })

    async def submit_market_order(
        self,
        ticker: str,
        side: str,       # "BUY" or "SELL"
        quantity: int,
    ) -> None:
        """Submit a market order. Non-blocking; result delivered via on_order_response."""
        await self._send(MSG_ORDER_SUBMIT, {
            "ticker": ticker,
            "side": side.upper(),
            "order_type": "MARKET",
            "price": 0,
            "quantity": quantity,
        })

    async def cancel_order(self, ticker: str, order_id: int) -> None:
        """Cancel an open order."""
        await self._send(MSG_ORDER_CANCEL, {
            "ticker": ticker,
            "order_id": order_id,
        })

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _authenticate(self) -> None:
        payload = {
            "username": self._cfg.username,
            "password": self._cfg.password,
        }
        await self._send_raw(MSG_AUTH, payload)
        logger.info("Sent auth for user %s", self._cfg.username)

    async def _read_loop(self) -> None:
        async for raw in self._ws:
            try:
                env = json.loads(raw)
                await self._dispatch(env)
            except json.JSONDecodeError:
                logger.warning("Received non-JSON message: %r", raw)
            except Exception as exc:
                logger.exception("Error dispatching message: %s", exc)

    async def _dispatch(self, env: dict) -> None:
        msg_type = env.get("type", "")
        payload = env.get("payload", {})

        if msg_type == MSG_PRICE_UPDATE and self._on_price_update:
            tick = PriceTick(
                ticker=payload.get("ticker", ""),
                price=payload.get("price", 0),
                timestamp_ms=env.get("ts", 0),
            )
            await self._invoke(self._on_price_update, tick)

        elif msg_type == MSG_TRADE and self._on_trade:
            trade = Trade(
                trade_id=payload.get("trade_id", 0),
                ticker=payload.get("ticker", ""),
                price=payload.get("price", 0),
                quantity=payload.get("quantity", 0),
                buyer_id=payload.get("buyer_id", ""),
                seller_id=payload.get("seller_id", ""),
                timestamp_ms=env.get("ts", 0),
            )
            await self._invoke(self._on_trade, trade)

        elif msg_type == MSG_ORDER_RESPONSE and self._on_order_response:
            resp = OrderResponse(
                order_id=payload.get("order_id", 0),
                success=payload.get("success", False),
                message=payload.get("message", ""),
                trades=payload.get("trades", []),
            )
            await self._invoke(self._on_order_response, resp)

        elif msg_type == MSG_LEADERBOARD and self._on_leaderboard:

            # ✅ Handle inconsistent server payload formats
            if isinstance(payload, list):
                raw_entries = payload
            elif isinstance(payload, dict):
                raw_entries = payload.get("entries", [])
            else:
                raw_entries = []

            entries = []
            for i, e in enumerate(raw_entries):
                if isinstance(e, dict):  # safety check
                    entries.append(
                        LeaderboardEntry(
                            participant_id=e.get("participant_id", ""),
                            net_worth=e.get("net_worth", 0),
                            cash=e.get("cash", 0),
                            rank=i + 1,
                        )
                    )

        elif msg_type == MSG_NEWS_FLASH and self._on_news:
            event = NewsEvent(
                id=payload.get("id", ""),
                headline=payload.get("headline", ""),
                ticker=payload.get("ticker", ""),
                sentiment_score=payload.get("sentiment_score", 0.0),
                event_type=payload.get("event_type", ""),
            )
            await self._invoke(self._on_news, event)

        elif msg_type == MSG_FRAUD_ALERT and self._on_fraud_alert:
            await self._invoke(self._on_fraud_alert, payload)

        elif msg_type == MSG_ROUND_END and self._on_round_end:
            await self._invoke(self._on_round_end, payload)

        elif msg_type == MSG_ROUND_STATUS and self._on_round_status:
            status = RoundStatus(
                state=payload.get("state", ""),
                remaining_seconds=payload.get("remaining_seconds", 0),
            )
            await self._invoke(self._on_round_status, status)

        elif msg_type == MSG_WALLET_UPDATE and self._on_wallet_update:
            update = WalletUpdate(
                cash=payload.get("cash", 0),
                positions=payload.get("positions", {}),
                net_worth=payload.get("net_worth", 0),
            )
            await self._invoke(self._on_wallet_update, update)

        elif msg_type == MSG_ERROR and self._on_error:
            await self._invoke(self._on_error, payload.get("message", str(payload)))

        elif msg_type == MSG_HEARTBEAT:
            pass  # nothing to do

    async def _send(self, msg_type: str, payload: dict) -> None:
        await self._send_raw(msg_type, payload)

    async def _send_raw(self, msg_type: str, payload: dict) -> None:
        if self._ws is None:
            raise RuntimeError("Not connected")
        envelope = {
            "type": msg_type,
            "payload": payload,
            "ts": int(time.time() * 1000),
        }
        async with self._send_lock:
            await self._ws.send(json.dumps(envelope))

    @staticmethod
    async def _invoke(fn: Callable, *args: Any) -> None:
        result = fn(*args)
        if asyncio.iscoroutine(result):
            await result
