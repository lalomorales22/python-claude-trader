# 🐧 Penguin-Burry Chart Analyzer
<img width="1131" height="658" alt="Screenshot 2026-02-03 at 9 18 18 AM" src="https://github.com/user-attachments/assets/a49adb18-345b-4ee2-91dc-bc729fb68c66" />

> **AI-Powered Trading Intelligence Platform** — Claude Opus 4.5 Vision + Web Search + Quantitative Analysis

A single-file Flask application that combines AI chart analysis with real-time market data, automated alerts, backtesting, and institutional-grade risk management. Built for crypto traders who want data-driven decisions.

![Python](https://img.shields.io/badge/Python-3.9+-blue.svg)
![Flask](https://img.shields.io/badge/Flask-3.0+-green.svg)
![Claude](https://img.shields.io/badge/Claude-Opus%204.5-purple.svg)
![License](https://img.shields.io/badge/License-MIT-yellow.svg)

## 🎯 What It Does

**Two Core Strategies:**

| Strategy | Description | Win Rate |
|----------|-------------|----------|
| 🐧 **Penguin** | BTC/altcoin divergence plays. Enters when BTC dumps but your alt shows strength. | ~70.8% |
| 🔴 **Burry** | Overbought exhaustion shorts. Catches blow-off tops with RSI >80, ADX <30. | ~80% on 5/5 |

**Key Features:**
- 📊 **AI Chart Analysis** — Upload any chart, Claude analyzes it with web search for live prices
- 🔔 **Real-Time Alerts** — Price, indicator, funding rate, and divergence alerts
- 🤖 **Auto-Scanner** — Continuously scans your watchlist for setups
- 📈 **Backtesting Engine** — Test strategies on 1+ year of historical data
- 🧠 **Pattern Recognition** — "This setup is 87% similar to your best winning trades"
- 🌊 **Market Regime Detection** — Knows when to trade momentum vs mean reversion
- ⚠️ **Risk Management** — Kelly criterion sizing, Monte Carlo simulations, stress testing

## 🚀 Quick Start

### Prerequisites

- Python 3.9 or higher
- An [Anthropic API Key](https://console.anthropic.com/) (Claude)

### Installation

```bash
# Clone the repository
git clone https://github.com/lalomorales22/python-claude-trader.git
cd python-claude-trader

# Create virtual environment (recommended)
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install flask anthropic requests
```

### Configuration

```bash
# Set your Anthropic API key
export ANTHROPIC_API_KEY="sk-ant-api03-..."

# Optional: Configure host/port
export PB_HOST="0.0.0.0"
export PB_PORT="7777"
```

### Run

```bash
python app.py
```

Then open **http://localhost:7777** in your browser.

## 📖 Usage Guide

### 1. Analyze a Chart

1. Go to the **Analyze** tab
2. Upload a chart screenshot (TradingView, exchange charts, etc.)
3. Select strategy: Penguin (long divergence) or Burry (short exhaustion)
4. Click **Analyze** — Claude will:
   - Search the web for current price, volume, news
   - Analyze the chart visually for patterns
   - Count signals (need 4/5 for TRADE recommendation)
   - Provide entry, stop-loss, and take-profit levels

### 2. Chat with Claude

The **Chat** tab maintains conversation history. Ask things like:
- "What's SOL doing right now?"
- "Should I short ETH here?"
- "Explain the ADX kill switch rule"

### 3. Set Up Alerts

Go to **Alerts** tab to create:
- **Price alerts**: "Alert me when SOL crosses $200"
- **Indicator alerts**: "Alert when RSI > 80 on BTC"
- **Funding alerts**: "Alert on extreme funding rates"
- **Divergence alerts**: "Alert when alts diverge from BTC"

### 4. Run Backtests

Use the API to backtest strategies:

```bash
# Backtest Penguin strategy on SOL for 1 year
curl "http://localhost:7777/api/intel/backtest/SOL/penguin?days=365"

# Optimize parameters
curl "http://localhost:7777/api/intel/backtest/optimize/ETH/burry"
```

### 5. Check Market Regime

```bash
# Is the market trending or ranging?
curl "http://localhost:7777/api/intel/regime/BTC"
```

Returns: `TRENDING`, `MEAN_REVERTING`, or `TRANSITIONAL` with strategy recommendations.

## 🔌 API Reference

### Core Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` | GET | Check API status |
| `/api/analyze` | POST | Analyze chart image |
| `/api/chat` | POST | Chat with Claude |
| `/api/market-data/<symbol>` | GET | Get price + indicators |

### Alerts & Monitoring

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/alerts` | GET/POST | List/create alerts |
| `/api/alerts/<id>` | GET/PUT/DELETE | Manage alert |
| `/api/monitor/status` | GET | Background monitor status |
| `/api/scan/setups` | GET | Current tradeable setups |

### Intelligence (Phase 5)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/intel/backtest/<symbol>/<strategy>` | GET | Run backtest |
| `/api/intel/backtest/optimize/<symbol>/<strategy>` | GET | Optimize params |
| `/api/intel/regime/<symbol>` | GET | Market regime |
| `/api/intel/attribution` | GET | Performance breakdown |
| `/api/intel/risk/simulate` | GET | Monte Carlo simulation |
| `/api/intel/risk/stress-test` | POST | Stress test scenarios |
| `/api/intel/dashboard` | GET | Full intelligence dashboard |

### Quantitative

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/quant/kelly` | GET | Kelly criterion sizing |
| `/api/quant/fear-greed` | GET | Fear & Greed Index |
| `/api/quant/funding/<symbol>` | GET | Funding rates |
| `/api/quant/correlation` | GET | Correlation matrix |

## 📊 The Penguin-Burry System

### Penguin Strategy (Long Divergence)

**Entry Criteria (need 4/5):**
1. BTC down 3-8% in 24h
2. Alt up 10-25% (divergence)
3. RSI 70-85 (momentum, not extreme)
4. Volume 2-3x average
5. Support level holding

**Exit Rules:**
- Stop Loss: -3%
- Take Profit: +15-20%
- Trailing stop after +10%

### Burry Strategy (Short Exhaustion)

**Entry Criteria (need 4/5):**
1. RSI > 80 (extreme overbought)
2. MACD histogram turning negative
3. ADX < 30 (CRITICAL — weak trend)
4. Stochastic > 90
5. Volume > 2x average

**ADX Kill Switch:**
- < 30: SAFE to short
- 30-40: CAUTION
- 40-50: DANGER
- \> 50: NEVER SHORT (death trap)

**Exit Rules:**
- Stop Loss: +3% (price rises = loss)
- Take Profit: -15% (price drops = profit)

## 🛡️ Risk Management Rules

1. **Minimum 4/5 signals** for TRADE recommendation
2. **3/5 signals = HOLD** (it's gambling)
3. **Position sizing: 60-80% max**, NEVER 100%
4. **Turtle Mode**: After 2 consecutive losses, require 5/5 signals
5. **Daily loss limit**: 8% → trading locked
6. **Portfolio heat**: Track total exposure vs balance

## 🗄️ Database Schema

SQLite database (`penguin_burry.db`) with tables:
- `analyses` — Chart analysis results
- `trade_journal` — Trade log with P&L
- `watchlist` — Symbols to track
- `alerts` — Alert definitions
- `alert_history` — Triggered alerts
- `positions` — Open positions
- `trade_patterns` — Indicator snapshots for pattern matching
- `backtest_results` — Saved backtests
- `regime_history` — Market regime tracking
- `settings` — App configuration
- `chat_history` — Conversation memory

## 🔧 Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | (required) | Your Claude API key |
| `CLAUDE_MODEL` | `claude-opus-4-5-20251101` | Model to use |
| `PB_HOST` | `0.0.0.0` | Server host |
| `PB_PORT` | `7777` | Server port |
| `PB_DB_PATH` | `penguin_burry.db` | Database path |

## 📁 Project Structure

```
python-claude-trader/
├── app.py              # Everything (backend + frontend, ~7200 lines)
├── penguin_burry.db    # SQLite database (auto-created)
├── pb_uploads/         # Uploaded chart images
├── tasks.md            # Development roadmap
├── handoff.md          # Session handoff notes
├── README.md           # This file
└── .gitignore          # Git ignore rules
```

## 🛠️ Development Phases

- [x] **Phase 1**: Foundation — Stateful chat, real data feeds, programmatic indicators
- [x] **Phase 2**: Wallet Integration — MetaMask/Phantom connection, execution layer
- [x] **Phase 3**: Quantitative Edge — Kelly criterion, funding rates, correlations
- [x] **Phase 4**: Automation — Background monitor, alerts, auto-scanner, OCO orders
- [x] **Phase 5**: Intelligence — Backtesting, pattern recognition, regime detection, risk simulation

## ⚠️ Disclaimer

This software is for **educational and research purposes only**. 

- Not financial advice
- Past performance doesn't guarantee future results
- Cryptocurrency trading involves substantial risk of loss
- Only trade with money you can afford to lose
- The authors are not responsible for any trading losses

## 🤝 Contributing

Contributions welcome! Please:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit changes (`git commit -m 'Add amazing feature'`)
4. Push to branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## 📄 License

MIT License — see [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- [Anthropic](https://anthropic.com) for Claude AI
- [CoinGecko](https://coingecko.com) for market data API
- [DEXScreener](https://dexscreener.com) for DEX data
- [Binance](https://binance.com) for funding rate data

---

**Built with 🐧 by traders, for traders.**

*"If it's not obvious, it's not a trade."*
