
import asyncio
import json
import time
import logging
import sys
import statistics
from collections import deque
import websockets


WS_URL = "ws://10.220.147.182:8081/ws"
USERNAME = "rishabh"
PASSWORD = "r_shabh222"

# Stocks available on the platform
TICKERS = ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA"]

STARTING_CASH = 10_000_000   # cents → $100,000.00
ORDER_SIZE = 5            # shares per trade
MAX_POSITION = 30           # max shares per stock at once
WINDOW_SIZE = 20           # ticks to calculate moving average
MOMENTUM_THR = 0.005        # 0.5% move triggers trade
NEWS_STRONG_THR = 0.5          # sentiment ≥ this = act on news
COOLDOWN_SECS = 2.0          # min seconds between trades per ticker

# ── Trailing stop ──────────────────────────────────────
TRAILING_STOP_PCT = 0.04   # exit if price drops 4% from peak since entry

# ── Portfolio stop loss ────────────────────────────────
PORTFOLIO_STOP_PCT = 0.12   # halt ALL trading if net worth drops 12%

# ── Sentiment bias decay ──────────────────────────────
BIAS_DECAY = 0.01   # bias recovers toward 1.0 by this much per tick


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger()


# Price history per ticker
price_history = {t: deque(maxlen=100) for t in TICKERS}

# Wallet state (updated from server)
wallet = {
    "cash": STARTING_CASH,
    "positions": {t: 0 for t in TICKERS},
    "net_worth": STARTING_CASH,
}

# Round state
round_active = False

# Last trade time per ticker (cooldown)
last_trade_time = {t: 0.0 for t in TICKERS}

# Pending news signals (ticker → sentiment)
news_signals = {}

# ── Trailing stop state ───────────────────────────────
peak_price = {}   # ticker → highest price seen since entry
trading_halted = False   # True when portfolio stop loss fires

# ── Sentiment bias (scales momentum threshold) ────────
# 1.0 = normal, <1.0 = more cautious after news
sentiment_bias = {}   # ticker → float

# WebSocket connection (set at runtime)
ws_conn = None


def make_msg(msg_type, payload):
    return json.dumps({
        "type": msg_type,
        "payload": payload,
        "ts": int(time.time() * 1000)
    })


async def send(msg_type, payload):
    if ws_conn:
        await ws_conn.send(make_msg(msg_type, payload))


async def submit_limit_order(ticker, side, price_cents, qty):
    """
    Place a limit order.
    price_cents: price in cents (e.g. 18250 = $182.50)
    side: "BUY" or "SELL"
    """
    await send("limit_order", {
        "ticker":   ticker,
        "side":     side,
        "price":    price_cents,
        "quantity": qty,
    })


def cents_to_dollars(cents):
    return cents / 100


def on_cooldown(ticker):
    return (time.time() - last_trade_time[ticker]) < COOLDOWN_SECS


def record_trade(ticker):
    last_trade_time[ticker] = time.time()


def moving_average(ticker):
    h = list(price_history[ticker])
    if len(h) < WINDOW_SIZE:
        return None
    return sum(h[-WINDOW_SIZE:]) / WINDOW_SIZE


async def evaluate_ticker(ticker, price_cents):
    """
    Core strategy per ticker.
    Combines momentum signal with news sentiment signal.
    Priority: portfolio stop → trailing stop → news → momentum
    """
    global trading_halted

    if not round_active:
        return

    # ── 1. Portfolio stop loss ─────────────────────────
    # Halt all trading if net worth drops too far
    if not trading_halted:
        nw = wallet["net_worth"]
        floor = STARTING_CASH * (1 - PORTFOLIO_STOP_PCT)
        if nw < floor:
            trading_halted = True
            log.warning(
                f"\n⛔ PORTFOLIO STOP LOSS — "
                f"${cents_to_dollars(nw):,.2f} < ${cents_to_dollars(floor):,.2f} "
                f"| ALL TRADING HALTED\n"
            )
    if trading_halted:
        return

    price = price_cents
    pos = wallet["positions"].get(ticker, 0)
    cash = wallet["cash"]

    # ── 2. Trailing stop ───────────────────────────────
    # Track peak price while holding; exit if it drops too far
    if pos > 0:
        if peak_price.get(ticker, 0) < price:
            peak_price[ticker] = price
        peak = peak_price.get(ticker, price)
        drop = (peak - price) / peak
        if drop >= TRAILING_STOP_PCT:
            sell_price = int(price * 0.999)
            log.info(
                f"   🛑 TRAIL STOP {ticker}: "
                f"peak ${cents_to_dollars(peak):.2f} → "
                f"now ${cents_to_dollars(price):.2f} "
                f"({drop*100:.1f}% drop) — selling {pos}"
            )
            await submit_limit_order(ticker, "SELL", sell_price, pos)
            record_trade(ticker)
            peak_price[ticker] = 0
            return   # exit fully, skip all other signals
    else:
        # No position — reset peak so it starts fresh on next entry
        peak_price[ticker] = 0

    # ── 3. Sentiment bias decay ────────────────────────
    # Bias drifts back toward 1.0 each tick so news effect fades naturally
    if ticker in sentiment_bias:
        sentiment_bias[ticker] = min(1.0, sentiment_bias[ticker] + BIAS_DECAY)

    if on_cooldown(ticker):
        return

    avg = moving_average(ticker)

    # ── 4. News signal overrides momentum ──────────────
    if ticker in news_signals:
        sentiment = news_signals.pop(ticker)

        if sentiment >= NEWS_STRONG_THR and pos < MAX_POSITION:
            buy_price = int(price * 1.001)
            qty = min(ORDER_SIZE, MAX_POSITION - pos)
            cost = buy_price * qty
            if cost <= cash:
                log.info(
                    f"   📰 NEWS BUY  {qty}×{ticker} "
                    f"@ ${cents_to_dollars(buy_price):.2f} "
                    f"(sentiment={sentiment:.2f})"
                )
                await submit_limit_order(ticker, "BUY", buy_price, qty)
                peak_price[ticker] = buy_price   # start tracking peak
                record_trade(ticker)
            return

        elif sentiment <= -NEWS_STRONG_THR and pos > 0:
            sell_price = int(price * 0.999)
            # Sell FULL position on strong negative news, not just ORDER_SIZE
            log.info(
                f"   📰 NEWS SELL {pos}×{ticker} "
                f"@ ${cents_to_dollars(sell_price):.2f} "
                f"(sentiment={sentiment:.2f})"
            )
            await submit_limit_order(ticker, "SELL", sell_price, pos)
            peak_price[ticker] = 0
            record_trade(ticker)
            return

    # ── 5. Momentum signal ─────────────────────────────
    if avg is None:
        return  # not enough history yet

    # Scale threshold by sentiment bias (cautious after news)
    bias = sentiment_bias.get(ticker, 1.0)
    threshold = MOMENTUM_THR * bias
    deviation = (price - avg) / avg

    if deviation > threshold and pos < MAX_POSITION:
        buy_price = int(price * 1.001)
        qty = min(ORDER_SIZE, MAX_POSITION - pos)
        cost = buy_price * qty
        if cost <= cash:
            log.info(
                f"   🟢 MOM  BUY  {qty}×{ticker} "
                f"@ ${cents_to_dollars(buy_price):.2f} "
                f"(dev={deviation:.3f} thr={threshold:.3f})"
            )
            await submit_limit_order(ticker, "BUY", buy_price, qty)
            peak_price[ticker] = buy_price   # start tracking peak
            record_trade(ticker)

    elif deviation < -threshold and pos > 0:
        sell_price = int(price * 0.999)
        # Sell FULL position on breakdown signal
        log.info(
            f"   🔴 MOM  SELL {pos}×{ticker} "
            f"@ ${cents_to_dollars(sell_price):.2f} "
            f"(dev={deviation:.3f} thr={threshold:.3f})"
        )
        await submit_limit_order(ticker, "SELL", sell_price, pos)
        peak_price[ticker] = 0
        record_trade(ticker)


async def handle_message(raw):
    global round_active

    try:
        msg = json.loads(raw)
        mtype = msg.get("type")
        payload = msg.get("payload", {})
    except json.JSONDecodeError:
        return

    # ── Auth response ───────────────────────────────────
    if mtype == "auth_response":
        if payload.get("success"):
            log.info(f"✅ Authenticated as '{USERNAME}'")
        else:
            log.error(f"❌ Auth failed: {payload.get('message', 'unknown')}")
            sys.exit(1)

    # ── Round lifecycle ─────────────────────────────────
    elif mtype == "round_state":
        state = payload.get("state", "")
        if state == "Active":
            round_active = True
            trading_halted = False   # reset stop loss for new round
            log.info("\n🟢 ROUND ACTIVE — Bot is now trading!\n")
        elif state in ("Lobby", "Paused"):
            round_active = False
            log.info(f"⏸️  Round state: {state} — waiting...")
        elif state == "Ended":
            round_active = False
            log.info("\n🏁 ROUND ENDED\n")

    # ── Price update ────────────────────────────────────
    elif mtype == "price_update":
        ticker = payload.get("ticker")
        price_cents = payload.get("price")
        if ticker and price_cents:
            price_history[ticker].append(price_cents)
            await evaluate_ticker(ticker, price_cents)

    # ── Wallet update ───────────────────────────────────
    elif mtype == "wallet_update":
        wallet["cash"] = payload.get("cash", wallet["cash"])
        wallet["net_worth"] = payload.get("net_worth", wallet["net_worth"])
        positions = payload.get("positions", {})
        for t, qty in positions.items():
            wallet["positions"][t] = qty

    # ── Trade confirmation ──────────────────────────────
    elif mtype == "trade":
        ticker = payload.get("ticker")
        side = payload.get("side")
        qty = payload.get("quantity")
        price = payload.get("price")
        nw = cents_to_dollars(wallet["net_worth"])
        log.info(
            f"   ✅ FILLED {side} {qty}×{ticker} @ ${cents_to_dollars(price):.2f} | Net worth: ${nw:,.2f}")

    # ── Order rejected ──────────────────────────────────
    elif mtype == "order_response":
        if not payload.get("success"):
            reason = payload.get("message", "unknown")
            log.warning(f"   ⚠️  Order rejected: {reason}")

    # ── News event ──────────────────────────────────────
    elif mtype == "news_flash":
        sentiment = payload.get("sentiment_score", 0)
        headline = payload.get("headline", "")
        ticker = payload.get("ticker")   # None = macro event
        if ticker:
            news_signals[ticker] = sentiment
            # Set sentiment bias — makes threshold harder to cross after news
            sentiment_bias[ticker] = max(0.3, 1.0 - abs(sentiment) * 0.5)
            log.info(
                f"   📰 NEWS [{ticker}] sentiment={sentiment:.2f} "
                f"bias→{sentiment_bias[ticker]:.2f} — '{headline[:50]}'"
            )
        else:
            log.info(
                f"   🌍 MACRO NEWS sentiment={sentiment:.2f} — '{headline[:50]}'")
            # Apply macro sentiment to all tickers at half strength
            if abs(sentiment) >= NEWS_STRONG_THR:
                for t in TICKERS:
                    if t not in news_signals:
                        news_signals[t] = sentiment * 0.5
                    # Macro news also sets bias (weaker effect)
                    sentiment_bias[t] = max(0.5, 1.0 - abs(sentiment) * 0.3)

    # ── Leaderboard ─────────────────────────────────────
    elif mtype == "leaderboard":
        entries = payload.get("entries", [])
        log.info(f"\n  {'─'*45}")
        log.info(f"  🏆 LEADERBOARD")
        for i, e in enumerate(entries[:5]):
            marker = " ←" if e.get("participant_id") == USERNAME else ""
            nw = cents_to_dollars(e.get("net_worth", 0))
            log.info(
                f"  #{i+1}  {e.get('participant_id', '?'):<20} ${nw:>12,.2f}{marker}")
        log.info(f"  {'─'*45}\n")

    # ── Fraud alert ─────────────────────────────────────
    elif mtype == "fraud_alert":
        log.warning(
            f"\n⛔ FRAUD ALERT — account frozen for 60s. Stop cancelling orders!\n")

    # ── Heartbeat (ignore) ──────────────────────────────
    elif mtype == "heartbeat":
        pass

    else:
        log.debug(f"Unhandled message type: {mtype}")


async def run():
    global ws_conn

    log.info(f"\n{'═'*50}")
    log.info(f"  🤖  HFT SIEGE BOT")
    log.info(f"  Server  : {WS_URL}")
    log.info(f"  Team    : {USERNAME}")
    log.info(f"  Tickers : {', '.join(TICKERS)}")
    log.info(f"{'═'*50}\n")

    while True:
        try:
            log.info(f"Connecting to {WS_URL}...")

            async with websockets.connect(WS_URL) as ws:
                ws_conn = ws
                log.info("Connected ✅")

                # Step 1 — Authenticate
                await send("auth", {
                    "username": USERNAME,
                    "password": PASSWORD,
                })

                # Step 2 — Listen forever
                async for raw_message in ws:
                    await handle_message(raw_message)

        except websockets.exceptions.ConnectionClosed as e:
            log.warning(f"⚡ Connection closed ({e}). Reconnecting in 3s...")
            ws_conn = None
            await asyncio.sleep(3)

        except ConnectionRefusedError:
            log.error(f"❌ Cannot connect to {WS_URL} — is the server running?")
            await asyncio.sleep(5)

        except KeyboardInterrupt:
            log.info("\n🛑 Bot stopped.")
            nw = cents_to_dollars(wallet["net_worth"])
            log.info(f"   Final net worth: ${nw:,.2f}")
            break

        except Exception as e:
            log.error(f"Unexpected error: {e}")
            await asyncio.sleep(2)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
