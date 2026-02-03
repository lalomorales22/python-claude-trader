# HANDOFF

## Project
Penguin-Burry Analyzer — Flask trading app with Claude Opus 4.5 vision + web search. Single file: `app.py`

## Last Completed: Phase 5 ✅
Intelligence & Optimization:
- **Backtesting Engine**: Full historical strategy replay
  - Fetch 1 year OHLCV data from CoinGecko
  - Replay Penguin/Burry strategies with proper entry/exit logic
  - Calculate win rate, profit factor, max drawdown, expectancy
  - Generate equity curves and monthly returns breakdown
  - Parameter optimization with walk-forward validation (80/20 split)
- **Pattern Recognition**: Learn from your trade history
  - Capture indicator snapshots for completed trades
  - Calculate similarity scores between current setup and historical winners
  - Track performance by time of day and day of week
  - Generate strategy health reports
- **Market Regime Classification (HMM-style)**:
  - 3-state model: TRENDING, MEAN_REVERTING, TRANSITIONAL
  - Auto-adjust strategy recommendations per regime
  - Detect and alert on regime changes
  - Track regime history over time
- **Performance Attribution**: Find your actual edge
  - Break down P&L by strategy, symbol, direction
  - Identify best/worst performing areas
  - Generate actionable trading recommendations
- **Risk Simulation & Stress Testing**:
  - Monte Carlo drawdown simulations (1000 runs)
  - Calculate probability of ruin
  - Stress test against scenarios (BTC crash, flash crash, short squeeze)
  - Optimal position sizing via Kelly criterion

## Previously Completed: Phase 4 ✅
Automation & Real-Time Alerts:
- **Background Monitor Thread**: Auto-starts on launch, runs continuously
- **Alert System**: Full CRUD for price, indicator, funding, divergence alerts
- **Auto-Scanner Engine**: Scans watchlist for Penguin/Burry setups
- **OCO Order Simulation**: One-Cancels-Other logic for SL/TP
- **Position Monitoring**: Real-time P&L tracking

## To Run
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
python app.py  # → http://localhost:7777
# Monitor auto-starts! Check the Alerts tab.
```

## Key Files
- `app.py` — all code (backend + frontend, ~7200 lines)
- `tasks.md` — full roadmap with task details
- `penguin_burry.db` — SQLite (auto-created)

## Phase 5 API Endpoints
**Backtesting:**
- `GET /api/intel/backtest/<symbol>/<strategy>` — run backtest (penguin/burry)
- `GET /api/intel/backtest/optimize/<symbol>/<strategy>` — parameter optimization
- `GET /api/intel/backtest/results` — saved backtest history

**Pattern Recognition:**
- `POST /api/intel/patterns/capture/<journal_id>` — capture trade snapshot
- `POST /api/intel/patterns/similar` — find similar historical trades
- `GET /api/intel/patterns/time-analysis` — performance by hour/day
- `GET /api/intel/patterns/health-report` — strategy health report

**Market Regime:**
- `GET /api/intel/regime/<symbol>` — current regime classification
- `GET /api/intel/regime/history/<symbol>` — regime change history

**Performance Attribution:**
- `GET /api/intel/attribution` — P&L breakdown by strategy/symbol/direction
- `GET /api/intel/recommendations` — actionable trading recommendations

**Risk Simulation:**
- `GET /api/intel/risk/ruin` — probability of ruin calculation
- `GET /api/intel/risk/simulate` — Monte Carlo drawdown simulation
- `POST /api/intel/risk/stress-test` — stress test against scenarios
- `GET /api/intel/risk/optimal-size` — Kelly criterion position sizing

**Dashboard:**
- `GET /api/intel/dashboard` — comprehensive intelligence dashboard (all above in one call)

## Database Tables Added in Phase 5
- `trade_patterns` — indicator snapshots for pattern recognition
- `backtest_results` — saved backtest runs
- `regime_history` — market regime tracking over time

## Example API Usage
```bash
# Backtest Penguin strategy on SOL for 365 days
curl "http://localhost:7777/api/intel/backtest/SOL/penguin?days=365&save=true"

# Get current market regime for BTC
curl "http://localhost:7777/api/intel/regime/BTC"

# Run Monte Carlo simulation
curl "http://localhost:7777/api/intel/risk/simulate?simulations=1000"

# Stress test against BTC crash scenario
curl -X POST "http://localhost:7777/api/intel/risk/stress-test" \
  -H "Content-Type: application/json" \
  -d '{"scenario": "btc_crash_20"}'

# Get full intelligence dashboard
curl "http://localhost:7777/api/intel/dashboard?symbol=BTC"
```

## Phase 4 API Endpoints (Still Active)
- `GET /api/alerts` — list all alerts
- `POST /api/alerts` — create new alert
- `GET/PUT/DELETE /api/alerts/<id>` — manage specific alert
- `POST /api/alerts/<id>/toggle` — enable/disable alert
- `POST /api/alerts/<id>/reset` — reset triggered alert
- `GET /api/alerts/history` — triggered alert history
- `GET /api/alerts/triggered` — poll for new notifications
- `GET /api/monitor/status` — background monitor status
- `POST /api/monitor/start` — start monitor
- `POST /api/monitor/stop` — stop monitor
- `POST /api/scan/run` — manual scan trigger
- `GET /api/scan/results` — recent scan results
- `GET /api/scan/setups` — current tradeable setups (4+ signals)
- `GET /api/positions/monitor` — detailed position monitoring
