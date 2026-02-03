# PENGUIN-BURRY PLATFORM — 5-PHASE EVOLUTION ROADMAP

> From chart analysis toy → personal quantitative trading terminal

---

## CURRENT STATE ASSESSMENT

### What Works
- Single-file Flask app with embedded frontend — easy to deploy and iterate
- Claude Opus 4.5 vision + web search for chart analysis
- SQLite persistence for analyses, journal, watchlist, templates, settings
- Penguin (divergence long) and Burry (exhaustion short) strategy prompts
- Signal counting, recommendation engine, confidence scoring
- Turtle mode concept and consecutive loss tracking in settings
- Clean dark UI with modals, tabs, toast notifications

### Critical Gaps
1. **No execution layer** — analysis stops at recommendations, can't trade
2. **No real data feeds** — relies on Claude web search for prices/volume (slow, imprecise)
3. **Stateless chat** — each Claude call is standalone, no conversation memory
4. **No programmatic indicators** — RSI, MACD, ADX calculated by Claude's vision, not math
5. **No wallet integration** — can't read balances, positions, or execute
6. **No backtesting** — can't validate strategies on historical data
7. **No real-time anything** — no WebSockets, no alerts, no auto-scanning
8. **Portfolio heat is a static number** — no actual position tracking
9. **Journal doesn't trigger risk rules** — logging a loss doesn't auto-activate turtle mode
10. **No quant techniques beyond classical TA** — missing order flow, vol analysis, funding rates, Kelly sizing, correlation, on-chain data

### Architecture Limitations
- Everything runs in a single Flask process (no async, no background tasks)
- No authentication (fine for personal use but matters for wallet integration)
- No rate limiting on endpoints
- Image analysis round-trip is 10-20 seconds with no streaming
- Frontend is vanilla JS in a raw string (hard to maintain at scale)

---

## PHASE 1: FIX THE FOUNDATION (Week 1-2) ✅ COMPLETED
> Make what exists actually work properly before adding features

**Completed: February 2, 2026**

### 1.1 — Stateful Chat with Conversation History ✅
**Problem:** Each `/api/chat` call sends only the current message. Claude has zero context from prior messages.
**Fix:** Load last N messages from `chat_history` table and send as conversation turns.
```
Messages structure:
[
  {"role": "user", "content": "what's SOL doing?"},
  {"role": "assistant", "content": "SOL is at $185..."},
  {"role": "user", "content": "should I short it?"},  // NOW Claude knows "it" = SOL
]
```
**Tasks:**
- [x] Modify `claude_chat_text()` to accept a `messages` list instead of single prompt
- [x] In `/api/chat`, load last 40 messages from `chat_history` before appending new message
- [x] Add token counting to avoid exceeding context window (truncate oldest messages)
- [ ] Add session/thread concept so you can have multiple chat threads (DEFERRED to Phase 2)

**Implementation Notes:**
- Added `estimate_tokens()` function (4 chars/token average)
- Added `truncate_messages_to_fit()` to stay under 12K token budget
- Updated `claude_chat_text()` with `conversation_history` parameter
- `/api/chat` now loads history and passes full conversation context
- Added `/api/chat/history` endpoint to retrieve chat history

### 1.2 — Real Data Feeds (CoinGecko + DEXScreener APIs) ✅
**Problem:** Claude web search is slow and imprecise for live prices. You're burning expensive Opus tokens to get a price you could fetch in 200ms.
**Fix:** Add direct API integrations and feed structured data TO Claude alongside the image.
**Tasks:**
- [x] Add CoinGecko API integration (free tier: 30 calls/min)
  - `/api/price/{symbol}` — current price, 24h change, volume, market cap
  - `/api/market-data/{symbol}` — full price data + calculated indicators
- [x] Add DEXScreener API integration
  - `/api/dex/search/{query}` — find pairs with liquidity, txns, price changes
- [ ] Add Finnhub integration for stock data (DEFERRED - crypto focus for now)
- [x] Create unified `/api/market-data/{symbol}` endpoint that:
  1. Fetches price data from CoinGecko (fallback to DEXScreener)
  2. Calculates RSI, MACD, ADX, Stochastic programmatically
  3. Returns structured JSON with all indicator values
- [ ] Modify analysis flow: fetch market data FIRST, then include it in Claude prompt (DEFERRED - manual via endpoints for now)

**Implementation Notes:**
- `fetch_coingecko_price()` - gets current price, 24h/7d change, volume, market cap, ATH
- `fetch_coingecko_ohlcv()` - gets OHLCV data for indicator calculation
- `fetch_dexscreener_token()` - searches for tokens, returns highest liquidity pair
- Symbol mapping for 30+ major coins (BTC, ETH, SOL, DOGE, etc.)
- New endpoints: `/api/price/<symbol>`, `/api/market-data/<symbol>`, `/api/dex/search/<query>`, `/api/indicators/<symbol>`

### 1.3 — Programmatic Technical Indicators ✅
**Problem:** Asking Claude to read RSI from a chart image is inherently imprecise.
**Fix:** Calculate indicators from OHLCV data using pure Python (no external dependencies).
**Tasks:**
- [x] Implement pure Python indicator calculations (no pandas/ta-lib dependency)
- [x] Calculate from OHLCV candles:
  - RSI (14-period) — exact value with zone classification
  - MACD (12, 26, 9) — histogram value, signal line, direction
  - ADX (14-period) — with +DI/-DI and zone classification (SAFE/CAUTION/DANGER/DEATH_TRAP)
  - Stochastic %K, %D (14, 3, 3)
  - Volume ratio (current vs 20-period SMA)
  - ATR (14-period) for volatility/stop-loss calculation
  - Bollinger Bands (20, 2) with width % and position
- [x] Create signal scoring function that returns 0-5 based on Burry thresholds
- [ ] Store calculated indicators in `analyses` table (DEFERRED - use real-time endpoints)
- [ ] Show exact indicator values in the analysis result UI (DEFERRED - frontend update)

**Implementation Notes:**
- `calculate_sma()`, `calculate_ema()` - core moving averages
- `calculate_rsi()` - RSI with gain/loss averaging
- `calculate_macd()` - full MACD with signal line and histogram
- `calculate_adx()` - ADX with +DI/-DI and kill switch zones
- `calculate_stochastic()` - %K and %D oscillators
- `calculate_atr()` - Average True Range
- `calculate_bollinger_bands()` - with width % and position within bands
- `calculate_volume_ratio()` - current vs average volume
- `calculate_all_indicators()` - master function returning all indicators + signal count
- `/api/indicators/<symbol>` endpoint for direct indicator access

### 1.4 — Auto-Triggering Risk Management ✅
**Problem:** Logging a loss in the journal doesn't update `consecutive_losses` or activate turtle mode.
**Fix:** Wire journal outcomes to settings automatically.
**Tasks:**
- [x] When journal entry saved with outcome='loss':
  - Increment `consecutive_losses`
  - If consecutive_losses >= 2, auto-set `turtle_mode = true`
  - Calculate daily_loss_pct from today's journal entries
  - If daily_loss_pct >= 8%, set a `trading_locked` flag
- [x] When journal entry saved with outcome='win':
  - Reset `consecutive_losses` to 0
  - If 3+ consecutive wins, log "overconfidence warning"
- [x] Add `portfolio_heat` calculation:
  - Sum all open positions' (size * leverage) / portfolio balance * 100
  - `/api/risk/heat` endpoint returns heat + all risk settings
- [ ] Display heat gauge in header (DEFERRED - frontend update)
- [ ] Block new analyses with TRADE recommendation if heat > 80% (DEFERRED - frontend enforcement)
- [x] Add daily P&L tracking (calculated from today's journal entries)

**Implementation Notes:**
- `update_risk_settings_on_outcome()` - auto-updates consecutive_losses/wins, turtle_mode, daily_loss_pct, trading_locked
- `calculate_portfolio_heat()` - calculates heat from open journal positions
- Journal POST/PUT endpoints now trigger risk management automatically
- New settings: `consecutive_wins`, `portfolio_heat`, `trading_locked`
- `/api/risk/heat` endpoint returns full risk dashboard

### 1.5 — UI/UX Quick Wins
- [ ] Add streaming responses for Claude analysis (SSE endpoint)
- [ ] Add keyboard shortcuts (Ctrl+Enter to analyze, Esc to close modals)
- [ ] Add dark/light theme toggle (just kidding, dark only obviously)
- [ ] Show loading states with progress indicators during analysis
- [ ] Add "Copy trade params" button that copies entry/SL/TP to clipboard
- [ ] Fix mobile responsiveness for the analysis grid

*Note: 1.5 UI tasks deferred to future iteration - backend foundation complete*

---

## PHASE 2: WALLET INTEGRATION & EXECUTION (Week 3-4) ✅ COMPLETED
> Connect to your actual money and enable one-click execution

**Completed: February 3, 2026**

### 2.1 — MetaMask Integration (EVM Chains) ✅
**Purpose:** Trade on Ethereum, Base, Arbitrum, BSC — wherever EVM tokens live.
**Tasks:**
- [x] Implement wallet connection flow:
  - Detect MetaMask provider
  - Request account access
  - Display connected address + chain
- [x] Add chain detection (ETH mainnet, Base, Arbitrum, BSC, Polygon)
- [x] Wallet status in header with address display
- [x] Account/chain change event handling
- [ ] Token balance reading (DEFERRED - needs web3.js CDN)
- [ ] Token approval flow (DEFERRED - needs actual swap integration)
- [ ] 1inch Aggregator integration (DEFERRED - simulation mode for now)

**Implementation Notes:**
- `connectMetaMask()` - handles connection and chain detection
- `window.ethereum.on()` - listens for account/chain changes
- Chain mapping for ethereum, bsc, base, arbitrum, polygon

### 2.2 — Phantom Integration (Solana) ✅
**Purpose:** Trade SOL tokens, memecoins, Jupiter swaps — your main DEX playground.
**Tasks:**
- [x] Implement Phantom wallet connection:
  - Detect Phantom provider
  - Request connection
  - Display wallet address
- [x] Wallet disconnect handling
- [ ] SPL token balance reading (DEFERRED - needs @solana/web3.js)
- [ ] Jupiter Aggregator integration (DEFERRED - simulation mode for now)
- [ ] Priority fee estimation (DEFERRED)

**Implementation Notes:**
- `connectPhantom()` - handles Solana wallet connection
- `window.solana.connect()` / `disconnect()` - Phantom API
- Public key displayed in header

### 2.3 — Unified Execution Layer ✅
**Tasks:**
- [x] Create `/api/execute` endpoint that:
  1. Validates trade against risk rules (position size, portfolio heat, turtle mode)
  2. Checks signal count (reject if < 4 unless override)
  3. Checks safety score (reject if < 40)
  4. Returns transaction payload for frontend to sign
- [x] Build execution confirmation flow:
  - Analysis → TRADE recommendation → "Execute" button
  - Execution modal with all parameters
  - Safety check integration
  - Risk validation with warnings
  - Confirmation with trade summary
- [x] Create `/api/execute/confirm` endpoint:
  - Creates position record
  - Creates linked journal entry
  - Updates portfolio heat
- [x] Position tracking table with full schema
- [ ] OCO order simulation (DEFERRED to Phase 4 - needs background process)

**Implementation Notes:**
- `validate_trade_against_risk()` - enforces all risk rules
- `openExecutionModal()` - full trade execution UI
- `validateExecution()` - calls /api/execute and displays results
- `confirmExecution()` - logs trade (simulated signing for now)

### 2.4 — Honeypot Protection Layer ✅
**Tasks:**
- [x] Integrate honeypot.is API for EVM tokens:
  - Check if honeypot (can you sell?)
  - Get buy/sell tax percentages
  - Get holder concentration
- [x] Integrate rugcheck.xyz API for Solana tokens:
  - Get safety score
  - Get risk factors
  - Check mint/freeze authority
- [x] Calculate safety score (0-100) with issues list
- [x] Block execution if safety score < 40
- [x] Get liquidity depth from DEXScreener
- [x] `/api/safety-check/<token_address>` endpoint

**Implementation Notes:**
- `fetch_honeypot_check()` - EVM chain safety checks
- `fetch_solana_safety_check()` - Solana token checks via rugcheck.xyz
- `fetch_token_liquidity()` - DEXScreener liquidity data
- Safety score calculation with issue tracking
- [ ] Add "Safety Score" (0-100) displayed before execution
- [ ] Block execution if safety score < 40 (configurable threshold)

---

## PHASE 3: QUANTITATIVE EDGE (Week 5-7) ✅ COMPLETED
> Go beyond RSI/MACD — add techniques the smart money actually uses

**Completed: February 3, 2026**

### 3.1 — Kelly Criterion Position Sizing ✅
**What:** Mathematically optimal bet size based on your edge.
**Why:** Your current sizing is rule-of-thumb (60-80%). Kelly tells you the EXACT optimal size.
**Formula:**
```
Kelly % = W - [(1 - W) / R]

Where:
  W = historical win rate (e.g., 0.708 for Penguin)
  R = avg win / avg loss ratio (e.g., 18% / 12% = 1.5)

Kelly = 0.708 - [(1 - 0.708) / 1.5] = 0.708 - 0.195 = 0.513 = 51.3%

Half-Kelly (safer) = 25.6%
Quarter-Kelly (conservative) = 12.8%
```
**Tasks:**
- [x] Calculate Kelly % from journal data (per strategy)
- [x] Implement Half-Kelly as default recommendation (full Kelly is too aggressive)
- [x] Show Kelly-optimal size alongside your rule-based size in analysis results
- [x] Track if you're over/under Kelly historically (are you betting too much or too little?)
- [x] Add Kelly to the journal stats dashboard

**Implementation Notes:**
- `calculate_kelly_criterion()` - calculates full/half/quarter Kelly from journal data
- `/api/quant/kelly` - get Kelly for specific strategy
- `/api/quant/kelly/all` - get Kelly for all strategies at once
- Displays recommended size (half-Kelly) + expected value
- Quant tab shows Kelly breakdown by strategy

### 3.2 — Volatility Regime Detection ✅
**What:** Markets switch between low-vol (grinding) and high-vol (explosive) regimes. Your strategies work differently in each.
**Why:** Burry works best in high-vol blow-offs. Penguin works in fear-rotation regimes. Trading the wrong strategy in the wrong regime = losses.
**Implementation:**
```
Regime Detection Methods:
1. ATR percentile (current ATR vs 90-day range)
   - Bottom 25% = Low Vol → favor mean reversion, tighter stops
   - Top 25% = High Vol → favor momentum, wider stops
   
2. Bollinger Band Width (measures volatility expansion/contraction)
   - Squeeze (narrow bands) = explosion coming, wait for breakout
   - Expansion = trend in progress, ride it
   
3. VIX / Crypto Fear & Greed Index
   - < 25 = Extreme Fear → Penguin territory (divergence plays)
   - > 75 = Extreme Greed → Burry territory (exhaustion tops)
   
4. Realized vs Implied Volatility spread
   - RV > IV = market underpricing risk → buy protection
   - IV > RV = market overpricing risk → sell premium
```
**Tasks:**
- [x] Calculate ATR percentile for each asset
- [x] Implement Bollinger Band Width indicator
- [x] Fetch Fear & Greed Index (alternative.me API for crypto, CNN for stocks)
- [x] Create regime classification: CALM / NORMAL / VOLATILE / EXTREME
- [x] Adjust strategy recommendations based on regime:
  - CALM → reduce leverage, widen time horizon, consider swing trades
  - VOLATILE → your Penguin/Burry sweet spot, full system
  - EXTREME → reduce size to Quarter-Kelly, defensive only
- [x] Display current regime in header alongside turtle mode banner
- [ ] Log regime at time of each trade for post-analysis (DEFERRED - Phase 4)

**Implementation Notes:**
- `calculate_atr_percentile()` - ATR vs 90-day historical range
- `calculate_bollinger_band_squeeze()` - detects volatility squeezes
- `fetch_fear_greed_index()` - alternative.me Crypto Fear & Greed API
- `classify_volatility_regime()` - combines all signals into CALM/NORMAL/VOLATILE/EXTREME
- `/api/quant/regime/<symbol>` - get full regime analysis
- `/api/quant/fear-greed` - get Fear & Greed Index with trading interpretation

### 3.3 — Funding Rate Analysis (Crypto Perps) ✅
**What:** Perpetual futures funding rates reveal positioning sentiment. When longs pay shorts (positive funding), the market is overleveraged long. Vice versa.
**Why:** Extreme funding = crowded trade = mean reversion incoming. This is a leading indicator your current system doesn't have.
**Implementation:**
```
Funding Rate Signals:
- Rate > +0.05% per 8h = Market overleveraged LONG → Burry setup forming
- Rate < -0.05% per 8h = Market overleveraged SHORT → squeeze incoming (Penguin)
- Rate > +0.1% = EXTREME → high probability of long liquidation cascade
- Rate < -0.1% = EXTREME → high probability of short squeeze

Annualized cost: Rate × 3 × 365
At 0.1% per 8h = 109.5% annualized → longs are paying $109.50/year per $100
This is unsustainable → reversion is coming
```
**Tasks:**
- [x] Fetch funding rates from Binance/Bybit API (or via CoinGlass)
- [x] Calculate current rate, 7-day average, and percentile rank
- [x] Add funding rate as a BONUS signal to both strategies:
  - Penguin: negative funding = +1 bonus signal
  - Burry: extreme positive funding = +1 bonus signal
- [x] Display funding rate in analysis results and market check
- [x] Alert when funding hits extreme levels (> |0.08%|)
- [ ] Track historical funding vs price to validate signal quality (DEFERRED)

**Implementation Notes:**
- `fetch_funding_rate()` - fetches from Binance FAPI for perpetual futures
- Supports 20+ major symbols (BTC, ETH, SOL, etc.)
- Returns current rate, 7-day avg, annualized %, signal classification
- `/api/quant/funding/<symbol>` - endpoint for funding rate data

### 3.4 — On-Chain Flow Analysis
**What:** Track where tokens are moving — exchange inflows (selling pressure), whale accumulation, smart money movements.
**Why:** Price follows flow. If whales are dumping to exchanges before a pump stalls, that's a Burry signal you can't see on a chart.
**Data Sources:**
```
Key On-Chain Metrics:
1. Exchange Net Flow (inflow - outflow)
   - Large inflow = selling pressure incoming
   - Large outflow = accumulation (bullish)

2. Whale Transaction Count (txns > $100K)
   - Spike in whale txns = big move coming
   - Direction of flow tells you which way

3. Active Addresses (momentum proxy)
   - Rising active addresses = genuine interest
   - Falling active addresses during pump = retail exhaustion (Burry signal)

4. Token Unlock Schedule
   - Large unlocks = supply shock incoming
   - Know the dates before you trade
```
**Tasks:**
- [ ] Integrate Blockchain.com API or Glassnode lite for BTC exchange flows
- [ ] Integrate Solscan/Helius API for Solana token holder analysis
- [ ] Track top 10 holders' recent activity for any token you're analyzing
- [ ] Add "Smart Money Flow" indicator: net exchange flow + whale direction
- [ ] Display on-chain data in analysis results alongside TA signals
- [ ] Add whale alert monitoring for tokens on watchlist

*Note: On-chain flow analysis deferred to Phase 4 - requires paid API access (Glassnode/Nansen)*

### 3.5 — Correlation & Regime Analysis ✅
**What:** Understanding what moves together and when correlations break.
**Why:** Your Penguin strategy IS a correlation play (BTC dumps, alts pump = correlation breakdown). Quantifying this makes it precise.
**Tasks:**
- [x] Calculate rolling 30-day correlation matrix for:
  - BTC vs top 10 alts
  - BTC vs SPY/QQQ (macro correlation)
  - SOL vs ETH (layer 1 rotation)
- [x] Detect correlation breakdowns (when 30d correlation drops below 0.3 from 0.7+)
  - Breakdown = divergence opportunity (Penguin trigger)
- [x] Implement beta calculation for each alt vs BTC
  - High beta (> 1.5) = amplified moves → better for Penguin divergence
  - Low beta (< 0.5) = defensive → skip for divergence plays
- [x] Display correlation heatmap in a new "Quant" tab
- [ ] Add "Divergence Strength Score" that combines:
  - Current correlation deviation from mean
  - Funding rate divergence
  - Volume divergence
  - On-chain flow divergence

**Implementation Notes:**
- `calculate_correlation()` - Pearson correlation between price series
- `calculate_beta()` - asset sensitivity to benchmark (BTC)
- `fetch_correlation_matrix()` - gets correlations for multiple symbols
- `detect_correlation_breakdown()` - identifies divergence opportunities
- `/api/quant/correlation` - correlation matrix endpoint (customizable symbols)
- Quant tab shows correlation table with color-coded values + betas

### 3.6 — Monte Carlo Trade Simulation
**What:** Before entering a trade, simulate 10,000 possible outcomes based on your historical stats.
**Why:** Tells you the probability distribution of outcomes, not just "this looks good." You see: "there's a 72% chance of +$15-25 and a 28% chance of -$3-8."
**Tasks:**
- [ ] Build Monte Carlo engine using journal data:
  - Input: strategy, signal count, current conditions
  - Sample from historical win rate and P&L distributions
  - Run 10,000 simulations
  - Output: probability of profit, expected value, worst case (5th percentile), best case (95th percentile)
- [ ] Display probability distribution chart in analysis results
- [ ] Calculate Expected Value for each trade:
  ```
  EV = (Win Rate × Avg Win) - (Loss Rate × Avg Loss)
  EV = (0.708 × 18%) - (0.292 × 12%) = 12.74% - 3.50% = +9.24%
  ```
- [ ] Only show TRADE recommendation if EV > 5% (configurable threshold)
- [ ] Track predicted vs actual outcomes to calibrate the model

### 3.7 — Volume Profile & VWAP
**What:** Volume Profile shows WHERE the most trading happened (price levels). VWAP shows the average price weighted by volume — the "fair value" line.
**Why:** Institutions trade around VWAP. High Volume Nodes (HVN) act as support/resistance that RSI can't see. Low Volume Nodes (LVN) are "air pockets" where price moves fast.
**Tasks:**
- [ ] Calculate VWAP from intraday OHLCV data
- [ ] Identify High Volume Nodes (top 20% of volume by price level)
- [ ] Identify Low Volume Nodes (bottom 20%)
- [ ] Add to analysis:
  - "Price is above/below VWAP" (institutional bias)
  - "Nearest HVN support: $X" (stronger than regular S/R)
  - "LVN gap between $X-$Y" (price could move fast through here)
- [ ] Use VWAP as dynamic stop-loss reference (more sophisticated than fixed %)

### 3.8 — Advanced Risk Metrics Dashboard ✅
**Tasks:**
- [x] Calculate and display:
  - **Sharpe Ratio**: (avg return - risk free rate) / std deviation of returns
    - > 2.0 = excellent, 1.0-2.0 = good, < 1.0 = needs work
  - **Sortino Ratio**: like Sharpe but only penalizes downside volatility
    - Better metric for directional traders (which you are)
  - **Max Drawdown**: largest peak-to-trough decline
    - Your current system should never exceed -15% weekly
  - **Calmar Ratio**: annualized return / max drawdown
  - **Win/Loss Streak Analysis**: longest consecutive wins/losses
  - **Profit Factor**: gross profit / gross loss (> 2.0 is elite)
  - **Expectancy per trade**: (win rate × avg win) - (loss rate × avg loss)
- [ ] Equity curve chart (portfolio value over time) (DEFERRED - needs more journal data)
- [ ] Drawdown chart (shows every drawdown depth and recovery) (DEFERRED)
- [ ] Monthly P&L heatmap calendar (DEFERRED)
- [ ] Strategy comparison table (Penguin vs Burry vs Scalp performance)

**Implementation Notes:**
- `calculate_advanced_risk_metrics()` - calculates Sharpe, Sortino, Profit Factor, Max DD, streaks
- `/api/quant/risk-metrics` - endpoint for all risk metrics
- Quant tab displays all metrics with color-coded ratings

---

## PHASE 4: AUTOMATION & REAL-TIME (Week 8-10) ✅ COMPLETED
> Make the platform work for you while you sleep (responsibly)

**Completed: February 3, 2026**

### 4.1 — WebSocket Price Feeds
**Tasks:**
- [ ] Connect to Binance WebSocket for crypto real-time prices (DEFERRED - using polling instead)
- [ ] Connect to CoinGecko WebSocket for broader coverage (DEFERRED)
- [ ] Implement server-side WebSocket relay (Flask-SocketIO or switch to FastAPI) (DEFERRED)
- [x] Push real-time prices to frontend for:
  - Watchlist live prices (via polling)
  - Open position P&L updates (background monitor)
  - Alert trigger checking (every 30s)
- [ ] Add price sparkline charts to watchlist items (DEFERRED)

**Implementation Notes:**
- Using background thread with 30-second polling instead of WebSockets for simplicity
- Background monitor auto-starts on app launch
- Position P&L updates on every check cycle

### 4.2 — Alert System ✅
**Types of Alerts:**
```
1. Price Alerts: "SOL hits $200" → notification ✓
2. Indicator Alerts: "BTC RSI crosses 80" → Burry scan triggered ✓
3. Divergence Alerts: "BTC -5%, SOL +12%" → Penguin signal detected ✓
4. Funding Rate Alerts: "BTC funding > 0.08%" → exhaustion watch ✓
5. Portfolio Alerts: "Heat > 60%" → reduce exposure ✓
6. Risk Alerts: "2 consecutive losses" → turtle mode activated ✓
7. Whale Alerts: "100K+ SOL moved to exchange" → selling pressure (DEFERRED - needs on-chain API)
```
**Tasks:**
- [x] Create `alerts` table with condition definitions
- [x] Background thread that checks conditions every 30 seconds
- [x] Browser notifications (Notification API)
- [ ] Optional: Telegram bot integration for mobile alerts (DEFERRED)
- [ ] Optional: Discord webhook for alerts (DEFERRED)
- [x] Alert history log with timestamps and what was triggered
- [ ] "Smart Alert" — Claude evaluates the alert context and adds commentary (DEFERRED)

**Implementation Notes:**
- `alerts` table stores all alert definitions with flexible conditions
- `alert_history` table logs all triggered alerts
- `triggered_alerts_queue` - in-memory queue for real-time frontend polling
- Alert types: price, indicator, divergence, funding, portfolio
- Conditions: crosses_above, crosses_below, reaches, rsi_above, rsi_below, etc.
- Browser notifications via Notification API with permission request

### 4.3 — Auto-Scanner (Penguin/Burry Signal Detection) ✅
**Tasks:**
- [x] Background process that runs every 5 minutes:
  1. Fetch BTC price change (1h, 4h, 24h)
  2. For each watchlist token, fetch price + indicators
  3. Score against Penguin criteria (divergence + RSI + volume + support)
  4. Score against Burry criteria (RSI + MACD + ADX + Stoch + volume)
  5. If score >= 4/5, push alert with full analysis
- [ ] Configurable scan interval (1min for scalping, 15min for swing) (DEFERRED)
- [x] Scan results dashboard showing all current setups ranked by signal count
- [x] "Opportunity Feed" — chronological list of detected setups
- [x] One-click "Analyze" button on any detected setup (sends to Claude for deep analysis)

**Implementation Notes:**
- `score_penguin_setup()` - scores against Penguin criteria (BTC weakness, divergence, RSI, volume, support)
- `score_burry_setup()` - scores against Burry criteria (RSI extreme, MACD bearish, ADX safe, Stoch, volume)
- ADX kill switch implemented - ADX > 50 nullifies Burry setup
- `scan_results` table stores all scan outputs
- Scans run every 5 minutes via background monitor
- `/api/scan/setups` returns current tradeable setups (4+ signals from last hour)

### 4.4 — Position Monitoring & Auto-Exit ✅
**Tasks:**
- [x] Background price monitoring for all open positions
- [x] Auto stop-loss execution (notification + position status update)
- [x] Auto take-profit (same flow)
- [ ] Trailing stop implementation (DEFERRED - Phase 5)
- [ ] Position aging alerts (DEFERRED)
- [ ] Break-even stop trigger after first TP hit (DEFERRED)

**Implementation Notes:**
- `check_position_pnl()` - monitors all open positions every 30 seconds
- Updates current_price, unrealized_pnl, unrealized_pnl_pct
- Checks SL/TP levels and triggers alerts
- OCO logic implemented - SL takes priority, cancels TP when hit
- Auto-updates linked journal entries on OCO trigger

### 4.5 — Trade Execution Queue / OCO Orders ✅
**Tasks:**
- [ ] Implement order queue for planned trades (DEFERRED - manual execution for now)
- [x] Conditional order types:
  - OCO (one cancels other — SL and TP linked)
- [ ] DCA (Dollar Cost Average) mode (DEFERRED)
- [ ] TWAP execution (DEFERRED)

**Implementation Notes:**
- OCO logic in `check_position_pnl()`:
  - When SL hits, TP is effectively canceled
  - When TP hits (and SL hasn't), SL is effectively canceled
  - Position marked as `oco_triggered` status
  - Linked journal entry auto-closed with outcome

---

## PHASE 5: INTELLIGENCE & OPTIMIZATION (Week 11-14)
> The platform learns from your history and gets smarter over time

### 5.1 — Backtesting Engine
**Tasks:**
- [ ] Fetch historical OHLCV data (1 year minimum)
- [ ] Replay Penguin strategy against historical data:
  - For each candle: calculate indicators → check signal criteria → simulate entry/exit
  - Track: entries, exits, P&L, drawdowns, win rate
- [ ] Replay Burry strategy against historical data (same flow)
- [ ] Generate backtest report:
  - Total trades, win rate, profit factor, max drawdown
  - Equity curve
  - Monthly returns breakdown
  - Best/worst trades
- [ ] Parameter optimization:
  - Test RSI thresholds (75-85 range) to find optimal
  - Test ADX thresholds (25-35 range)
  - Test position sizes
  - Test stop-loss distances
  - Output: optimal parameters for each strategy
- [ ] Walk-forward validation (backtest on 80%, validate on 20%)
- [ ] Survivorship bias awareness (don't test on tokens that got rugged)

### 5.2 — Pattern Recognition Enhancement
**Tasks:**
- [ ] Build database of past trade outcomes with full indicator snapshots
- [ ] Use Claude to analyze patterns in winning vs losing trades:
  - "What did my winning Penguin trades have in common?"
  - "What early signals predicted my largest losses?"
- [ ] Implement simple pattern matching:
  - Current setup similarity score vs historical winners
  - "This setup is 87% similar to your 3 best Penguin trades"
- [ ] Track time-of-day performance (when are you sharpest?)
- [ ] Track day-of-week performance
- [ ] Track performance by market regime
- [ ] Generate weekly "Strategy Health Report" via Claude

### 5.3 — Market Regime Classification (Hidden Markov Model)
**What:** HMMs detect hidden market states (trending, mean-reverting, chaotic) from observable data.
**Why:** Your strategies have regime-dependent performance. Penguin dominates in fear-rotation. Burry dominates in blow-off tops. Neither works in choppy ranging markets.
**Tasks:**
- [ ] Implement 3-state HMM:
  - State 1: TRENDING (ADX > 30, directional, momentum strategies work)
  - State 2: MEAN-REVERTING (ADX < 20, range-bound, fade extremes)
  - State 3: TRANSITIONAL (high vol, no direction, reduce exposure)
- [ ] Train on historical BTC + SPY data
- [ ] Display current detected regime prominently
- [ ] Auto-adjust strategy weights:
  - TRENDING → Penguin (ride the move)
  - MEAN-REVERTING → Burry (fade the extremes)
  - TRANSITIONAL → HOLD (reduce all sizes by 50%)
- [ ] Regime change alerts ("Market shifted from TRENDING to TRANSITIONAL")

### 5.4 — Performance Attribution
**Tasks:**
- [ ] Break down P&L by source:
  - How much came from Penguin vs Burry vs Scalp?
  - How much came from crypto vs stocks?
  - How much came from BTC-correlated vs independent moves?
  - How much came from timing (entry) vs sizing vs exit management?
- [ ] Identify your actual edge:
  - "72% of your profits come from Penguin trades during US hours on high-vol days"
  - "Your Burry trades break even — consider dropping leverage from 50x to 30x"
- [ ] Recommendations engine:
  - "Based on last 30 trades: increase Penguin allocation, reduce Burry leverage, avoid trading before 9am PST"
- [ ] Weekly/monthly review report generated by Claude analyzing your journal

### 5.5 — Risk Simulation & Stress Testing
**Tasks:**
- [ ] "What if" scenario analysis:
  - "What happens to my portfolio if BTC drops 20%?"
  - "What's my max loss if all open positions hit stop?"
  - "How many losing trades in a row can I survive?"
- [ ] Ruin probability calculation:
  ```
  P(ruin) = ((1 - edge) / (1 + edge)) ^ (bankroll / bet_size)
  
  With 70% win rate and 2:1 R/R:
  Edge = 0.70 × 2 - 0.30 × 1 = 1.10
  At 10% position size: P(ruin) ≈ 0.0001 (safe)
  At 50% position size: P(ruin) ≈ 0.12 (dangerous)
  ```
- [ ] Optimal bankroll management based on ruin probability
- [ ] Stress test against historical black swan events:
  - March 2020 COVID crash
  - May 2021 crypto crash
  - FTX collapse November 2022
  - Recent major moves
- [ ] Display "Account Survival Probability" metric

---

## ARCHITECTURE NOTES

### Migration Path
Phase 1-2 can stay single-file Flask. By Phase 3, consider:
```
Option A: Stay Python monolith
  - Flask → FastAPI (async, WebSocket support)
  - Add Celery + Redis for background tasks
  - SQLite → PostgreSQL (if data grows)

Option B: Split into services (your Rust expertise)
  - Rust backend for execution + real-time data (speed critical)
  - Python service for Claude analysis + quant calculations
  - SQLite stays fine for personal use
  - React frontend (if vanilla JS gets unwieldy)
```

### Security Considerations
- [ ] Never store private keys server-side — wallet signing happens client-side only
- [ ] Add API key encryption at rest
- [ ] Add session tokens for wallet-connected state
- [ ] Rate limit execution endpoints
- [ ] Add transaction simulation before every execution
- [ ] Implement spending limits (max trade size, daily limit)

### Data Storage Growth Plan
```
Phase 1-2: SQLite (fine for single user)
Phase 3-4: SQLite + JSON files for large datasets (OHLCV history)
Phase 5:   Consider TimescaleDB if tick data storage needed
```

---

## PRIORITY MATRIX

| Task | Impact | Effort | Priority |
|------|--------|--------|----------|
| Real data feeds (1.2) | HIGH | MEDIUM | DO FIRST |
| Programmatic indicators (1.3) | HIGH | MEDIUM | DO FIRST |
| Stateful chat (1.1) | MEDIUM | LOW | DO FIRST |
| Auto risk triggers (1.4) | HIGH | LOW | DO FIRST |
| Phantom wallet (2.2) | HIGH | HIGH | DO SECOND |
| MetaMask wallet (2.1) | MEDIUM | HIGH | DO SECOND |
| Kelly Criterion (3.1) | HIGH | LOW | DO SECOND |
| Funding rates (3.3) | HIGH | LOW | DO SECOND |
| Vol regime detection (3.2) | HIGH | MEDIUM | DO THIRD |
| Monte Carlo (3.6) | MEDIUM | MEDIUM | DO THIRD |
| On-chain flows (3.4) | MEDIUM | HIGH | DO THIRD |
| WebSocket feeds (4.1) | HIGH | HIGH | DO FOURTH |
| Alert system (4.2) | HIGH | MEDIUM | DO FOURTH |
| Auto-scanner (4.3) | HIGH | HIGH | DO FOURTH |
| Backtesting (5.1) | HIGH | HIGH | DO FIFTH |
| Regime detection (5.3) | MEDIUM | HIGH | DO FIFTH |

---

## KEY QUANT CONCEPTS REFERENCE

### Kelly Criterion
Optimal bet sizing. Half-Kelly is the sweet spot — full Kelly maximizes growth but with brutal drawdowns.

### Expected Value (EV)
```
EV = (P(win) × win_amount) - (P(loss) × loss_amount)
```
Never take negative EV trades. Period.

### Sharpe Ratio
```
Sharpe = (Return - Risk_Free_Rate) / Std_Dev_of_Returns
```
Target > 2.0. Below 1.0 means you're taking too much risk for the return.

### Sortino Ratio
Like Sharpe but only counts downside volatility. Better for directional traders because upside volatility is a GOOD thing.

### Profit Factor
```
Profit Factor = Gross Profits / Gross Losses
```
> 2.0 = elite. 1.5-2.0 = good. < 1.0 = you're losing money.

### Maximum Drawdown
Largest peak-to-trough decline in your equity curve. Your system should cap this at ~15% weekly.

### Ruin Probability
The mathematical probability of losing your entire account given your win rate, reward/risk, and position sizing. Kelly Criterion minimizes this.

### Correlation Breakdown
When historically correlated assets decouple — this IS the Penguin signal, quantified.

### Funding Rate Edge
When everyone is on one side of a perp trade, the funding rate goes extreme. This is unsustainable and mean-reverts. It's a timing tool for your existing strategies.

### Volume Profile
Shows price levels where the most volume traded. High Volume Nodes act as magnets (price returns to them). Low Volume Nodes are air pockets (price moves fast through them).

---

*"The money is made in the waiting, not the trading." — but when you trade, trade with every edge stacked in your favor.* 🐧
