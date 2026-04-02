import asyncio
import collections
import logging
import os
import time

from hft_siege_client import (
    ClientConfig,
    HFTSiegeClient,
    LeaderboardEntry,
    NewsEvent,
    OrderResponse,
    PriceTick,
    RoundStatus,
    Trade,
    WalletUpdate,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")


WS_URL = os.getenv("WS_URL", "ws://10.220.147.182:8081/ws")
USERNAME = os.getenv("USERNAME", "rishabh")
PASSWORD = os.getenv("PASSWORD", "r_shabh222")

ORDER_SIZE = int(os.getenv("ORDER_SIZE", "5"))
WINDOW_SIZE = 10  # reduced (faster reaction)
MOMENTUM_THRESHOLD = 0.005
MAX_POSITION = 30
MIN_POSITION = 0
COOLDOWN_SECS = 0.5  # faster trading


price_windows: dict[str, collections.deque] = {}
positions: dict[str, int] = {}
cash: int = 0
net_worth: int = 0
round_active: bool = False
last_trade_time: dict[str, float] = {}

sentiment_bias: dict[str, float] = collections.defaultdict(lambda: 1.0)

cfg = ClientConfig(
    url=WS_URL,
    username=USERNAME,
    password=PASSWORD,
    reconnect=True,
)
client = HFTSiegeClient(cfg)


def on_cooldown(ticker: str) -> bool:
    return (time.time() - last_trade_time.get(ticker, 0)) < COOLDOWN_SECS


def record_trade(ticker: str) -> None:
    last_trade_time[ticker] = time.time()


async def evaluate(ticker: str, price: int) -> None:
    if not round_active:
        return
    if on_cooldown(ticker):
        return

    window = price_windows.get(ticker)
    if not window or len(window) < WINDOW_SIZE:
        return

    low = min(window)
    high = max(window)
    pos = positions.get(ticker, 0)
    bias = sentiment_bias[ticker]
    thresh = MOMENTUM_THRESHOLD * bias

    # DEBUG (safe)
    log.debug(
        f"{ticker} price={price} low={low} high={high} pos={pos} cash={cash}")

    #  BUY breakout (FIXED LOGIC)
    if price > high * (1 + thresh) and pos < MAX_POSITION:
        qty = min(ORDER_SIZE, MAX_POSITION - pos)
        cost = price * qty

        if qty > 0 and cost <= cash:
            log.info(f"🟢 BUY  {qty}×{ticker} @ ${price/100:.2f}")
            await client.submit_limit_order(ticker, "BUY", price, qty)
            record_trade(ticker)

    #  SELL breakdown (FIXED LOGIC)
    elif price < low * (1 - thresh) and pos > MIN_POSITION:
        qty = min(ORDER_SIZE, pos - MIN_POSITION)

        if qty > 0:
            log.info(f"🔴 SELL {qty}×{ticker} @ ${price/100:.2f}")
            await client.submit_limit_order(ticker, "SELL", price, qty)
            record_trade(ticker)


@client.on_connected
async def handle_connected():
    log.info(f"✅ Connected as '{USERNAME}'")


@client.on_disconnected
async def handle_disconnected():
    log.warning("⚡ Disconnected — SDK will reconnect automatically")


@client.on_round_status
async def handle_round_status(status: RoundStatus):
    global round_active
    was_active = round_active
    round_active = (status.state == "Active")

    if round_active and not was_active:
        log.info(f"\n{'═'*45}")
        log.info(f"  🟢 ROUND ACTIVE — {status.remaining_seconds}s remaining")
        log.info(f"{'═'*45}\n")
    elif not round_active and was_active:
        log.info(f"⏸️ Round {status.state} — trading paused")


@client.on_wallet_update
async def handle_wallet(update: WalletUpdate):
    global cash, net_worth
    cash = update.cash
    net_worth = update.net_worth

    for ticker, qty in update.positions.items():
        positions[ticker] = qty

    log.info(f"💰 Cash: ${cash/100:.2f} | Net Worth: ${net_worth/100:.2f}")


@client.on_price_update
async def handle_price(tick: PriceTick):
    print("Price recieved")
    ticker = tick.ticker
    price = tick.price

    # DEBUG price flow
    # log.info(f"PRICE {ticker}: {price}")

    if ticker not in price_windows:
        price_windows[ticker] = collections.deque(maxlen=WINDOW_SIZE)

    price_windows[ticker].append(price)

    await evaluate(ticker, price)


@client.on_order_response
async def handle_order_response(resp: OrderResponse):
    if not resp.success:
        log.warning(f"⚠️ Order {resp.order_id} rejected: {resp.message}")


@client.on_trade
async def handle_trade(trade: Trade):
    nw = net_worth / 100
    log.info(
        f"✅ FILLED {trade.quantity}×{trade.ticker} @ ${trade.price_dollars:.2f} | Net: ${nw:,.2f}")


@client.on_news
async def handle_news(event: NewsEvent):
    score = event.sentiment_score
    log.info(
        f"📰 [{event.ticker or 'MACRO'}] {score:+.2f} — {event.headline[:55]}")

    if event.ticker:
        old = sentiment_bias[event.ticker]
        sentiment_bias[event.ticker] = max(0.3, 1.0 - abs(score) * 0.5)
        log.info(
            f"   Bias {event.ticker}: {old:.2f} → {sentiment_bias[event.ticker]:.2f}")
    else:
        for ticker in price_windows:
            sentiment_bias[ticker] = max(0.3, 1.0 - abs(score) * 0.3)
        log.info("   All biases adjusted")


@client.on_leaderboard
async def handle_leaderboard(entries: list[LeaderboardEntry]):
    log.info(f"\n── LEADERBOARD ──────────────────────")
    for e in entries[:5]:
        marker = " ← YOU" if e.participant_id == USERNAME else ""
        log.info(
            f"#{e.rank} {e.participant_id:<20} ${e.net_worth_dollars:>12,.2f}{marker}")
    log.info(f"────────────────────────────────────\n")


@client.on_round_end
async def handle_round_end(payload: dict):
    log.info(f"\n{'═'*45}")
    log.info(f"🏁 ROUND ENDED — Winner: {payload.get('winner', '?')}")
    log.info(f"{'═'*45}\n")


@client.on_fraud_alert
async def handle_fraud_alert(payload: dict):
    log.warning("⛔ FRAUD ALERT — frozen 60s!")


@client.on_error
async def handle_error(message: str):
    log.error(f"Server error: {message}")


if __name__ == "__main__":
    log.info(f"\n{'═'*45}")
    log.info(f"🤖 HFT SIEGE BOT")
    log.info(f"Server : {WS_URL}")
    log.info(f"Team   : {USERNAME}")
    log.info(f"Window : {WINDOW_SIZE} | Thresh: {MOMENTUM_THRESHOLD*100:.1f}%")
    log.info(f"MaxPos : {MAX_POSITION} | Cooldown: {COOLDOWN_SECS}s")
    log.info(f"{'═'*45}\n")

    asyncio.run(client.connect())
