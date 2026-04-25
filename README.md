# 🤖 Competition Trading Bot

An algorithmic trading bot built for the **HFT Siege** competition, running on the [mason](https://github.com/adimukh1234/mason) platform. Connects via WebSocket, receives real-time price feeds, and executes limit orders autonomously using a momentum + news strategy with full risk management.

---

## Files

| File | Purpose |
|---|---|
| `bot.py` | Main competition bot — run this |
| `mock_server.py` | Local test server that simulates the competition |

---

## Quickstart

```bash
# Install dependency
pip install websockets

# Terminal 1 — start the mock server
python mock_server.py

# Terminal 2 — start the bot
python bot.py
```

For the real competition, just update the three config lines in `bot.py`:

```python
WS_URL   = "ws://<organizer-ip>:8081/ws"
USERNAME = "your_team_name"
PASSWORD = "your_password"
```

---

## Strategy

The bot runs a **momentum strategy** with a **news sentiment overlay** and five layers of logic that execute in strict priority order on every price tick:

```
1. Portfolio stop loss   → halt everything if down 12%
2. Trailing stop         → exit position if price drops 4% from peak
3. Sentiment bias decay  → news caution fades naturally over ~70 ticks
4. News signal           → override momentum if strong sentiment event
5. Momentum signal       → buy/sell based on moving average deviation
```

### Momentum Signal

Compares current price to a 20-tick moving average:

```
price > avg × (1 + threshold)  →  BUY
price < avg × (1 - threshold)  →  SELL (full position)
```

Threshold is scaled by `sentiment_bias` — after news, the threshold rises, making the bot harder to trigger on that ticker until the bias decays back to 1.0.

### News Signal

On a `news_flash` event:
- **Ticker-specific news**: stored as a signal, sets `sentiment_bias` based on score strength
- **Macro news**: applied to all tickers at 50% strength
- **Strong positive** (`sentiment ≥ 0.5`): BUY immediately on next price tick
- **Strong negative** (`sentiment ≤ -0.5`): SELL full position immediately

### Sentiment Bias

After news fires, the momentum threshold is scaled up to prevent chasing volatile post-news moves:

```python
bias = max(0.3, 1.0 - abs(sentiment) * 0.5)   # set on news
bias = min(1.0, bias + 0.01)                   # decays +0.01 per tick
```

A sentiment score of 0.8 sets bias to 0.6, raising the effective threshold from 0.5% to 0.3% — the bot becomes more selective. After ~40 ticks the bias is fully recovered.

---

## Risk Management

| Protection | Mechanism | Default |
|---|---|---|
| **Trailing stop** | Sell full position if price drops X% from peak since entry | 4% |
| **Portfolio stop loss** | Halt ALL trading if net worth drops X% from start | 12% |
| **Cooldown** | Min time between trades on same ticker | 2.0s |
| **Position cap** | Max shares held per ticker at once | 30 |
| **Cash check** | Never place a buy order the account can't afford | always on |

The trailing stop runs **before** the cooldown check — it cannot be blocked.

---

## Configuration

All parameters are at the top of `bot.py`. Change only these for competition tuning:

```python
# Connection
WS_URL   = "ws://localhost:8081/ws"
USERNAME = "your_team_name"
PASSWORD = "your_password"

# Universe
TICKERS = ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA"]

# Sizing
ORDER_SIZE   = 5     # shares per trade — raise if bot is profitable
MAX_POSITION = 30    # max shares per ticker

# Strategy
WINDOW_SIZE    = 20    # moving average lookback (lower = faster reaction)
MOMENTUM_THR   = 0.005 # 0.5% deviation triggers trade
NEWS_STRONG_THR = 0.5  # sentiment threshold to act on news
COOLDOWN_SECS  = 2.0   # seconds between trades per ticker

# Risk
TRAILING_STOP_PCT  = 0.04  # 4% drop from peak → exit
PORTFOLIO_STOP_PCT = 0.12  # 12% portfolio loss → halt all trading
BIAS_DECAY         = 0.01  # news caution recovery speed per tick
```

### Tuning Cheat Sheet

| Symptom | Fix |
|---|---|
| No trades firing | Lower `MOMENTUM_THR` → `0.003`, lower `WINDOW_SIZE` → `10` |
| Too many bad trades | Raise `MOMENTUM_THR` → `0.008` |
| Orders not filling | Change buy price multiplier `1.001` → `1.003` in `evaluate_ticker` |
| Missing fast moves | Lower `COOLDOWN_SECS` → `0.5` |
| Trailing stop fires too early | Raise `TRAILING_STOP_PCT` → `0.06` |
| Holding losses too long | Lower `TRAILING_STOP_PCT` → `0.02` |
| Bot profitable — want more | Raise `ORDER_SIZE` to `10`, `MAX_POSITION` to `50` |

---

## Mock Server

`mock_server.py` is a full simulation of the competition server. Run it locally to test the bot before competing.

**What it simulates:**

- Real-time price feeds using Geometric Brownian Motion
- 4 auto-cycling market phases: Trending → Volatile → Crash → Recovery
- 8 pre-scheduled news events (ticker + macro) at specific ticks
- Limit order matching engine (fills when market price crosses your limit)
- Wallet updates after every fill
- Leaderboard broadcasts every 30 ticks
- Round end after 300 ticks (~90 seconds)

**Mock server config** (top of `mock_server.py`):

```python
TICK_INTERVAL  = 0.3   # seconds between price updates
ROUND_TICKS    = 300   # ticks per round (~90s)
```

Lower `TICK_INTERVAL` to stress-test the bot under a faster feed.

---

## Protocol

The bot communicates over WebSocket using JSON messages:

```json
{ "type": "...", "payload": {}, "ts": 1234567890 }
```

| Message type | Direction | Description |
|---|---|---|
| `auth` | Bot → Server | Login with username + password |
| `auth_response` | Server → Bot | Login success/failure |
| `round_state` | Server → Bot | Round Active / Paused / Ended |
| `price_update` | Server → Bot | New price for a ticker |
| `wallet_update` | Server → Bot | Current cash, positions, net worth |
| `trade` | Server → Bot | Order fill confirmation |
| `order_response` | Server → Bot | Order accepted or rejected |
| `news_flash` | Server → Bot | News event with sentiment score |
| `leaderboard` | Server → Bot | Current rankings |
| `fraud_alert` | Server → Bot | Account frozen (cancel too many orders) |
| `limit_order` | Bot → Server | Place a limit order |

---

## Logs

The bot writes to both terminal and `bot.log`. Key log prefixes:

```
✅ FILLED    — order was executed
🟢 MOM BUY  — momentum buy signal
🔴 MOM SELL — momentum sell signal
📰 NEWS BUY/SELL — news-driven trade
🛑 TRAIL STOP — trailing stop triggered
⛔ PORTFOLIO STOP LOSS — global halt
⚠️  Order rejected — server refused the order
🏆 LEADERBOARD — current rankings
```

---

## Platform

Built for [mason](https://github.com/adimukh1234/mason) — an HFT competition framework.
