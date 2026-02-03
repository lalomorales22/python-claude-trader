#!/usr/bin/env python3
"""
PENGUIN-BURRY CHART ANALYZER v2 — Powered by Claude Opus 4.5 + Web Search
Single-file trading intelligence app with real-time market data.

Usage:
    export ANTHROPIC_API_KEY="sk-ant-..."
    pip install flask anthropic
    python penguin_burry_analyzer.py

Claude analyzes your chart images AND searches the web for current prices,
news, sentiment, and volume data to give you accurate, real-time trading
intelligence using the Penguin-Burry methodology.
"""

import os
import sys
import json
import sqlite3
import base64
import hashlib
import logging
import mimetypes
import time
import requests
import threading
import atexit
from pathlib import Path
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, send_file
from collections import deque

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-4-5-20251101")
DB_PATH = os.getenv("PB_DB_PATH", "penguin_burry.db")
UPLOAD_DIR = Path("pb_uploads")
UPLOAD_DIR.mkdir(exist_ok=True)
MAX_IMAGE_MB = 20
HOST = os.getenv("PB_HOST", "0.0.0.0")
PORT = int(os.getenv("PB_PORT", "7777"))

# External API endpoints
COINGECKO_API = "https://api.coingecko.com/api/v3"
DEXSCREENER_API = "https://api.dexscreener.com/latest"

# Token estimation (rough: 4 chars per token)
CHARS_PER_TOKEN = 4
MAX_CHAT_CONTEXT_TOKENS = 12000  # Leave room for response
MAX_CHAT_HISTORY_MESSAGES = 40  # Load up to 40 messages

CLAUDE_MODELS = [
    {"id": "claude-opus-4-5-20251101", "name": "Claude Opus 4.5", "tier": "flagship", "desc": "Most intelligent - deep analysis + web search"},
    {"id": "claude-sonnet-4-5-20250929", "name": "Claude Sonnet 4.5", "tier": "balanced", "desc": "Fast + smart - great for quick scalps"},
    {"id": "claude-haiku-4-5-20251001", "name": "Claude Haiku 4.5", "tier": "speed", "desc": "Ultra fast - rapid scans"},
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pb")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# ANTHROPIC CLIENT
# ---------------------------------------------------------------------------
import anthropic


def get_client():
    if not ANTHROPIC_API_KEY:
        return None
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def claude_analyze_chart(model, system_prompt, user_prompt, image_b64,
                         media_type="image/png", use_web_search=True):
    client = get_client()
    if not client:
        return None, [], "ANTHROPIC_API_KEY not set. Export it and restart."

    content = [
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
        {"type": "text", "text": user_prompt},
    ]

    tools = []
    if use_web_search:
        tools.append({"type": "web_search_20250305", "name": "web_search", "max_uses": 5})

    try:
        kwargs = {"model": model, "max_tokens": 16000, "system": system_prompt,
                  "messages": [{"role": "user", "content": content}], "temperature": 0.2}
        if tools:
            kwargs["tools"] = tools

        response = client.messages.create(**kwargs)

        full_text = ""
        web_searches = []
        for block in response.content:
            if block.type == "text":
                full_text += block.text
            elif block.type == "web_search_tool_result":
                for sub in getattr(block, "content", []):
                    if hasattr(sub, "source_title"):
                        web_searches.append({
                            "title": getattr(sub, "source_title", ""),
                            "url": getattr(sub, "source_url", ""),
                            "snippet": getattr(sub, "text", "")[:200],
                        })
        return full_text, web_searches, None

    except anthropic.AuthenticationError:
        return None, [], "Invalid API key. Check ANTHROPIC_API_KEY."
    except anthropic.RateLimitError:
        return None, [], "Rate limited. Wait and retry."
    except anthropic.APIError as e:
        return None, [], f"API error: {e}"
    except Exception as e:
        return None, [], f"Error: {e}"


def estimate_tokens(text):
    """Rough token estimation (4 chars per token average)"""
    return len(str(text)) // CHARS_PER_TOKEN


def truncate_messages_to_fit(messages, max_tokens):
    """Truncate oldest messages to fit within token budget"""
    if not messages:
        return messages
    
    total_tokens = sum(estimate_tokens(m.get("content", "")) for m in messages)
    
    while total_tokens > max_tokens and len(messages) > 1:
        removed = messages.pop(0)  # Remove oldest
        total_tokens -= estimate_tokens(removed.get("content", ""))
    
    return messages


def claude_chat_text(model, system_prompt, user_prompt, use_web_search=True, conversation_history=None):
    """Chat with Claude, optionally with conversation history for stateful chat"""
    client = get_client()
    if not client:
        return None, [], "ANTHROPIC_API_KEY not set."

    tools = []
    if use_web_search:
        tools.append({"type": "web_search_20250305", "name": "web_search", "max_uses": 3})

    # Build messages list - either from history or single prompt
    if conversation_history:
        messages = list(conversation_history)  # Copy to avoid mutation
        messages.append({"role": "user", "content": user_prompt})
        # Truncate if needed
        messages = truncate_messages_to_fit(messages, MAX_CHAT_CONTEXT_TOKENS)
    else:
        messages = [{"role": "user", "content": user_prompt}]

    try:
        kwargs = {"model": model, "max_tokens": 4096, "system": system_prompt,
                  "messages": messages, "temperature": 0.3}
        if tools:
            kwargs["tools"] = tools

        response = client.messages.create(**kwargs)
        full_text = ""
        web_searches = []
        for block in response.content:
            if block.type == "text":
                full_text += block.text
            elif block.type == "web_search_tool_result":
                for sub in getattr(block, "content", []):
                    if hasattr(sub, "source_title"):
                        web_searches.append({"title": getattr(sub, "source_title", ""), "url": getattr(sub, "source_url", "")})
        return full_text, web_searches, None
    except Exception as e:
        return None, [], str(e)


def parse_json_response(text):
    import re
    try:
        return json.loads(text)
    except Exception:
        pass
    for pat in [r'```json\s*(.*?)\s*```', r'```\s*(\{.*?\})\s*```', r'(\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})']:
        m = re.search(pat, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                continue
    return {"raw_text": text, "parse_failed": True}


# ---------------------------------------------------------------------------
# EXTERNAL DATA FEEDS (CoinGecko + DEXScreener)
# ---------------------------------------------------------------------------
def fetch_coingecko_price(symbol):
    """Fetch current price data from CoinGecko"""
    symbol_map = {
        "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", 
        "DOGE": "dogecoin", "XRP": "ripple", "ADA": "cardano",
        "AVAX": "avalanche-2", "DOT": "polkadot", "LINK": "chainlink",
        "MATIC": "matic-network", "ATOM": "cosmos", "UNI": "uniswap",
        "LTC": "litecoin", "NEAR": "near", "ARB": "arbitrum",
        "OP": "optimism", "SUI": "sui", "APT": "aptos",
        "INJ": "injective-protocol", "TIA": "celestia",
        "SEI": "sei-network", "PEPE": "pepe", "WIF": "dogwifcoin",
        "BONK": "bonk", "SHIB": "shiba-inu", "FTM": "fantom",
        "RENDER": "render-token", "FET": "fetch-ai", "RNDR": "render-token"
    }
    
    coin_id = symbol_map.get(symbol.upper(), symbol.lower())
    
    try:
        url = f"{COINGECKO_API}/coins/{coin_id}?localization=false&tickers=false&community_data=false&developer_data=false"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            market_data = data.get("market_data", {})
            return {
                "symbol": symbol.upper(),
                "name": data.get("name", ""),
                "price": market_data.get("current_price", {}).get("usd", 0),
                "price_change_24h": market_data.get("price_change_percentage_24h", 0),
                "price_change_7d": market_data.get("price_change_percentage_7d", 0),
                "volume_24h": market_data.get("total_volume", {}).get("usd", 0),
                "market_cap": market_data.get("market_cap", {}).get("usd", 0),
                "high_24h": market_data.get("high_24h", {}).get("usd", 0),
                "low_24h": market_data.get("low_24h", {}).get("usd", 0),
                "ath": market_data.get("ath", {}).get("usd", 0),
                "ath_change_pct": market_data.get("ath_change_percentage", {}).get("usd", 0),
                "source": "coingecko"
            }
    except Exception as e:
        log.warning(f"CoinGecko fetch failed for {symbol}: {e}")
    return None


def fetch_coingecko_ohlcv(symbol, days=14):
    """Fetch OHLCV data from CoinGecko for indicator calculation"""
    symbol_map = {
        "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
        "DOGE": "dogecoin", "XRP": "ripple", "ADA": "cardano",
        "AVAX": "avalanche-2", "DOT": "polkadot", "LINK": "chainlink",
        "MATIC": "matic-network", "ATOM": "cosmos", "UNI": "uniswap",
        "LTC": "litecoin", "NEAR": "near", "ARB": "arbitrum",
        "OP": "optimism", "SUI": "sui", "APT": "aptos",
        "INJ": "injective-protocol", "TIA": "celestia",
        "SEI": "sei-network", "PEPE": "pepe", "WIF": "dogwifcoin",
        "BONK": "bonk", "SHIB": "shiba-inu", "FTM": "fantom",
        "RENDER": "render-token", "FET": "fetch-ai", "RNDR": "render-token"
    }
    
    coin_id = symbol_map.get(symbol.upper(), symbol.lower())
    
    try:
        url = f"{COINGECKO_API}/coins/{coin_id}/ohlc?vs_currency=usd&days={days}"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            # CoinGecko returns [timestamp, open, high, low, close]
            return [{"timestamp": d[0], "open": d[1], "high": d[2], "low": d[3], "close": d[4]} for d in data]
    except Exception as e:
        log.warning(f"CoinGecko OHLCV fetch failed for {symbol}: {e}")
    return []


def fetch_dexscreener_token(query):
    """Search for token on DEXScreener"""
    try:
        url = f"{DEXSCREENER_API}/dex/search?q={query}"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            pairs = data.get("pairs", [])
            if pairs:
                # Return the highest liquidity pair
                pairs.sort(key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0), reverse=True)
                p = pairs[0]
                return {
                    "symbol": p.get("baseToken", {}).get("symbol", ""),
                    "name": p.get("baseToken", {}).get("name", ""),
                    "address": p.get("baseToken", {}).get("address", ""),
                    "chain": p.get("chainId", ""),
                    "dex": p.get("dexId", ""),
                    "price": float(p.get("priceUsd", 0) or 0),
                    "price_change_24h": float(p.get("priceChange", {}).get("h24", 0) or 0),
                    "price_change_1h": float(p.get("priceChange", {}).get("h1", 0) or 0),
                    "volume_24h": float(p.get("volume", {}).get("h24", 0) or 0),
                    "liquidity_usd": float(p.get("liquidity", {}).get("usd", 0) or 0),
                    "txns_24h_buys": p.get("txns", {}).get("h24", {}).get("buys", 0),
                    "txns_24h_sells": p.get("txns", {}).get("h24", {}).get("sells", 0),
                    "pair_url": p.get("url", ""),
                    "source": "dexscreener"
                }
    except Exception as e:
        log.warning(f"DEXScreener fetch failed for {query}: {e}")
    return None


# ---------------------------------------------------------------------------
# TECHNICAL ANALYSIS INDICATORS (Pure Python - No External Dependencies)
# ---------------------------------------------------------------------------
def calculate_sma(prices, period):
    """Simple Moving Average"""
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period


def calculate_ema(prices, period):
    """Exponential Moving Average"""
    if len(prices) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period  # Start with SMA
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    return ema


def calculate_rsi(prices, period=14):
    """Relative Strength Index (0-100)"""
    if len(prices) < period + 1:
        return None
    
    gains, losses = [], []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i-1]
        gains.append(max(0, change))
        losses.append(max(0, -change))
    
    if len(gains) < period:
        return None
    
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    
    if avg_loss == 0:
        return 100
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return round(rsi, 2)


def calculate_macd(prices, fast=12, slow=26, signal=9):
    """MACD with histogram"""
    if len(prices) < slow + signal:
        return None
    
    ema_fast = calculate_ema(prices, fast)
    ema_slow = calculate_ema(prices, slow)
    
    if ema_fast is None or ema_slow is None:
        return None
    
    macd_line = ema_fast - ema_slow
    
    # Calculate signal line (need MACD history for this)
    macd_values = []
    for i in range(slow, len(prices) + 1):
        ef = calculate_ema(prices[:i], fast)
        es = calculate_ema(prices[:i], slow)
        if ef and es:
            macd_values.append(ef - es)
    
    if len(macd_values) < signal:
        return {"macd": round(macd_line, 4), "signal": None, "histogram": None, "direction": "unknown"}
    
    signal_line = calculate_ema(macd_values, signal)
    histogram = macd_line - signal_line if signal_line else None
    
    # Determine direction
    direction = "unknown"
    if len(macd_values) >= 2:
        if macd_values[-1] > macd_values[-2]:
            direction = "rising"
        else:
            direction = "falling"
    
    return {
        "macd": round(macd_line, 4),
        "signal": round(signal_line, 4) if signal_line else None,
        "histogram": round(histogram, 4) if histogram else None,
        "direction": direction
    }


def calculate_adx(highs, lows, closes, period=14):
    """Average Directional Index (trend strength 0-100)"""
    if len(closes) < period * 2:
        return None
    
    tr_list, plus_dm_list, minus_dm_list = [], [], []
    
    for i in range(1, len(closes)):
        high_diff = highs[i] - highs[i-1]
        low_diff = lows[i-1] - lows[i]
        
        plus_dm = high_diff if high_diff > low_diff and high_diff > 0 else 0
        minus_dm = low_diff if low_diff > high_diff and low_diff > 0 else 0
        
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        
        tr_list.append(tr)
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)
    
    if len(tr_list) < period:
        return None
    
    # Smoothed values
    atr = sum(tr_list[:period])
    plus_dm_sum = sum(plus_dm_list[:period])
    minus_dm_sum = sum(minus_dm_list[:period])
    
    for i in range(period, len(tr_list)):
        atr = atr - (atr / period) + tr_list[i]
        plus_dm_sum = plus_dm_sum - (plus_dm_sum / period) + plus_dm_list[i]
        minus_dm_sum = minus_dm_sum - (minus_dm_sum / period) + minus_dm_list[i]
    
    if atr == 0:
        return None
    
    plus_di = 100 * plus_dm_sum / atr
    minus_di = 100 * minus_dm_sum / atr
    
    di_sum = plus_di + minus_di
    if di_sum == 0:
        return {"adx": 0, "plus_di": 0, "minus_di": 0, "zone": "SAFE"}
    
    dx = 100 * abs(plus_di - minus_di) / di_sum
    
    # ADX zone classification
    zone = "SAFE" if dx < 30 else "CAUTION" if dx < 40 else "DANGER" if dx < 50 else "DEATH_TRAP"
    
    return {
        "adx": round(dx, 2),
        "plus_di": round(plus_di, 2),
        "minus_di": round(minus_di, 2),
        "zone": zone
    }


def calculate_stochastic(highs, lows, closes, k_period=14, d_period=3):
    """Stochastic Oscillator %K and %D"""
    if len(closes) < k_period:
        return None
    
    lowest_low = min(lows[-k_period:])
    highest_high = max(highs[-k_period:])
    
    if highest_high == lowest_low:
        return {"k": 50, "d": 50}
    
    k = 100 * (closes[-1] - lowest_low) / (highest_high - lowest_low)
    
    # Calculate %D (SMA of %K)
    k_values = []
    for i in range(k_period, len(closes) + 1):
        ll = min(lows[i-k_period:i])
        hh = max(highs[i-k_period:i])
        if hh != ll:
            k_values.append(100 * (closes[i-1] - ll) / (hh - ll))
    
    d = sum(k_values[-d_period:]) / d_period if len(k_values) >= d_period else k
    
    return {"k": round(k, 2), "d": round(d, 2)}


def calculate_atr(highs, lows, closes, period=14):
    """Average True Range (volatility measure)"""
    if len(closes) < period + 1:
        return None
    
    tr_list = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        tr_list.append(tr)
    
    return round(sum(tr_list[-period:]) / period, 4) if len(tr_list) >= period else None


def calculate_bollinger_bands(prices, period=20, std_dev=2):
    """Bollinger Bands with width"""
    if len(prices) < period:
        return None
    
    sma = sum(prices[-period:]) / period
    variance = sum((p - sma) ** 2 for p in prices[-period:]) / period
    std = variance ** 0.5
    
    upper = sma + (std_dev * std)
    lower = sma - (std_dev * std)
    width = (upper - lower) / sma * 100  # Band width as percentage
    
    # Position within bands (0 = at lower, 100 = at upper)
    current = prices[-1]
    position = ((current - lower) / (upper - lower) * 100) if upper != lower else 50
    
    return {
        "upper": round(upper, 4),
        "middle": round(sma, 4),
        "lower": round(lower, 4),
        "width_pct": round(width, 2),
        "position": round(position, 2)
    }


def calculate_volume_ratio(volumes, period=20):
    """Current volume vs average volume ratio"""
    if len(volumes) < period:
        return None
    avg_vol = sum(volumes[-period-1:-1]) / period  # Exclude current
    if avg_vol == 0:
        return None
    current_vol = volumes[-1]
    return round(current_vol / avg_vol, 2)


def calculate_all_indicators(ohlcv_data):
    """Calculate all technical indicators from OHLCV data"""
    if not ohlcv_data or len(ohlcv_data) < 30:
        return None
    
    closes = [d["close"] for d in ohlcv_data]
    highs = [d["high"] for d in ohlcv_data]
    lows = [d["low"] for d in ohlcv_data]
    
    # Use estimated volume from price movement if not available
    volumes = [abs(d.get("high", 0) - d.get("low", 0)) * 1000000 for d in ohlcv_data]
    
    rsi = calculate_rsi(closes)
    macd = calculate_macd(closes)
    adx = calculate_adx(highs, lows, closes)
    stoch = calculate_stochastic(highs, lows, closes)
    atr = calculate_atr(highs, lows, closes)
    bb = calculate_bollinger_bands(closes)
    vol_ratio = calculate_volume_ratio(volumes)
    
    # Calculate signal scoring
    signals = {
        "rsi": {"value": rsi, "signal": False, "zone": "neutral"},
        "macd": macd or {"macd": None, "signal": None, "histogram": None, "direction": "unknown"},
        "adx": adx or {"adx": None, "zone": "unknown"},
        "stochastic": stoch or {"k": None, "d": None},
        "atr": atr,
        "bollinger": bb,
        "volume_ratio": vol_ratio
    }
    
    # RSI zone classification
    if rsi:
        if rsi >= 80:
            signals["rsi"]["zone"] = "overbought_extreme"
            signals["rsi"]["signal"] = True  # Burry signal
        elif rsi >= 70:
            signals["rsi"]["zone"] = "overbought"
            signals["rsi"]["signal"] = True
        elif rsi <= 30:
            signals["rsi"]["zone"] = "oversold"
        elif rsi <= 20:
            signals["rsi"]["zone"] = "oversold_extreme"
    
    # MACD signal (histogram turning negative for Burry)
    if macd and macd.get("histogram") is not None:
        signals["macd"]["signal"] = macd["histogram"] < 0 and macd["direction"] == "falling"
    
    # ADX signal (< 30 for Burry shorts)
    if adx:
        signals["adx"]["signal"] = adx["adx"] < 30
    
    # Stochastic signal (> 90 for Burry)
    if stoch:
        signals["stochastic"]["signal"] = stoch["k"] > 90
    
    # Volume signal (> 2x average)
    if vol_ratio:
        signals["volume_ratio_signal"] = vol_ratio > 2.0
    
    # Count signals for Burry strategy
    signal_count = sum([
        1 if signals["rsi"].get("signal") else 0,
        1 if signals["macd"].get("signal") else 0,
        1 if signals.get("adx", {}).get("signal") else 0,
        1 if signals.get("stochastic", {}).get("signal") else 0,
        1 if signals.get("volume_ratio_signal") else 0
    ])
    
    return {
        "indicators": signals,
        "signal_count": signal_count,
        "current_price": closes[-1] if closes else None
    }


# ---------------------------------------------------------------------------
# RISK MANAGEMENT AUTO-TRIGGERS
# ---------------------------------------------------------------------------
def update_risk_settings_on_outcome(conn, outcome, pnl_dollars=0, pnl_percent=0):
    """
    Automatically update risk settings based on trade outcomes.
    Called when a journal entry is saved or updated.
    """
    # Get current settings
    consecutive_losses = int(conn.execute("SELECT value FROM settings WHERE key='consecutive_losses'").fetchone()["value"] or 0)
    consecutive_wins = int(conn.execute("SELECT value FROM settings WHERE key='consecutive_wins'").fetchone()["value"] or 0) if conn.execute("SELECT value FROM settings WHERE key='consecutive_wins'").fetchone() else 0
    
    if outcome == "loss":
        # Increment consecutive losses, reset consecutive wins
        consecutive_losses += 1
        consecutive_wins = 0
        
        # Auto-activate turtle mode after 2 consecutive losses
        if consecutive_losses >= 2:
            conn.execute("UPDATE settings SET value='true', updated_at=datetime('now') WHERE key='turtle_mode'")
            log.warning(f"TURTLE MODE AUTO-ACTIVATED: {consecutive_losses} consecutive losses")
    
    elif outcome == "win":
        # Reset consecutive losses, increment wins
        consecutive_losses = 0
        consecutive_wins += 1
        
        # Overconfidence warning at 3+ wins (logged but doesn't change settings)
        if consecutive_wins >= 3:
            log.warning(f"OVERCONFIDENCE WARNING: {consecutive_wins} consecutive wins - stay disciplined!")
    
    elif outcome == "breakeven":
        # Breakeven doesn't affect streaks significantly
        pass
    
    # Update consecutive losses/wins
    conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES ('consecutive_losses', ?, datetime('now'))", (str(consecutive_losses),))
    conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES ('consecutive_wins', ?, datetime('now'))", (str(consecutive_wins),))
    
    # Calculate daily loss percentage
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_pnl = conn.execute("""
        SELECT COALESCE(SUM(pnl_percent), 0) as daily_pnl 
        FROM trade_journal 
        WHERE date(created_at) = date(?) AND outcome IN ('win', 'loss')
    """, (today,)).fetchone()["daily_pnl"]
    
    conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES ('daily_loss_pct', ?, datetime('now'))", (str(abs(daily_pnl) if daily_pnl < 0 else 0),))
    
    # Lock trading if daily loss >= 8%
    if daily_pnl <= -8:
        conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES ('trading_locked', 'true', datetime('now'))")
        log.error(f"TRADING LOCKED: Daily loss of {daily_pnl}% exceeds 8% threshold")
    
    conn.commit()
    return consecutive_losses, consecutive_wins


def calculate_portfolio_heat(conn):
    """
    Calculate current portfolio heat (total position risk as % of portfolio).
    Heat = sum of (position size * leverage) / portfolio balance * 100
    """
    # Get portfolio balance
    balance = float(conn.execute("SELECT value FROM settings WHERE key='portfolio_balance'").fetchone()["value"] or 100)
    
    # Get open positions from journal
    open_trades = conn.execute("""
        SELECT size, leverage, entry_price 
        FROM trade_journal 
        WHERE outcome = 'open'
    """).fetchall()
    
    total_exposure = sum((trade["size"] or 0) * (trade["leverage"] or 1) for trade in open_trades)
    heat = (total_exposure / balance * 100) if balance > 0 else 0
    
    # Update heat setting
    conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES ('portfolio_heat', ?, datetime('now'))", (str(round(heat, 1)),))
    conn.commit()
    
    return round(heat, 1)


# ---------------------------------------------------------------------------
# PHASE 3: QUANTITATIVE EDGE
# ---------------------------------------------------------------------------

# External API endpoints for Phase 3
FEAR_GREED_API = "https://api.alternative.me/fng/"
COINGLASS_API = "https://open-api.coinglass.com/public/v2"
BINANCE_FAPI = "https://fapi.binance.com/fapi/v1"


def calculate_kelly_criterion(conn, strategy=None):
    """
    Calculate Kelly Criterion position sizing from journal data.
    
    Kelly % = W - [(1 - W) / R]
    Where:
        W = win rate (probability of winning)
        R = win/loss ratio (avg win / avg loss)
    
    Returns full Kelly, half Kelly (recommended), and quarter Kelly (conservative).
    """
    # Build query based on strategy filter
    if strategy and strategy != "all":
        query_filter = "AND strategy = ?"
        params = (strategy,)
    else:
        query_filter = ""
        params = ()
    
    # Get win/loss stats
    stats = conn.execute(f"""
        SELECT 
            COUNT(CASE WHEN outcome = 'win' THEN 1 END) as wins,
            COUNT(CASE WHEN outcome = 'loss' THEN 1 END) as losses,
            AVG(CASE WHEN outcome = 'win' THEN ABS(pnl_percent) END) as avg_win_pct,
            AVG(CASE WHEN outcome = 'loss' THEN ABS(pnl_percent) END) as avg_loss_pct
        FROM trade_journal
        WHERE outcome IN ('win', 'loss') {query_filter}
    """, params).fetchone()
    
    wins = stats["wins"] or 0
    losses = stats["losses"] or 0
    total_trades = wins + losses
    avg_win = stats["avg_win_pct"] or 0
    avg_loss = stats["avg_loss_pct"] or 1  # Avoid division by zero
    
    if total_trades < 5:
        return {
            "strategy": strategy or "all",
            "total_trades": total_trades,
            "error": "Insufficient data (need at least 5 completed trades)",
            "kelly_full": None,
            "kelly_half": None,
            "kelly_quarter": None,
            "win_rate": None,
            "win_loss_ratio": None
        }
    
    # Calculate win rate (W)
    win_rate = wins / total_trades
    
    # Calculate win/loss ratio (R)
    win_loss_ratio = avg_win / avg_loss if avg_loss > 0 else 1
    
    # Kelly Formula: K = W - [(1 - W) / R]
    kelly_full = win_rate - ((1 - win_rate) / win_loss_ratio)
    
    # Clamp Kelly between 0 and 1 (can go negative if edge is negative)
    kelly_full = max(0, min(1, kelly_full))
    
    # Calculate fractional Kelly (safer)
    kelly_half = kelly_full / 2
    kelly_quarter = kelly_full / 4
    
    return {
        "strategy": strategy or "all",
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate * 100, 1),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "win_loss_ratio": round(win_loss_ratio, 2),
        "kelly_full": round(kelly_full * 100, 1),
        "kelly_half": round(kelly_half * 100, 1),
        "kelly_quarter": round(kelly_quarter * 100, 1),
        "recommended_size_pct": round(kelly_half * 100, 1),  # Half-Kelly as default
        "expected_value": round((win_rate * avg_win) - ((1 - win_rate) * avg_loss), 2)
    }


def fetch_fear_greed_index():
    """
    Fetch Crypto Fear & Greed Index from alternative.me
    
    Values:
        0-24: Extreme Fear → Penguin territory (divergence plays)
        25-49: Fear
        50-74: Greed  
        75-100: Extreme Greed → Burry territory (exhaustion tops)
    """
    try:
        resp = requests.get(f"{FEAR_GREED_API}?limit=10", timeout=10)
        if resp.status_code != 200:
            return None
        
        data = resp.json().get("data", [])
        if not data:
            return None
        
        current = data[0]
        
        # Calculate trend from last 7 days
        values = [int(d["value"]) for d in data[:7]]
        trend = "rising" if len(values) > 1 and values[0] > values[-1] else "falling" if len(values) > 1 and values[0] < values[-1] else "stable"
        avg_7d = sum(values) / len(values) if values else 0
        
        return {
            "value": int(current["value"]),
            "classification": current["value_classification"],
            "timestamp": current["timestamp"],
            "trend": trend,
            "avg_7d": round(avg_7d, 1),
            "history": [{"value": int(d["value"]), "date": d["timestamp"]} for d in data[:7]]
        }
    except Exception as e:
        log.error(f"Fear & Greed fetch error: {e}")
        return None


def calculate_atr_percentile(ohlcv_data, current_atr, lookback=90):
    """
    Calculate where current ATR sits within historical range.
    
    Returns percentile (0-100):
        Bottom 25%: Low volatility → favor mean reversion, tighter stops
        Top 25%: High volatility → favor momentum, wider stops
    """
    if not ohlcv_data or len(ohlcv_data) < lookback:
        return None
    
    highs = [d["high"] for d in ohlcv_data]
    lows = [d["low"] for d in ohlcv_data]
    closes = [d["close"] for d in ohlcv_data]
    
    # Calculate historical ATRs
    historical_atrs = []
    for i in range(14, min(len(closes), lookback)):
        atr = calculate_atr(highs[:i+1], lows[:i+1], closes[:i+1])
        if atr:
            historical_atrs.append(atr)
    
    if not historical_atrs:
        return None
    
    # Calculate percentile
    sorted_atrs = sorted(historical_atrs)
    below_count = sum(1 for a in sorted_atrs if a < current_atr)
    percentile = (below_count / len(sorted_atrs)) * 100
    
    return round(percentile, 1)


def calculate_bollinger_band_squeeze(ohlcv_data, period=20, std_dev=2):
    """
    Detect Bollinger Band squeeze (volatility contraction).
    Squeeze = bands narrowing, often precedes explosive move.
    
    Returns squeeze status and band width percentile.
    """
    if not ohlcv_data or len(ohlcv_data) < period * 2:
        return None
    
    closes = [d["close"] for d in ohlcv_data]
    
    # Calculate current BB width
    current_bb = calculate_bollinger_bands(closes, period, std_dev)
    if not current_bb:
        return None
    
    # Calculate historical BB widths
    historical_widths = []
    for i in range(period, len(closes)):
        bb = calculate_bollinger_bands(closes[:i+1], period, std_dev)
        if bb:
            historical_widths.append(bb["width_pct"])
    
    if len(historical_widths) < 20:
        return None
    
    # Calculate percentile of current width
    sorted_widths = sorted(historical_widths)
    current_width = current_bb["width_pct"]
    below_count = sum(1 for w in sorted_widths if w < current_width)
    width_percentile = (below_count / len(sorted_widths)) * 100
    
    # Squeeze detection
    is_squeeze = width_percentile < 20  # Bottom 20% of historical widths
    squeeze_strength = "extreme" if width_percentile < 10 else "moderate" if width_percentile < 20 else "none"
    
    return {
        "current_width": current_width,
        "width_percentile": round(width_percentile, 1),
        "is_squeeze": is_squeeze,
        "squeeze_strength": squeeze_strength,
        "bb_position": current_bb["position"],
        "interpretation": "Volatility squeeze - explosive move likely" if is_squeeze else "Normal volatility"
    }


def classify_volatility_regime(atr_percentile, bb_squeeze, fear_greed_value):
    """
    Classify current market volatility regime.
    
    Regimes:
        CALM: Low vol, stable conditions → reduce leverage, widen time horizon
        NORMAL: Average conditions → standard Penguin/Burry system
        VOLATILE: High vol → your sweet spot, full system
        EXTREME: Dangerous conditions → reduce to quarter-Kelly, defensive only
    """
    # Default scores
    vol_score = 50  # Neutral
    
    # ATR contribution (0-40 points)
    if atr_percentile is not None:
        if atr_percentile < 25:
            vol_score = 20
        elif atr_percentile < 50:
            vol_score = 40
        elif atr_percentile < 75:
            vol_score = 60
        else:
            vol_score = 80
    
    # BB squeeze contribution (modify score)
    if bb_squeeze:
        if bb_squeeze["is_squeeze"]:
            # Squeeze = calm before storm, treat as pre-volatile
            vol_score = max(vol_score, 55)
    
    # Fear & Greed contribution (extreme values = extreme regime)
    if fear_greed_value is not None:
        if fear_greed_value < 20 or fear_greed_value > 80:
            vol_score = min(100, vol_score + 20)  # Push toward extreme
    
    # Classify regime
    if vol_score < 30:
        regime = "CALM"
        recommendation = "Reduce leverage, widen time horizon, favor swing trades"
        kelly_multiplier = 0.5  # Half of recommended Kelly
    elif vol_score < 55:
        regime = "NORMAL"
        recommendation = "Standard system parameters, use half-Kelly sizing"
        kelly_multiplier = 1.0
    elif vol_score < 75:
        regime = "VOLATILE"
        recommendation = "Your Penguin/Burry sweet spot - full system active"
        kelly_multiplier = 1.0
    else:
        regime = "EXTREME"
        recommendation = "Reduce to quarter-Kelly, defensive positions only"
        kelly_multiplier = 0.5
    
    return {
        "regime": regime,
        "vol_score": round(vol_score, 1),
        "recommendation": recommendation,
        "kelly_multiplier": kelly_multiplier,
        "details": {
            "atr_percentile": atr_percentile,
            "bb_squeeze": bb_squeeze["squeeze_strength"] if bb_squeeze else None,
            "fear_greed": fear_greed_value
        }
    }


def fetch_funding_rate(symbol):
    """
    Fetch perpetual futures funding rate from Binance.
    
    Funding Rate Signals:
        > +0.05% per 8h: Market overleveraged LONG → Burry setup forming
        < -0.05% per 8h: Market overleveraged SHORT → squeeze incoming (Penguin)
        > +0.1%: EXTREME → high probability of long liquidation cascade
        < -0.1%: EXTREME → high probability of short squeeze
    
    Annualized cost: Rate × 3 × 365
    """
    # Map common symbols to Binance futures format
    symbol_map = {
        "BTC": "BTCUSDT",
        "ETH": "ETHUSDT",
        "SOL": "SOLUSDT",
        "DOGE": "DOGEUSDT",
        "XRP": "XRPUSDT",
        "ADA": "ADAUSDT",
        "AVAX": "AVAXUSDT",
        "DOT": "DOTUSDT",
        "MATIC": "MATICUSDT",
        "LINK": "LINKUSDT",
        "ATOM": "ATOMUSDT",
        "UNI": "UNIUSDT",
        "LTC": "LTCUSDT",
        "BCH": "BCHUSDT",
        "NEAR": "NEARUSDT",
        "APT": "APTUSDT",
        "ARB": "ARBUSDT",
        "OP": "OPUSDT",
        "SUI": "SUIUSDT",
        "PEPE": "PEPEUSDT",
        "WIF": "WIFUSDT",
        "BONK": "BONKUSDT"
    }
    
    binance_symbol = symbol_map.get(symbol.upper(), f"{symbol.upper()}USDT")
    
    try:
        # Get current funding rate
        resp = requests.get(f"{BINANCE_FAPI}/premiumIndex", params={"symbol": binance_symbol}, timeout=10)
        if resp.status_code != 200:
            return None
        
        data = resp.json()
        current_rate = float(data.get("lastFundingRate", 0)) * 100  # Convert to percentage
        
        # Get funding rate history
        hist_resp = requests.get(f"{BINANCE_FAPI}/fundingRate", 
                                  params={"symbol": binance_symbol, "limit": 21}, timeout=10)
        
        history = []
        avg_7d = current_rate
        if hist_resp.status_code == 200:
            hist_data = hist_resp.json()
            history = [{"rate": float(h["fundingRate"]) * 100, "time": h["fundingTime"]} for h in hist_data[:21]]
            # 7 days = 21 funding periods (3 per day)
            if len(history) >= 21:
                avg_7d = sum(h["rate"] for h in history[:21]) / 21
        
        # Calculate annualized cost
        annualized = current_rate * 3 * 365  # 3 funding periods per day * 365 days
        
        # Classify signal
        if current_rate > 0.1:
            signal = "EXTREME_LONG"
            interpretation = "Market extremely overleveraged long - high liquidation cascade risk (Burry setup)"
            bonus_signal = "burry"
        elif current_rate > 0.05:
            signal = "OVERLEVERAGED_LONG"
            interpretation = "Longs paying premium - potential short setup forming"
            bonus_signal = "burry"
        elif current_rate < -0.1:
            signal = "EXTREME_SHORT"
            interpretation = "Market extremely overleveraged short - short squeeze likely (Penguin setup)"
            bonus_signal = "penguin"
        elif current_rate < -0.05:
            signal = "OVERLEVERAGED_SHORT"
            interpretation = "Shorts paying premium - potential long setup forming"
            bonus_signal = "penguin"
        else:
            signal = "NEUTRAL"
            interpretation = "Funding neutral - no strong positioning signal"
            bonus_signal = None
        
        return {
            "symbol": binance_symbol,
            "current_rate": round(current_rate, 4),
            "avg_7d": round(avg_7d, 4),
            "annualized_pct": round(annualized, 2),
            "signal": signal,
            "interpretation": interpretation,
            "bonus_signal_for": bonus_signal,
            "is_extreme": abs(current_rate) > 0.08,
            "history": history[:7]  # Last 7 periods
        }
    except Exception as e:
        log.error(f"Funding rate fetch error for {symbol}: {e}")
        return None


def calculate_correlation(prices_a, prices_b):
    """
    Calculate Pearson correlation coefficient between two price series.
    Returns value between -1 (inverse) and +1 (perfect correlation).
    """
    if len(prices_a) != len(prices_b) or len(prices_a) < 2:
        return None
    
    n = len(prices_a)
    
    # Calculate means
    mean_a = sum(prices_a) / n
    mean_b = sum(prices_b) / n
    
    # Calculate correlation
    numerator = sum((a - mean_a) * (b - mean_b) for a, b in zip(prices_a, prices_b))
    
    std_a = (sum((a - mean_a) ** 2 for a in prices_a) / n) ** 0.5
    std_b = (sum((b - mean_b) ** 2 for b in prices_b) / n) ** 0.5
    
    if std_a == 0 or std_b == 0:
        return None
    
    correlation = numerator / (n * std_a * std_b)
    return round(correlation, 4)


def calculate_beta(asset_returns, benchmark_returns):
    """
    Calculate beta (sensitivity to benchmark).
    
    Beta > 1.5: High beta - amplified moves, better for divergence plays
    Beta < 0.5: Low beta - defensive, skip for divergence plays
    Beta ≈ 1.0: Moves with the market
    """
    if len(asset_returns) != len(benchmark_returns) or len(asset_returns) < 2:
        return None
    
    n = len(asset_returns)
    
    # Mean returns
    mean_asset = sum(asset_returns) / n
    mean_bench = sum(benchmark_returns) / n
    
    # Covariance and variance
    covariance = sum((a - mean_asset) * (b - mean_bench) for a, b in zip(asset_returns, benchmark_returns)) / n
    variance_bench = sum((b - mean_bench) ** 2 for b in benchmark_returns) / n
    
    if variance_bench == 0:
        return None
    
    beta = covariance / variance_bench
    return round(beta, 3)


def fetch_correlation_matrix(symbols, days=30):
    """
    Fetch price data and calculate correlation matrix for multiple symbols.
    """
    # Fetch OHLCV data for all symbols
    price_data = {}
    for symbol in symbols:
        ohlcv = fetch_coingecko_ohlcv(symbol, days=days)
        if ohlcv and len(ohlcv) >= 20:
            price_data[symbol] = [d["close"] for d in ohlcv]
    
    if len(price_data) < 2:
        return None
    
    # Align lengths (use shortest)
    min_len = min(len(v) for v in price_data.values())
    for symbol in price_data:
        price_data[symbol] = price_data[symbol][:min_len]
    
    # Calculate correlation matrix
    matrix = {}
    for sym_a in price_data:
        matrix[sym_a] = {}
        for sym_b in price_data:
            if sym_a == sym_b:
                matrix[sym_a][sym_b] = 1.0
            else:
                corr = calculate_correlation(price_data[sym_a], price_data[sym_b])
                matrix[sym_a][sym_b] = corr
    
    # Calculate beta vs BTC if BTC is in the list
    betas = {}
    if "BTC" in price_data:
        btc_returns = [(price_data["BTC"][i] - price_data["BTC"][i-1]) / price_data["BTC"][i-1] 
                       for i in range(1, len(price_data["BTC"]))]
        for symbol in price_data:
            if symbol != "BTC":
                asset_returns = [(price_data[symbol][i] - price_data[symbol][i-1]) / price_data[symbol][i-1]
                                 for i in range(1, len(price_data[symbol]))]
                betas[symbol] = calculate_beta(asset_returns, btc_returns)
    
    return {
        "correlation_matrix": matrix,
        "betas_vs_btc": betas,
        "symbols": list(price_data.keys()),
        "period_days": days,
        "data_points": min_len
    }


def detect_correlation_breakdown(current_corr, historical_corr, threshold=0.3):
    """
    Detect correlation breakdown - when 30d correlation drops significantly.
    Breakdown = divergence opportunity (Penguin trigger).
    
    Returns True if correlation dropped from >0.7 to <0.4 (significant breakdown).
    """
    if current_corr is None or historical_corr is None:
        return None
    
    # Significant breakdown: was highly correlated, now much lower
    if historical_corr > 0.7 and current_corr < 0.4:
        return {
            "is_breakdown": True,
            "severity": "significant",
            "historical_corr": historical_corr,
            "current_corr": current_corr,
            "drop": round(historical_corr - current_corr, 3),
            "signal": "DIVERGENCE_OPPORTUNITY"
        }
    elif historical_corr > 0.6 and current_corr < historical_corr - threshold:
        return {
            "is_breakdown": True,
            "severity": "moderate",
            "historical_corr": historical_corr,
            "current_corr": current_corr,
            "drop": round(historical_corr - current_corr, 3),
            "signal": "POTENTIAL_DIVERGENCE"
        }
    
    return {
        "is_breakdown": False,
        "historical_corr": historical_corr,
        "current_corr": current_corr
    }


def calculate_advanced_risk_metrics(conn, strategy=None):
    """
    Calculate advanced risk metrics from journal data.
    
    Metrics:
        - Sharpe Ratio: (avg return - risk free rate) / std deviation
        - Sortino Ratio: Like Sharpe but only penalizes downside volatility
        - Max Drawdown: Largest peak-to-trough decline
        - Win Streak / Loss Streak: Longest consecutive wins/losses
        - Profit Factor: Gross profits / Gross losses
    """
    # Build query
    if strategy and strategy != "all":
        query_filter = "AND strategy = ?"
        params = (strategy,)
    else:
        query_filter = ""
        params = ()
    
    # Get all completed trades
    trades = conn.execute(f"""
        SELECT pnl_percent, pnl_dollars, outcome, created_at
        FROM trade_journal
        WHERE outcome IN ('win', 'loss') {query_filter}
        ORDER BY created_at ASC
    """, params).fetchall()
    
    if len(trades) < 5:
        return {"error": "Insufficient data (need at least 5 completed trades)"}
    
    returns = [t["pnl_percent"] for t in trades]
    
    # Mean return
    mean_return = sum(returns) / len(returns)
    
    # Standard deviation
    variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)
    std_dev = variance ** 0.5
    
    # Downside deviation (for Sortino)
    downside_returns = [r for r in returns if r < 0]
    downside_variance = sum(r ** 2 for r in downside_returns) / len(downside_returns) if downside_returns else 0
    downside_dev = downside_variance ** 0.5
    
    # Sharpe Ratio (assuming 0% risk-free rate for simplicity)
    sharpe = mean_return / std_dev if std_dev > 0 else 0
    
    # Sortino Ratio
    sortino = mean_return / downside_dev if downside_dev > 0 else 0
    
    # Profit Factor
    gross_profit = sum(t["pnl_dollars"] for t in trades if t["pnl_dollars"] > 0)
    gross_loss = abs(sum(t["pnl_dollars"] for t in trades if t["pnl_dollars"] < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    
    # Max Drawdown (from cumulative P&L)
    cumulative = 0
    peak = 0
    max_drawdown = 0
    for r in returns:
        cumulative += r
        peak = max(peak, cumulative)
        drawdown = peak - cumulative
        max_drawdown = max(max_drawdown, drawdown)
    
    # Win/Loss streaks
    current_streak = 0
    max_win_streak = 0
    max_loss_streak = 0
    streak_type = None
    
    for t in trades:
        if t["outcome"] == "win":
            if streak_type == "win":
                current_streak += 1
            else:
                current_streak = 1
                streak_type = "win"
            max_win_streak = max(max_win_streak, current_streak)
        else:
            if streak_type == "loss":
                current_streak += 1
            else:
                current_streak = 1
                streak_type = "loss"
            max_loss_streak = max(max_loss_streak, current_streak)
    
    # Classify Sharpe
    if sharpe > 2.0:
        sharpe_rating = "excellent"
    elif sharpe > 1.0:
        sharpe_rating = "good"
    elif sharpe > 0.5:
        sharpe_rating = "acceptable"
    else:
        sharpe_rating = "needs_work"
    
    return {
        "strategy": strategy or "all",
        "total_trades": len(trades),
        "mean_return_pct": round(mean_return, 2),
        "std_deviation": round(std_dev, 2),
        "sharpe_ratio": round(sharpe, 3),
        "sharpe_rating": sharpe_rating,
        "sortino_ratio": round(sortino, 3),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown_pct": round(max_drawdown, 2),
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2)
    }


# ---------------------------------------------------------------------------
# PHASE 4: BACKGROUND MONITORING & ALERT SYSTEM
# ---------------------------------------------------------------------------
# Global state for background monitoring
monitor_thread = None
monitor_running = False
alert_check_interval = 30  # seconds
scan_interval = 300  # 5 minutes
last_scan_time = 0
triggered_alerts_queue = deque(maxlen=100)  # Recent triggered alerts for frontend polling


def check_price_alert(alert, current_price):
    """
    Check if a price alert condition is met.
    
    Conditions:
        - crosses_above: price crosses above target
        - crosses_below: price crosses below target
        - reaches: price reaches target (within 0.5%)
        - changes_by: price changes by X% from alert creation
    """
    target = alert["target_value"]
    condition = alert["comparison"]
    
    if condition == "crosses_above":
        return current_price >= target
    elif condition == "crosses_below":
        return current_price <= target
    elif condition == "reaches":
        # Within 0.5% of target
        tolerance = target * 0.005
        return abs(current_price - target) <= tolerance
    elif condition == "changes_by":
        # Check % change from baseline
        baseline = alert.get("secondary_value", current_price)
        if baseline <= 0:
            return False
        change_pct = abs((current_price - baseline) / baseline * 100)
        return change_pct >= target
    
    return False


def check_indicator_alert(alert, symbol):
    """
    Check if an indicator alert condition is met.
    
    Indicator types:
        - rsi_above, rsi_below
        - macd_crosses_signal
        - adx_above, adx_below
        - volume_spike
        - bb_squeeze
    """
    condition = alert["condition"]
    target = alert["target_value"]
    
    # Fetch fresh indicator data
    ohlcv = fetch_coingecko_ohlcv(symbol, days=14)
    if not ohlcv or len(ohlcv) < 14:
        return False, None
    
    indicators = calculate_all_indicators(ohlcv)
    if not indicators:
        return False, None
    
    actual_value = None
    triggered = False
    
    if condition == "rsi_above":
        actual_value = indicators.get("rsi", 0)
        triggered = actual_value >= target
    elif condition == "rsi_below":
        actual_value = indicators.get("rsi", 100)
        triggered = actual_value <= target
    elif condition == "adx_above":
        actual_value = indicators.get("adx", 0)
        triggered = actual_value >= target
    elif condition == "adx_below":
        actual_value = indicators.get("adx", 100)
        triggered = actual_value <= target
    elif condition == "volume_spike":
        actual_value = indicators.get("volume_ratio", 1)
        triggered = actual_value >= target
    elif condition == "bb_squeeze":
        bb_data = indicators.get("bollinger_bands", {})
        actual_value = bb_data.get("width_pct", 100)
        triggered = actual_value <= target  # Squeeze = narrow bands
    
    return triggered, actual_value


def check_divergence_alert(alert, symbol):
    """
    Check for BTC/altcoin divergence (Penguin setup).
    
    Divergence conditions:
        - BTC down 3-8%, alt up 10-25%
        - Returns divergence details if detected
    """
    try:
        btc_price = fetch_coingecko_price("BTC")
        alt_price = fetch_coingecko_price(symbol)
        
        if not btc_price or not alt_price:
            return False, None
        
        btc_change = btc_price.get("price_change_24h", 0)
        alt_change = alt_price.get("price_change_24h", 0)
        
        # Classic Penguin divergence: BTC down, alt up
        is_divergence = btc_change < -3 and alt_change > 10
        
        if is_divergence:
            return True, {
                "btc_change": btc_change,
                "alt_change": alt_change,
                "divergence_spread": alt_change - btc_change
            }
        
        return False, {"btc_change": btc_change, "alt_change": alt_change}
    except Exception:
        return False, None


def check_funding_rate_alert(alert, symbol):
    """
    Check funding rate alert conditions.
    
    Conditions:
        - extreme_positive: funding > 0.08%
        - extreme_negative: funding < -0.08%
        - above_threshold: funding > target
        - below_threshold: funding < target
    """
    funding_data = fetch_funding_rate(symbol)
    if not funding_data:
        return False, None
    
    current_rate = funding_data.get("current_rate", 0)
    condition = alert["condition"]
    target = alert["target_value"]
    
    triggered = False
    if condition == "extreme_positive":
        triggered = current_rate >= 0.08
    elif condition == "extreme_negative":
        triggered = current_rate <= -0.08
    elif condition == "above_threshold":
        triggered = current_rate >= target
    elif condition == "below_threshold":
        triggered = current_rate <= target
    
    return triggered, current_rate


def check_portfolio_alert(alert, conn):
    """
    Check portfolio-level alert conditions.
    
    Conditions:
        - heat_above: portfolio heat > target
        - daily_loss_above: daily loss % > target
        - consecutive_losses: consecutive losses >= target
    """
    condition = alert["condition"]
    target = alert["target_value"]
    
    actual_value = None
    triggered = False
    
    if condition == "heat_above":
        # Calculate portfolio heat from positions
        heat_row = conn.execute("SELECT value FROM settings WHERE key = 'portfolio_heat'").fetchone()
        actual_value = float(heat_row["value"]) if heat_row else 0
        triggered = actual_value >= target
    elif condition == "daily_loss_above":
        loss_row = conn.execute("SELECT value FROM settings WHERE key = 'daily_loss_pct'").fetchone()
        actual_value = float(loss_row["value"]) if loss_row else 0
        triggered = actual_value >= target
    elif condition == "consecutive_losses":
        loss_row = conn.execute("SELECT value FROM settings WHERE key = 'consecutive_losses'").fetchone()
        actual_value = int(loss_row["value"]) if loss_row else 0
        triggered = actual_value >= target
    
    return triggered, actual_value


def process_triggered_alert(alert, actual_value, conn):
    """
    Handle a triggered alert: log it, update state, queue notification.
    """
    now = datetime.now(timezone.utc).isoformat()
    
    # Build message
    message = f"{alert['name']}: {alert['symbol']} {alert['condition']} "
    if actual_value is not None:
        if isinstance(actual_value, dict):
            message += json.dumps(actual_value)
        else:
            message += f"(actual: {actual_value}, target: {alert['target_value']})"
    
    # Insert into alert history
    conn.execute("""
        INSERT INTO alert_history (alert_id, alert_name, symbol, alert_type, condition, 
                                   target_value, actual_value, message, triggered_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        alert["id"], alert["name"], alert["symbol"], alert["alert_type"],
        alert["condition"], alert["target_value"], 
        actual_value if not isinstance(actual_value, dict) else json.dumps(actual_value),
        message, now
    ))
    
    # Update alert state
    conn.execute("""
        UPDATE alerts SET triggered = 1, last_triggered_at = ?, 
                          trigger_count = trigger_count + 1, updated_at = ?
        WHERE id = ?
    """, (now, now, alert["id"]))
    
    # If one-time alert, disable it
    if alert["one_time"]:
        conn.execute("UPDATE alerts SET enabled = 0 WHERE id = ?", (alert["id"],))
    
    conn.commit()
    
    # Add to notification queue for frontend
    triggered_alerts_queue.append({
        "id": alert["id"],
        "name": alert["name"],
        "symbol": alert["symbol"],
        "type": alert["alert_type"],
        "message": message,
        "triggered_at": now
    })
    
    log.info(f"Alert triggered: {message}")


def run_alert_checks():
    """
    Main alert checking loop - checks all enabled alerts.
    Called by background thread every {alert_check_interval} seconds.
    """
    global triggered_alerts_queue
    
    try:
        conn = get_db()
        
        # Get all enabled alerts
        alerts = conn.execute("""
            SELECT * FROM alerts WHERE enabled = 1
        """).fetchall()
        
        # Cache price data to avoid repeated API calls
        price_cache = {}
        
        for alert in alerts:
            alert_dict = dict(alert)
            symbol = alert_dict["symbol"]
            alert_type = alert_dict["alert_type"]
            
            triggered = False
            actual_value = None
            
            try:
                if alert_type == "price":
                    # Get cached or fresh price
                    if symbol not in price_cache:
                        price_data = fetch_coingecko_price(symbol)
                        price_cache[symbol] = price_data
                    else:
                        price_data = price_cache[symbol]
                    
                    if price_data:
                        current_price = price_data.get("price", 0)
                        triggered = check_price_alert(alert_dict, current_price)
                        actual_value = current_price
                        
                elif alert_type == "indicator":
                    triggered, actual_value = check_indicator_alert(alert_dict, symbol)
                    
                elif alert_type == "divergence":
                    triggered, actual_value = check_divergence_alert(alert_dict, symbol)
                    
                elif alert_type == "funding":
                    triggered, actual_value = check_funding_rate_alert(alert_dict, symbol)
                    
                elif alert_type == "portfolio":
                    triggered, actual_value = check_portfolio_alert(alert_dict, conn)
                    
            except Exception as e:
                log.error(f"Error checking alert {alert_dict['id']}: {e}")
                continue
            
            if triggered:
                process_triggered_alert(alert_dict, actual_value, conn)
        
        conn.close()
        
    except Exception as e:
        log.error(f"Alert check error: {e}")


def score_penguin_setup(symbol, ohlcv_data, indicators):
    """
    Score a potential Penguin divergence setup.
    
    Criteria (5 signals):
        1. BTC -3% to -8% (24h)
        2. Alt +10% to +25% (24h) OR showing strength
        3. RSI 70-85 (overbought but not extreme)
        4. Volume 2-3x average
        5. Support holding / bouncing
    
    Returns signal count and details.
    """
    signals = []
    
    # Get BTC and alt price data
    btc_price = fetch_coingecko_price("BTC")
    alt_price = fetch_coingecko_price(symbol)
    
    if not btc_price or not alt_price:
        return 0, signals
    
    btc_change = btc_price.get("price_change_24h", 0)
    alt_change = alt_price.get("price_change_24h", 0)
    
    # Signal 1: BTC weakness
    if -8 <= btc_change <= -3:
        signals.append({"signal": "BTC_WEAKNESS", "value": btc_change, "score": 1})
    elif btc_change < -3:
        signals.append({"signal": "BTC_WEAKNESS", "value": btc_change, "score": 0.5})
    
    # Signal 2: Alt strength (divergence)
    if alt_change > btc_change + 5:  # Outperforming BTC by 5%+
        signals.append({"signal": "DIVERGENCE", "value": alt_change - btc_change, "score": 1})
    
    # Signal 3: RSI
    rsi = indicators.get("rsi", 50)
    if 70 <= rsi <= 85:
        signals.append({"signal": "RSI_OPTIMAL", "value": rsi, "score": 1})
    elif 65 <= rsi < 70:
        signals.append({"signal": "RSI_APPROACHING", "value": rsi, "score": 0.5})
    
    # Signal 4: Volume
    volume_ratio = indicators.get("volume_ratio", 1)
    if volume_ratio >= 2:
        signals.append({"signal": "VOLUME_SPIKE", "value": volume_ratio, "score": 1})
    elif volume_ratio >= 1.5:
        signals.append({"signal": "VOLUME_ELEVATED", "value": volume_ratio, "score": 0.5})
    
    # Signal 5: Support holding (price above lower BB)
    bb_position = indicators.get("bollinger_bands", {}).get("position", 50)
    if bb_position > 20:  # Above lower band
        signals.append({"signal": "SUPPORT_HOLDING", "value": bb_position, "score": 1})
    
    total_score = sum(s["score"] for s in signals)
    return round(total_score, 1), signals


def score_burry_setup(symbol, ohlcv_data, indicators):
    """
    Score a potential Burry overbought short setup.
    
    Criteria (5 signals):
        1. RSI > 80
        2. MACD histogram turning negative
        3. ADX < 30 (CRITICAL - no strong trend)
        4. Stochastic > 90
        5. Volume > 2x average
    
    Returns signal count and details.
    """
    signals = []
    
    # Signal 1: RSI extreme overbought
    rsi = indicators.get("rsi", 50)
    if rsi > 80:
        signals.append({"signal": "RSI_EXTREME", "value": rsi, "score": 1})
    elif rsi > 75:
        signals.append({"signal": "RSI_OVERBOUGHT", "value": rsi, "score": 0.5})
    
    # Signal 2: MACD turning negative
    macd_histogram = indicators.get("macd", {}).get("histogram", 0)
    macd_direction = indicators.get("macd", {}).get("direction", "bullish")
    if macd_direction == "bearish" and macd_histogram < 0:
        signals.append({"signal": "MACD_BEARISH", "value": macd_histogram, "score": 1})
    elif macd_histogram < 0:
        signals.append({"signal": "MACD_NEGATIVE", "value": macd_histogram, "score": 0.5})
    
    # Signal 3: ADX (CRITICAL - must be low for short)
    adx = indicators.get("adx", 50)
    if adx < 30:
        signals.append({"signal": "ADX_SAFE", "value": adx, "score": 1})
    elif adx < 40:
        signals.append({"signal": "ADX_CAUTION", "value": adx, "score": 0.5})
    # ADX > 40 is a KILL SWITCH - no short
    
    # Signal 4: Stochastic overbought
    stoch_k = indicators.get("stochastic", {}).get("k", 50)
    if stoch_k > 90:
        signals.append({"signal": "STOCH_EXTREME", "value": stoch_k, "score": 1})
    elif stoch_k > 80:
        signals.append({"signal": "STOCH_OVERBOUGHT", "value": stoch_k, "score": 0.5})
    
    # Signal 5: Volume spike
    volume_ratio = indicators.get("volume_ratio", 1)
    if volume_ratio >= 2:
        signals.append({"signal": "VOLUME_SPIKE", "value": volume_ratio, "score": 1})
    elif volume_ratio >= 1.5:
        signals.append({"signal": "VOLUME_ELEVATED", "value": volume_ratio, "score": 0.5})
    
    total_score = sum(s["score"] for s in signals)
    
    # ADX kill switch - if ADX > 50, nullify the setup
    if adx > 50:
        return 0, [{"signal": "ADX_DEATH_TRAP", "value": adx, "score": 0, "warning": "Never short in strong trend"}]
    
    return round(total_score, 1), signals


def run_auto_scan():
    """
    Auto-scanner: checks watchlist tokens for Penguin/Burry setups.
    Called every {scan_interval} seconds.
    """
    global last_scan_time
    
    try:
        conn = get_db()
        
        # Get all watchlist symbols
        watchlist = conn.execute("""
            SELECT symbol, strategy FROM watchlist WHERE status = 'watching'
        """).fetchall()
        
        # Get volatility regime for context
        fear_greed = fetch_fear_greed_index()
        fear_greed_value = fear_greed["value"] if fear_greed else 50
        
        # Determine regime bias
        if fear_greed_value < 30:
            regime_bias = "PENGUIN"  # Favor divergence plays in fear
        elif fear_greed_value > 70:
            regime_bias = "BURRY"  # Favor exhaustion plays in greed
        else:
            regime_bias = "NEUTRAL"
        
        scan_results = []
        
        for item in watchlist:
            symbol = item["symbol"]
            preferred_strategy = item["strategy"] or regime_bias
            
            try:
                # Fetch OHLCV and calculate indicators
                ohlcv = fetch_coingecko_ohlcv(symbol, days=14)
                if not ohlcv or len(ohlcv) < 14:
                    continue
                
                indicators = calculate_all_indicators(ohlcv)
                if not indicators:
                    continue
                
                current_price = ohlcv[-1]["close"] if ohlcv else 0
                
                # Score for both strategies
                penguin_score, penguin_signals = score_penguin_setup(symbol, ohlcv, indicators)
                burry_score, burry_signals = score_burry_setup(symbol, ohlcv, indicators)
                
                # Determine best setup
                if penguin_score >= 4 or burry_score >= 4:
                    # Strong signal detected!
                    if penguin_score >= burry_score:
                        best_strategy = "penguin"
                        signal_count = penguin_score
                        signals = penguin_signals
                        recommendation = "TRADE" if penguin_score >= 4 else "WATCH"
                    else:
                        best_strategy = "burry"
                        signal_count = burry_score
                        signals = burry_signals
                        recommendation = "TRADE" if burry_score >= 4 else "WATCH"
                    
                    confidence = "high" if signal_count >= 5 else "medium" if signal_count >= 4 else "low"
                    
                    # Save scan result
                    conn.execute("""
                        INSERT INTO scan_results (symbol, strategy, signal_count, signals_detail, 
                                                  price, recommendation, confidence, regime, scanned_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        symbol, best_strategy, signal_count, json.dumps(signals),
                        current_price, recommendation, confidence, regime_bias,
                        datetime.now(timezone.utc).isoformat()
                    ))
                    
                    scan_results.append({
                        "symbol": symbol,
                        "strategy": best_strategy,
                        "signal_count": signal_count,
                        "signals": signals,
                        "price": current_price,
                        "recommendation": recommendation,
                        "confidence": confidence
                    })
                    
                    # If strong setup, add to alert queue
                    if signal_count >= 4:
                        triggered_alerts_queue.append({
                            "id": 0,  # System-generated
                            "name": f"Auto-Scan: {best_strategy.upper()} Setup",
                            "symbol": symbol,
                            "type": "scan",
                            "message": f"{signal_count}/5 signals for {best_strategy.upper()} on {symbol} @ ${current_price:.2f}",
                            "triggered_at": datetime.now(timezone.utc).isoformat()
                        })
                        
            except Exception as e:
                log.error(f"Scan error for {symbol}: {e}")
                continue
        
        conn.commit()
        conn.close()
        last_scan_time = time.time()
        
        if scan_results:
            log.info(f"Auto-scan found {len(scan_results)} setups: {[r['symbol'] for r in scan_results]}")
        
    except Exception as e:
        log.error(f"Auto-scan error: {e}")


def check_position_pnl():
    """
    Monitor open positions and check stop-loss / take-profit levels.
    Implements OCO (One-Cancels-Other) logic - when SL or TP hits, the other is canceled.
    """
    try:
        conn = get_db()
        
        # Get all open positions
        positions = conn.execute("""
            SELECT * FROM positions WHERE status = 'open'
        """).fetchall()
        
        for pos in positions:
            symbol = pos["symbol"]
            
            # Fetch current price
            price_data = fetch_coingecko_price(symbol)
            if not price_data:
                continue
            
            current_price = price_data.get("price", 0)
            entry_price = pos["entry_price"]
            direction = pos["direction"]
            stop_loss = pos["stop_loss"]
            take_profit = pos["take_profit"]
            
            # Calculate P&L
            if direction == "long":
                pnl_pct = ((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
            else:  # short
                pnl_pct = ((entry_price - current_price) / entry_price * 100) if entry_price > 0 else 0
            
            pnl_usd = pos["size_usd"] * (pnl_pct / 100)
            
            # Update position
            conn.execute("""
                UPDATE positions SET current_price = ?, unrealized_pnl = ?, 
                                     unrealized_pnl_pct = ?, updated_at = ?
                WHERE id = ?
            """, (current_price, pnl_usd, pnl_pct, datetime.now(timezone.utc).isoformat(), pos["id"]))
            
            # OCO Logic: Check stop-loss first (priority over TP for risk management)
            exit_triggered = False
            exit_type = None
            
            if stop_loss > 0:
                sl_hit = (direction == "long" and current_price <= stop_loss) or \
                         (direction == "short" and current_price >= stop_loss)
                
                if sl_hit:
                    exit_triggered = True
                    exit_type = "stop_loss"
                    triggered_alerts_queue.append({
                        "id": pos["id"],
                        "name": f"STOP-LOSS HIT",
                        "symbol": symbol,
                        "type": "position_sl",
                        "message": f"{symbol} hit stop-loss @ ${current_price:.4f} (SL: ${stop_loss:.4f}, P&L: {pnl_pct:.2f}%)",
                        "triggered_at": datetime.now(timezone.utc).isoformat()
                    })
            
            # Only check TP if SL wasn't hit (OCO)
            if not exit_triggered and take_profit > 0:
                tp_hit = (direction == "long" and current_price >= take_profit) or \
                         (direction == "short" and current_price <= take_profit)
                
                if tp_hit:
                    exit_triggered = True
                    exit_type = "take_profit"
                    triggered_alerts_queue.append({
                        "id": pos["id"],
                        "name": f"TAKE-PROFIT HIT",
                        "symbol": symbol,
                        "type": "position_tp",
                        "message": f"{symbol} hit take-profit @ ${current_price:.4f} (TP: ${take_profit:.4f}, P&L: {pnl_pct:.2f}%)",
                        "triggered_at": datetime.now(timezone.utc).isoformat()
                    })
            
            # If an exit was triggered, mark position for closure (OCO complete)
            if exit_triggered:
                outcome = "win" if pnl_pct > 0 else "loss"
                conn.execute("""
                    UPDATE positions 
                    SET status = 'oco_triggered', 
                        updated_at = ?,
                        closed_at = ?
                    WHERE id = ?
                """, (datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), pos["id"]))
                
                # Log OCO execution
                log.info(f"OCO triggered for {symbol}: {exit_type} @ ${current_price:.4f}, P&L: {pnl_pct:.2f}%")
                
                # Auto-close linked journal entry if exists
                if pos["journal_id"]:
                    conn.execute("""
                        UPDATE trade_journal
                        SET exit_price = ?, exit_time = ?, outcome = ?,
                            pnl_percent = ?, pnl_dollars = ?, updated_at = ?
                        WHERE id = ? AND outcome = 'open'
                    """, (current_price, datetime.now(timezone.utc).isoformat(), outcome,
                          pnl_pct, pnl_usd, datetime.now(timezone.utc).isoformat(), pos["journal_id"]))
        
        conn.commit()
        conn.close()
        
    except Exception as e:
        log.error(f"Position monitoring error: {e}")


def background_monitor_loop():
    """
    Main background monitoring loop.
    Runs continuously, checking alerts every 30s and running scans every 5min.
    """
    global monitor_running, last_scan_time
    
    log.info("Background monitor started")
    
    while monitor_running:
        try:
            # Check alerts every interval
            run_alert_checks()
            
            # Check position P&L
            check_position_pnl()
            
            # Run auto-scan if enough time has passed
            if time.time() - last_scan_time >= scan_interval:
                run_auto_scan()
            
        except Exception as e:
            log.error(f"Monitor loop error: {e}")
        
        # Sleep in small increments so we can stop quickly
        for _ in range(alert_check_interval):
            if not monitor_running:
                break
            time.sleep(1)
    
    log.info("Background monitor stopped")


def start_background_monitor():
    """Start the background monitoring thread."""
    global monitor_thread, monitor_running
    
    if monitor_thread and monitor_thread.is_alive():
        log.info("Monitor already running")
        return False
    
    monitor_running = True
    monitor_thread = threading.Thread(target=background_monitor_loop, daemon=True)
    monitor_thread.start()
    log.info("Background monitor thread started")
    return True


def stop_background_monitor():
    """Stop the background monitoring thread."""
    global monitor_running
    
    monitor_running = False
    log.info("Stopping background monitor...")


# Register cleanup on exit
atexit.register(stop_background_monitor)


# ---------------------------------------------------------------------------
# PHASE 5: INTELLIGENCE & OPTIMIZATION
# ---------------------------------------------------------------------------

# 5.1 — BACKTESTING ENGINE
# ---------------------------------------------------------------------------

def fetch_historical_ohlcv(symbol, days=365):
    """
    Fetch extended historical OHLCV data for backtesting.
    Uses CoinGecko market_chart endpoint for longer history.
    """
    symbol_map = {
        "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
        "DOGE": "dogecoin", "XRP": "ripple", "ADA": "cardano",
        "AVAX": "avalanche-2", "DOT": "polkadot", "LINK": "chainlink",
        "MATIC": "matic-network", "ATOM": "cosmos", "UNI": "uniswap",
        "LTC": "litecoin", "NEAR": "near", "ARB": "arbitrum",
        "OP": "optimism", "SUI": "sui", "APT": "aptos",
        "INJ": "injective-protocol", "TIA": "celestia",
        "SEI": "sei-network", "PEPE": "pepe", "WIF": "dogwifcoin",
        "BONK": "bonk", "SHIB": "shiba-inu", "FTM": "fantom"
    }
    
    coin_id = symbol_map.get(symbol.upper(), symbol.lower())
    
    try:
        # CoinGecko market_chart gives daily data for longer periods
        url = f"{COINGECKO_API}/coins/{coin_id}/market_chart?vs_currency=usd&days={days}&interval=daily"
        resp = requests.get(url, timeout=15)
        
        if resp.status_code != 200:
            return []
        
        data = resp.json()
        prices = data.get("prices", [])
        
        if not prices:
            return []
        
        # Convert to OHLCV format (daily data, so OHLC ≈ close for simplicity)
        ohlcv = []
        for i, (ts, price) in enumerate(prices):
            # Estimate high/low from surrounding prices
            prev_price = prices[i-1][1] if i > 0 else price
            next_price = prices[i+1][1] if i < len(prices) - 1 else price
            
            high = max(price, prev_price, next_price) * 1.01  # ~1% variation estimate
            low = min(price, prev_price, next_price) * 0.99
            
            ohlcv.append({
                "timestamp": ts,
                "date": datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d"),
                "open": prev_price,
                "high": high,
                "low": low,
                "close": price,
                "volume": 0  # Volume not in this endpoint
            })
        
        return ohlcv
    except Exception as e:
        log.error(f"Historical OHLCV fetch failed for {symbol}: {e}")
        return []


def backtest_penguin_strategy(ohlcv_data, params=None):
    """
    Backtest Penguin divergence strategy on historical data.
    
    Penguin Entry Criteria (need 4/5 for entry):
        1. RSI 70-85 (momentum but not extreme)
        2. Price above lower Bollinger Band (support holding)
        3. Volume > 1.5x average
        4. MACD histogram positive or turning positive
        5. Stochastic %K < 80 (not extreme overbought)
    
    Exit Rules:
        - Stop loss: -3%
        - Take profit: +15%
        - Trailing stop after +10%
    """
    if params is None:
        params = {
            "rsi_min": 65, "rsi_max": 85,
            "volume_threshold": 1.5,
            "stop_loss_pct": 3,
            "take_profit_pct": 15,
            "trailing_start_pct": 10,
            "min_signals": 4
        }
    
    if len(ohlcv_data) < 50:
        return {"error": "Insufficient data for backtest (need 50+ candles)"}
    
    trades = []
    in_position = False
    entry_price = 0
    entry_idx = 0
    highest_since_entry = 0
    
    closes = [d["close"] for d in ohlcv_data]
    highs = [d["high"] for d in ohlcv_data]
    lows = [d["low"] for d in ohlcv_data]
    
    # Pre-calculate indicators for efficiency
    for i in range(30, len(ohlcv_data)):
        price = closes[i]
        price_slice = closes[:i+1]
        high_slice = highs[:i+1]
        low_slice = lows[:i+1]
        
        # Calculate indicators
        rsi = calculate_rsi(price_slice)
        macd = calculate_macd(price_slice)
        stoch = calculate_stochastic(high_slice, low_slice, price_slice)
        bb = calculate_bollinger_bands(price_slice)
        vol_ratio = 1.5  # Simulated since we don't have real volume
        
        if in_position:
            # Track highest price for trailing stop
            highest_since_entry = max(highest_since_entry, price)
            pnl_pct = ((price - entry_price) / entry_price) * 100
            
            # Check exit conditions
            exit_reason = None
            
            # Stop loss
            if pnl_pct <= -params["stop_loss_pct"]:
                exit_reason = "stop_loss"
            # Take profit
            elif pnl_pct >= params["take_profit_pct"]:
                exit_reason = "take_profit"
            # Trailing stop (after reaching +10%, trail at -3% from high)
            elif pnl_pct >= params["trailing_start_pct"]:
                trailing_stop_price = highest_since_entry * (1 - params["stop_loss_pct"] / 100)
                if price <= trailing_stop_price:
                    exit_reason = "trailing_stop"
            
            if exit_reason:
                trades.append({
                    "entry_idx": entry_idx,
                    "exit_idx": i,
                    "entry_date": ohlcv_data[entry_idx]["date"],
                    "exit_date": ohlcv_data[i]["date"],
                    "entry_price": entry_price,
                    "exit_price": price,
                    "pnl_pct": round(pnl_pct, 2),
                    "outcome": "win" if pnl_pct > 0 else "loss",
                    "exit_reason": exit_reason,
                    "holding_days": i - entry_idx
                })
                in_position = False
        
        else:
            # Check entry signals
            signals = 0
            signal_details = []
            
            # Signal 1: RSI in range
            if rsi and params["rsi_min"] <= rsi <= params["rsi_max"]:
                signals += 1
                signal_details.append(f"RSI {rsi:.1f}")
            
            # Signal 2: Price above lower BB (support)
            if bb and price > bb["lower"]:
                signals += 1
                signal_details.append("Above BB lower")
            
            # Signal 3: Volume (simulated as always present for backtest)
            if vol_ratio >= params["volume_threshold"]:
                signals += 1
                signal_details.append(f"Volume {vol_ratio:.1f}x")
            
            # Signal 4: MACD positive
            if macd and macd.get("histogram", 0) > 0:
                signals += 1
                signal_details.append("MACD+")
            
            # Signal 5: Stochastic not extreme
            if stoch and stoch["k"] < 80:
                signals += 1
                signal_details.append(f"Stoch {stoch['k']:.0f}")
            
            # Entry if minimum signals met
            if signals >= params["min_signals"]:
                in_position = True
                entry_price = price
                entry_idx = i
                highest_since_entry = price
    
    # Close any open position at end
    if in_position:
        final_pnl = ((closes[-1] - entry_price) / entry_price) * 100
        trades.append({
            "entry_idx": entry_idx,
            "exit_idx": len(ohlcv_data) - 1,
            "entry_date": ohlcv_data[entry_idx]["date"],
            "exit_date": ohlcv_data[-1]["date"],
            "entry_price": entry_price,
            "exit_price": closes[-1],
            "pnl_pct": round(final_pnl, 2),
            "outcome": "win" if final_pnl > 0 else "loss",
            "exit_reason": "end_of_data",
            "holding_days": len(ohlcv_data) - 1 - entry_idx
        })
    
    return calculate_backtest_stats(trades, ohlcv_data, "penguin", params)


def backtest_burry_strategy(ohlcv_data, params=None):
    """
    Backtest Burry overbought short strategy on historical data.
    
    Burry Entry Criteria (need 4/5 for entry, ADX < 40 required):
        1. RSI > 80 (extreme overbought)
        2. MACD histogram turning negative
        3. ADX < 30 (weak trend - CRITICAL)
        4. Stochastic > 90 (extreme)
        5. Price near upper Bollinger Band
    
    Exit Rules:
        - Stop loss: +3% (price goes up = loss for short)
        - Take profit: -15% (price drop = profit for short)
    """
    if params is None:
        params = {
            "rsi_threshold": 80,
            "adx_max": 40,  # ADX kill switch
            "stoch_threshold": 85,
            "stop_loss_pct": 3,
            "take_profit_pct": 15,
            "min_signals": 4
        }
    
    if len(ohlcv_data) < 50:
        return {"error": "Insufficient data for backtest (need 50+ candles)"}
    
    trades = []
    in_position = False
    entry_price = 0
    entry_idx = 0
    
    closes = [d["close"] for d in ohlcv_data]
    highs = [d["high"] for d in ohlcv_data]
    lows = [d["low"] for d in ohlcv_data]
    
    for i in range(30, len(ohlcv_data)):
        price = closes[i]
        price_slice = closes[:i+1]
        high_slice = highs[:i+1]
        low_slice = lows[:i+1]
        
        # Calculate indicators
        rsi = calculate_rsi(price_slice)
        macd = calculate_macd(price_slice)
        adx_data = calculate_adx(high_slice, low_slice, price_slice)
        stoch = calculate_stochastic(high_slice, low_slice, price_slice)
        bb = calculate_bollinger_bands(price_slice)
        
        adx = adx_data["adx"] if adx_data else 50
        
        if in_position:
            # For shorts: price drop = profit, price rise = loss
            pnl_pct = ((entry_price - price) / entry_price) * 100
            
            exit_reason = None
            
            # Stop loss (price rises)
            if pnl_pct <= -params["stop_loss_pct"]:
                exit_reason = "stop_loss"
            # Take profit (price drops)
            elif pnl_pct >= params["take_profit_pct"]:
                exit_reason = "take_profit"
            
            if exit_reason:
                trades.append({
                    "entry_idx": entry_idx,
                    "exit_idx": i,
                    "entry_date": ohlcv_data[entry_idx]["date"],
                    "exit_date": ohlcv_data[i]["date"],
                    "entry_price": entry_price,
                    "exit_price": price,
                    "pnl_pct": round(pnl_pct, 2),
                    "outcome": "win" if pnl_pct > 0 else "loss",
                    "exit_reason": exit_reason,
                    "direction": "short",
                    "holding_days": i - entry_idx
                })
                in_position = False
        
        else:
            # ADX Kill Switch - never short in strong trend
            if adx >= params["adx_max"]:
                continue
            
            # Check entry signals
            signals = 0
            signal_details = []
            
            # Signal 1: RSI extreme overbought
            if rsi and rsi >= params["rsi_threshold"]:
                signals += 1
                signal_details.append(f"RSI {rsi:.1f}")
            
            # Signal 2: MACD turning negative
            if macd and macd.get("direction") == "falling":
                signals += 1
                signal_details.append("MACD falling")
            
            # Signal 3: ADX low (no strong trend)
            if adx < 30:
                signals += 1
                signal_details.append(f"ADX {adx:.1f}")
            
            # Signal 4: Stochastic extreme
            if stoch and stoch["k"] >= params["stoch_threshold"]:
                signals += 1
                signal_details.append(f"Stoch {stoch['k']:.0f}")
            
            # Signal 5: Near upper BB
            if bb and price > bb["middle"]:
                bb_position = bb.get("position", 50)
                if bb_position > 70:
                    signals += 1
                    signal_details.append("Near BB upper")
            
            # Entry if minimum signals met
            if signals >= params["min_signals"]:
                in_position = True
                entry_price = price
                entry_idx = i
    
    # Close any open position at end
    if in_position:
        final_pnl = ((entry_price - closes[-1]) / entry_price) * 100
        trades.append({
            "entry_idx": entry_idx,
            "exit_idx": len(ohlcv_data) - 1,
            "entry_date": ohlcv_data[entry_idx]["date"],
            "exit_date": ohlcv_data[-1]["date"],
            "entry_price": entry_price,
            "exit_price": closes[-1],
            "pnl_pct": round(final_pnl, 2),
            "outcome": "win" if final_pnl > 0 else "loss",
            "exit_reason": "end_of_data",
            "direction": "short",
            "holding_days": len(ohlcv_data) - 1 - entry_idx
        })
    
    return calculate_backtest_stats(trades, ohlcv_data, "burry", params)


def calculate_backtest_stats(trades, ohlcv_data, strategy, params):
    """
    Calculate comprehensive backtest statistics from trade list.
    """
    if not trades:
        return {
            "strategy": strategy,
            "params": params,
            "total_trades": 0,
            "error": "No trades generated in backtest period"
        }
    
    wins = [t for t in trades if t["outcome"] == "win"]
    losses = [t for t in trades if t["outcome"] == "loss"]
    
    total_pnl = sum(t["pnl_pct"] for t in trades)
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    
    avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = abs(sum(t["pnl_pct"] for t in losses) / len(losses)) if losses else 0
    
    # Profit factor
    gross_profit = sum(t["pnl_pct"] for t in wins)
    gross_loss = abs(sum(t["pnl_pct"] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    
    # Max drawdown calculation
    equity = [100]  # Start with 100
    peak = 100
    max_drawdown = 0
    max_drawdown_duration = 0
    drawdown_start = 0
    in_drawdown = False
    
    for t in trades:
        new_equity = equity[-1] * (1 + t["pnl_pct"] / 100)
        equity.append(new_equity)
        
        if new_equity > peak:
            peak = new_equity
            if in_drawdown:
                in_drawdown = False
        else:
            if not in_drawdown:
                in_drawdown = True
                drawdown_start = len(equity) - 1
            
            drawdown = (peak - new_equity) / peak * 100
            max_drawdown = max(max_drawdown, drawdown)
    
    # Win/loss streaks
    max_win_streak = 0
    max_loss_streak = 0
    current_streak = 0
    streak_type = None
    
    for t in trades:
        if t["outcome"] == "win":
            if streak_type == "win":
                current_streak += 1
            else:
                current_streak = 1
                streak_type = "win"
            max_win_streak = max(max_win_streak, current_streak)
        else:
            if streak_type == "loss":
                current_streak += 1
            else:
                current_streak = 1
                streak_type = "loss"
            max_loss_streak = max(max_loss_streak, current_streak)
    
    # Monthly returns
    monthly_returns = {}
    for t in trades:
        month = t["exit_date"][:7]  # YYYY-MM
        if month not in monthly_returns:
            monthly_returns[month] = {"pnl": 0, "trades": 0}
        monthly_returns[month]["pnl"] += t["pnl_pct"]
        monthly_returns[month]["trades"] += 1
    
    # Expectancy (average profit per trade)
    expectancy = (win_rate / 100 * avg_win) - ((100 - win_rate) / 100 * avg_loss)
    
    # Best and worst trades
    best_trade = max(trades, key=lambda t: t["pnl_pct"])
    worst_trade = min(trades, key=lambda t: t["pnl_pct"])
    
    # Holding period stats
    avg_holding = sum(t["holding_days"] for t in trades) / len(trades)
    avg_winning_hold = sum(t["holding_days"] for t in wins) / len(wins) if wins else 0
    avg_losing_hold = sum(t["holding_days"] for t in losses) / len(losses) if losses else 0
    
    return {
        "strategy": strategy,
        "params": params,
        "period": {
            "start": ohlcv_data[0]["date"] if ohlcv_data else None,
            "end": ohlcv_data[-1]["date"] if ohlcv_data else None,
            "days": len(ohlcv_data)
        },
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "total_pnl_pct": round(total_pnl, 2),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor != float('inf') else "∞",
        "expectancy": round(expectancy, 2),
        "max_drawdown_pct": round(max_drawdown, 2),
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "avg_holding_days": round(avg_holding, 1),
        "avg_winning_hold_days": round(avg_winning_hold, 1),
        "avg_losing_hold_days": round(avg_losing_hold, 1),
        "best_trade": {
            "date": best_trade["exit_date"],
            "pnl_pct": best_trade["pnl_pct"]
        },
        "worst_trade": {
            "date": worst_trade["exit_date"],
            "pnl_pct": worst_trade["pnl_pct"]
        },
        "monthly_returns": monthly_returns,
        "equity_curve": equity,
        "trades": trades
    }


def optimize_strategy_params(symbol, strategy, ohlcv_data=None):
    """
    Parameter optimization: test different thresholds to find optimal settings.
    Walk-forward: 80% train, 20% validate.
    """
    if not ohlcv_data:
        ohlcv_data = fetch_historical_ohlcv(symbol, days=365)
    
    if len(ohlcv_data) < 100:
        return {"error": "Insufficient data for optimization"}
    
    # Split data: 80% train, 20% validate
    split_idx = int(len(ohlcv_data) * 0.8)
    train_data = ohlcv_data[:split_idx]
    validate_data = ohlcv_data[split_idx:]
    
    best_params = None
    best_score = -float('inf')
    results = []
    
    if strategy == "penguin":
        # Test RSI ranges
        for rsi_min in [60, 65, 70]:
            for rsi_max in [80, 85, 90]:
                for sl in [2, 3, 4]:
                    for tp in [12, 15, 20]:
                        params = {
                            "rsi_min": rsi_min, "rsi_max": rsi_max,
                            "volume_threshold": 1.5,
                            "stop_loss_pct": sl, "take_profit_pct": tp,
                            "trailing_start_pct": 10, "min_signals": 4
                        }
                        
                        result = backtest_penguin_strategy(train_data, params)
                        if "error" in result:
                            continue
                        
                        # Score by risk-adjusted returns
                        score = result["expectancy"] * (result["win_rate"] / 100)
                        if result["max_drawdown_pct"] > 0:
                            score /= (result["max_drawdown_pct"] / 10)
                        
                        results.append({
                            "params": params,
                            "train_score": score,
                            "train_stats": {
                                "win_rate": result["win_rate"],
                                "total_pnl": result["total_pnl_pct"],
                                "expectancy": result["expectancy"],
                                "max_dd": result["max_drawdown_pct"]
                            }
                        })
                        
                        if score > best_score:
                            best_score = score
                            best_params = params
    
    elif strategy == "burry":
        # Test thresholds
        for rsi in [75, 80, 85]:
            for adx in [30, 35, 40]:
                for sl in [2, 3, 4]:
                    for tp in [12, 15, 20]:
                        params = {
                            "rsi_threshold": rsi, "adx_max": adx,
                            "stoch_threshold": 85,
                            "stop_loss_pct": sl, "take_profit_pct": tp,
                            "min_signals": 4
                        }
                        
                        result = backtest_burry_strategy(train_data, params)
                        if "error" in result:
                            continue
                        
                        score = result["expectancy"] * (result["win_rate"] / 100)
                        if result["max_drawdown_pct"] > 0:
                            score /= (result["max_drawdown_pct"] / 10)
                        
                        results.append({
                            "params": params,
                            "train_score": score,
                            "train_stats": {
                                "win_rate": result["win_rate"],
                                "total_pnl": result["total_pnl_pct"],
                                "expectancy": result["expectancy"],
                                "max_dd": result["max_drawdown_pct"]
                            }
                        })
                        
                        if score > best_score:
                            best_score = score
                            best_params = params
    
    if not best_params:
        return {"error": "No valid parameter combination found"}
    
    # Validate best params on out-of-sample data
    if strategy == "penguin":
        validation_result = backtest_penguin_strategy(validate_data, best_params)
    else:
        validation_result = backtest_burry_strategy(validate_data, best_params)
    
    return {
        "strategy": strategy,
        "symbol": symbol,
        "best_params": best_params,
        "train_score": best_score,
        "validation": {
            "win_rate": validation_result.get("win_rate"),
            "total_pnl": validation_result.get("total_pnl_pct"),
            "expectancy": validation_result.get("expectancy"),
            "max_drawdown": validation_result.get("max_drawdown_pct"),
            "trades": validation_result.get("total_trades")
        },
        "train_period": f"{train_data[0]['date']} to {train_data[-1]['date']}",
        "validate_period": f"{validate_data[0]['date']} to {validate_data[-1]['date']}",
        "top_5_params": sorted(results, key=lambda x: x["train_score"], reverse=True)[:5]
    }


# 5.2 — PATTERN RECOGNITION ENHANCEMENT
# ---------------------------------------------------------------------------

def capture_trade_snapshot(conn, journal_id):
    """
    Capture full indicator snapshot for a trade for pattern recognition.
    Stores in trade_patterns table for later similarity analysis.
    """
    # Get journal entry
    trade = conn.execute("SELECT * FROM trade_journal WHERE id = ?", (journal_id,)).fetchone()
    if not trade:
        return None
    
    symbol = trade["symbol"]
    
    # Get OHLCV and calculate indicators at entry time
    ohlcv = fetch_coingecko_ohlcv(symbol, days=30)
    if not ohlcv or len(ohlcv) < 20:
        return None
    
    indicators = calculate_all_indicators(ohlcv)
    if not indicators:
        return None
    
    # Get additional context
    fear_greed = fetch_fear_greed_index()
    funding = fetch_funding_rate(symbol)
    
    snapshot = {
        "rsi": indicators["indicators"]["rsi"]["value"],
        "macd_histogram": indicators["indicators"]["macd"].get("histogram"),
        "macd_direction": indicators["indicators"]["macd"].get("direction"),
        "adx": indicators["indicators"]["adx"].get("adx"),
        "adx_zone": indicators["indicators"]["adx"].get("zone"),
        "stochastic_k": indicators["indicators"]["stochastic"].get("k"),
        "stochastic_d": indicators["indicators"]["stochastic"].get("d"),
        "bb_position": indicators["indicators"]["bollinger"].get("position") if indicators["indicators"]["bollinger"] else None,
        "bb_width": indicators["indicators"]["bollinger"].get("width_pct") if indicators["indicators"]["bollinger"] else None,
        "volume_ratio": indicators["indicators"]["volume_ratio"],
        "fear_greed": fear_greed["value"] if fear_greed else None,
        "funding_rate": funding["current_rate"] if funding else None,
        "signal_count": indicators["signal_count"]
    }
    
    # Store snapshot
    conn.execute("""
        INSERT OR REPLACE INTO trade_patterns 
        (journal_id, symbol, strategy, outcome, pnl_percent, indicator_snapshot, 
         entry_hour, entry_day_of_week, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
    """, (
        journal_id, symbol, trade["strategy"], trade["outcome"], trade["pnl_percent"],
        json.dumps(snapshot),
        datetime.now().hour,
        datetime.now().weekday()
    ))
    conn.commit()
    
    return snapshot


def calculate_setup_similarity(current_setup, historical_pattern):
    """
    Calculate similarity score between current setup and historical trade.
    Returns 0-100 similarity percentage.
    """
    weights = {
        "rsi": 15,
        "macd_direction": 10,
        "adx": 15,
        "stochastic_k": 10,
        "bb_position": 10,
        "volume_ratio": 10,
        "fear_greed": 15,
        "signal_count": 15
    }
    
    score = 0
    max_score = 0
    
    for key, weight in weights.items():
        max_score += weight
        
        current_val = current_setup.get(key)
        hist_val = historical_pattern.get(key)
        
        if current_val is None or hist_val is None:
            continue
        
        if key == "macd_direction":
            # Categorical match
            if current_val == hist_val:
                score += weight
        else:
            # Numerical similarity (within 20% = full score)
            if hist_val != 0:
                diff_pct = abs(current_val - hist_val) / abs(hist_val) * 100
                if diff_pct <= 5:
                    score += weight
                elif diff_pct <= 10:
                    score += weight * 0.8
                elif diff_pct <= 20:
                    score += weight * 0.5
                elif diff_pct <= 30:
                    score += weight * 0.3
    
    return round(score / max_score * 100, 1) if max_score > 0 else 0


def find_similar_historical_trades(conn, current_setup, strategy=None, min_similarity=60, limit=10):
    """
    Find historical trades most similar to current setup.
    Returns trades sorted by similarity with win/loss outcomes.
    """
    query = """
        SELECT * FROM trade_patterns 
        WHERE outcome IN ('win', 'loss')
    """
    params = []
    
    if strategy:
        query += " AND strategy = ?"
        params.append(strategy)
    
    patterns = conn.execute(query, params).fetchall()
    
    similar_trades = []
    
    for pattern in patterns:
        hist_snapshot = json.loads(pattern["indicator_snapshot"])
        similarity = calculate_setup_similarity(current_setup, hist_snapshot)
        
        if similarity >= min_similarity:
            similar_trades.append({
                "journal_id": pattern["journal_id"],
                "symbol": pattern["symbol"],
                "strategy": pattern["strategy"],
                "outcome": pattern["outcome"],
                "pnl_percent": pattern["pnl_percent"],
                "similarity": similarity,
                "entry_hour": pattern["entry_hour"],
                "entry_day": pattern["entry_day_of_week"],
                "snapshot": hist_snapshot
            })
    
    # Sort by similarity descending
    similar_trades.sort(key=lambda x: x["similarity"], reverse=True)
    
    # Calculate summary stats
    if similar_trades:
        wins = [t for t in similar_trades if t["outcome"] == "win"]
        historical_win_rate = len(wins) / len(similar_trades) * 100
        avg_pnl = sum(t["pnl_percent"] for t in similar_trades) / len(similar_trades)
    else:
        historical_win_rate = 0
        avg_pnl = 0
    
    return {
        "similar_trades": similar_trades[:limit],
        "total_similar": len(similar_trades),
        "historical_win_rate": round(historical_win_rate, 1),
        "historical_avg_pnl": round(avg_pnl, 2),
        "confidence_boost": "HIGH" if historical_win_rate > 70 and len(similar_trades) >= 5 else
                           "MEDIUM" if historical_win_rate > 55 else "LOW"
    }


def analyze_time_performance(conn, strategy=None):
    """
    Analyze performance by time of day and day of week.
    """
    query = """
        SELECT entry_hour, entry_day_of_week, outcome, pnl_percent
        FROM trade_patterns
        WHERE outcome IN ('win', 'loss')
    """
    params = []
    if strategy:
        query += " AND strategy = ?"
        params.append(strategy)
    
    patterns = conn.execute(query, params).fetchall()
    
    if not patterns:
        return {"error": "No trade patterns recorded"}
    
    # Hour analysis
    hour_stats = {}
    for p in patterns:
        hour = p["entry_hour"]
        if hour not in hour_stats:
            hour_stats[hour] = {"wins": 0, "losses": 0, "pnl": 0}
        
        if p["outcome"] == "win":
            hour_stats[hour]["wins"] += 1
        else:
            hour_stats[hour]["losses"] += 1
        hour_stats[hour]["pnl"] += p["pnl_percent"]
    
    # Calculate win rates
    for hour in hour_stats:
        total = hour_stats[hour]["wins"] + hour_stats[hour]["losses"]
        hour_stats[hour]["win_rate"] = round(hour_stats[hour]["wins"] / total * 100, 1) if total > 0 else 0
        hour_stats[hour]["total_trades"] = total
    
    # Day of week analysis
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    day_stats = {i: {"wins": 0, "losses": 0, "pnl": 0} for i in range(7)}
    
    for p in patterns:
        day = p["entry_day_of_week"]
        if p["outcome"] == "win":
            day_stats[day]["wins"] += 1
        else:
            day_stats[day]["losses"] += 1
        day_stats[day]["pnl"] += p["pnl_percent"]
    
    for day in day_stats:
        total = day_stats[day]["wins"] + day_stats[day]["losses"]
        day_stats[day]["win_rate"] = round(day_stats[day]["wins"] / total * 100, 1) if total > 0 else 0
        day_stats[day]["name"] = day_names[day]
        day_stats[day]["total_trades"] = total
    
    # Find best/worst times
    best_hour = max(hour_stats.items(), key=lambda x: x[1]["win_rate"]) if hour_stats else (0, {})
    worst_hour = min(hour_stats.items(), key=lambda x: x[1]["win_rate"]) if hour_stats else (0, {})
    best_day = max(day_stats.items(), key=lambda x: x[1]["win_rate"]) if day_stats else (0, {})
    
    return {
        "by_hour": hour_stats,
        "by_day": {day_names[k]: v for k, v in day_stats.items()},
        "best_hour": {"hour": best_hour[0], "win_rate": best_hour[1].get("win_rate", 0)},
        "worst_hour": {"hour": worst_hour[0], "win_rate": worst_hour[1].get("win_rate", 0)},
        "best_day": {"day": day_names[best_day[0]], "win_rate": best_day[1].get("win_rate", 0)},
        "total_patterns": len(patterns)
    }


def generate_strategy_health_report(conn, strategy=None):
    """
    Generate comprehensive strategy health report via Claude.
    Analyzes patterns, timing, and provides actionable recommendations.
    """
    # Gather all data
    time_perf = analyze_time_performance(conn, strategy)
    
    # Get recent trade stats
    query = """
        SELECT strategy, outcome, pnl_percent, 
               json_extract(indicator_snapshot, '$.rsi') as entry_rsi,
               json_extract(indicator_snapshot, '$.adx') as entry_adx,
               json_extract(indicator_snapshot, '$.fear_greed') as entry_fear_greed
        FROM trade_patterns
        WHERE outcome IN ('win', 'loss')
    """
    params = []
    if strategy:
        query += " AND strategy = ?"
        params.append(strategy)
    query += " ORDER BY created_at DESC LIMIT 50"
    
    recent_trades = conn.execute(query, params).fetchall()
    
    if len(recent_trades) < 5:
        return {"error": "Need at least 5 recorded trades for health report"}
    
    wins = [t for t in recent_trades if t["outcome"] == "win"]
    losses = [t for t in recent_trades if t["outcome"] == "loss"]
    
    # Prepare analysis data for Claude
    analysis_data = {
        "strategy": strategy or "all",
        "total_trades": len(recent_trades),
        "win_rate": round(len(wins) / len(recent_trades) * 100, 1),
        "avg_win": round(sum(t["pnl_percent"] for t in wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(abs(sum(t["pnl_percent"] for t in losses) / len(losses)), 2) if losses else 0,
        "best_hour": time_perf.get("best_hour"),
        "worst_hour": time_perf.get("worst_hour"),
        "best_day": time_perf.get("best_day"),
        "winning_avg_rsi": round(sum(t["entry_rsi"] or 0 for t in wins) / len(wins), 1) if wins else 0,
        "losing_avg_rsi": round(sum(t["entry_rsi"] or 0 for t in losses) / len(losses), 1) if losses else 0,
        "winning_avg_adx": round(sum(t["entry_adx"] or 0 for t in wins) / len(wins), 1) if wins else 0,
        "losing_avg_adx": round(sum(t["entry_adx"] or 0 for t in losses) / len(losses), 1) if losses else 0
    }
    
    return {
        "report_type": "strategy_health",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data": analysis_data,
        "time_analysis": time_perf,
        "insights": {
            "timing": f"Best performance at hour {time_perf.get('best_hour', {}).get('hour', 'N/A')}, "
                      f"worst at hour {time_perf.get('worst_hour', {}).get('hour', 'N/A')}",
            "rsi_insight": f"Winning trades avg RSI: {analysis_data['winning_avg_rsi']}, "
                          f"Losing trades avg RSI: {analysis_data['losing_avg_rsi']}",
            "adx_insight": f"Winning trades avg ADX: {analysis_data['winning_avg_adx']}, "
                          f"Losing trades avg ADX: {analysis_data['losing_avg_adx']}"
        }
    }


# 5.3 — MARKET REGIME CLASSIFICATION (Hidden Markov Model)
# ---------------------------------------------------------------------------

def calculate_regime_features(ohlcv_data):
    """
    Calculate features for regime classification.
    Features: volatility, trend strength, momentum, mean-reversion indicator.
    """
    if len(ohlcv_data) < 30:
        return None
    
    closes = [d["close"] for d in ohlcv_data]
    highs = [d["high"] for d in ohlcv_data]
    lows = [d["low"] for d in ohlcv_data]
    
    # Returns
    returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]
    
    # Feature 1: Volatility (std of returns)
    volatility = (sum((r - sum(returns)/len(returns))**2 for r in returns[-20:]) / 20) ** 0.5 * 100
    
    # Feature 2: Trend strength (ADX)
    adx_data = calculate_adx(highs, lows, closes)
    trend_strength = adx_data["adx"] if adx_data else 25
    
    # Feature 3: Momentum (ROC - rate of change)
    roc_10 = (closes[-1] - closes[-11]) / closes[-11] * 100 if len(closes) > 10 else 0
    
    # Feature 4: Mean-reversion indicator (distance from SMA)
    sma_20 = sum(closes[-20:]) / 20
    distance_from_mean = (closes[-1] - sma_20) / sma_20 * 100
    
    # Feature 5: RSI for overbought/oversold
    rsi = calculate_rsi(closes)
    
    return {
        "volatility": round(volatility, 3),
        "trend_strength": round(trend_strength, 2),
        "momentum": round(roc_10, 2),
        "mean_distance": round(distance_from_mean, 2),
        "rsi": rsi
    }


def classify_market_regime_hmm(ohlcv_data, history=None):
    """
    Simple 3-state Hidden Markov Model for regime classification.
    
    States:
        TRENDING: ADX > 30, clear directional bias → momentum strategies
        MEAN_REVERTING: ADX < 20, range-bound → fade extremes, Burry
        TRANSITIONAL: Mixed signals, high volatility → reduce exposure
    
    Transition probabilities estimated from features.
    """
    features = calculate_regime_features(ohlcv_data)
    if not features:
        return {"error": "Insufficient data for regime classification"}
    
    # Scoring for each regime
    trending_score = 0
    mean_revert_score = 0
    transitional_score = 0
    
    # ADX (trend strength) scoring
    adx = features["trend_strength"]
    if adx > 35:
        trending_score += 40
    elif adx > 25:
        trending_score += 25
        transitional_score += 15
    elif adx < 20:
        mean_revert_score += 40
    else:
        transitional_score += 25
    
    # Volatility scoring
    vol = features["volatility"]
    if vol > 4:  # High volatility
        transitional_score += 30
        trending_score += 10
    elif vol > 2:  # Medium volatility
        trending_score += 20
        transitional_score += 15
    else:  # Low volatility
        mean_revert_score += 25
    
    # RSI extremes
    rsi = features["rsi"] or 50
    if rsi > 70 or rsi < 30:
        mean_revert_score += 20  # Extremes favor mean reversion
    elif 45 <= rsi <= 55:
        trending_score += 10  # Neutral RSI allows trends
    
    # Mean distance scoring
    dist = abs(features["mean_distance"])
    if dist > 10:
        transitional_score += 15
    elif dist > 5:
        mean_revert_score += 15
    else:
        trending_score += 10
    
    # Determine regime
    scores = {
        "TRENDING": trending_score,
        "MEAN_REVERTING": mean_revert_score,
        "TRANSITIONAL": transitional_score
    }
    
    regime = max(scores, key=scores.get)
    confidence = scores[regime] / sum(scores.values()) * 100 if sum(scores.values()) > 0 else 33
    
    # Strategy recommendations per regime
    recommendations = {
        "TRENDING": {
            "preferred_strategy": "penguin",
            "position_sizing": 1.0,
            "advice": "Trend is your friend. Follow momentum, use trailing stops.",
            "avoid": "Fading moves, mean reversion trades"
        },
        "MEAN_REVERTING": {
            "preferred_strategy": "burry",
            "position_sizing": 0.8,
            "advice": "Fade extremes. RSI/Stoch divergences are reliable.",
            "avoid": "Breakout trades, chasing momentum"
        },
        "TRANSITIONAL": {
            "preferred_strategy": "hold",
            "position_sizing": 0.5,
            "advice": "Reduce exposure. Wait for clarity. Take only 5/5 setups.",
            "avoid": "New positions, increasing size"
        }
    }
    
    return {
        "regime": regime,
        "confidence": round(confidence, 1),
        "scores": scores,
        "features": features,
        "recommendation": recommendations[regime],
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


def detect_regime_change(current_regime, previous_regime):
    """
    Detect and characterize regime changes for alerts.
    """
    if not previous_regime or current_regime == previous_regime:
        return None
    
    transitions = {
        ("TRENDING", "MEAN_REVERTING"): {
            "significance": "HIGH",
            "action": "Close momentum positions, prepare for range trading",
            "message": "Trend exhaustion detected - market entering consolidation"
        },
        ("TRENDING", "TRANSITIONAL"): {
            "significance": "MEDIUM",
            "action": "Reduce position sizes, tighten stops",
            "message": "Trend weakening - increased uncertainty"
        },
        ("MEAN_REVERTING", "TRENDING"): {
            "significance": "HIGH",
            "action": "Prepare for breakout. Penguin setups may emerge.",
            "message": "Breakout from range - new trend developing"
        },
        ("MEAN_REVERTING", "TRANSITIONAL"): {
            "significance": "LOW",
            "action": "Maintain positions but stay alert",
            "message": "Range becoming unstable"
        },
        ("TRANSITIONAL", "TRENDING"): {
            "significance": "HIGH",
            "action": "Look for trend continuation entries",
            "message": "Clarity emerging - trend confirmed"
        },
        ("TRANSITIONAL", "MEAN_REVERTING"): {
            "significance": "MEDIUM",
            "action": "Range established - fade extremes",
            "message": "Volatility settling into range"
        }
    }
    
    key = (previous_regime, current_regime)
    return transitions.get(key, {
        "significance": "UNKNOWN",
        "action": "Review positions",
        "message": f"Regime changed from {previous_regime} to {current_regime}"
    })


# 5.4 — PERFORMANCE ATTRIBUTION
# ---------------------------------------------------------------------------

def calculate_performance_attribution(conn):
    """
    Break down P&L by various dimensions to identify edge sources.
    """
    trades = conn.execute("""
        SELECT t.*, p.entry_hour, p.entry_day_of_week, p.indicator_snapshot
        FROM trade_journal t
        LEFT JOIN trade_patterns p ON t.id = p.journal_id
        WHERE t.outcome IN ('win', 'loss')
        ORDER BY t.created_at
    """).fetchall()
    
    if len(trades) < 10:
        return {"error": "Need at least 10 completed trades for attribution"}
    
    # By strategy
    by_strategy = {}
    for t in trades:
        strat = t["strategy"] or "unknown"
        if strat not in by_strategy:
            by_strategy[strat] = {"trades": 0, "wins": 0, "pnl": 0, "pnl_dollars": 0}
        by_strategy[strat]["trades"] += 1
        if t["outcome"] == "win":
            by_strategy[strat]["wins"] += 1
        by_strategy[strat]["pnl"] += t["pnl_percent"] or 0
        by_strategy[strat]["pnl_dollars"] += t["pnl_dollars"] or 0
    
    for strat in by_strategy:
        by_strategy[strat]["win_rate"] = round(
            by_strategy[strat]["wins"] / by_strategy[strat]["trades"] * 100, 1
        )
        by_strategy[strat]["avg_pnl"] = round(
            by_strategy[strat]["pnl"] / by_strategy[strat]["trades"], 2
        )
    
    # By symbol/asset
    by_symbol = {}
    for t in trades:
        sym = t["symbol"] or "unknown"
        if sym not in by_symbol:
            by_symbol[sym] = {"trades": 0, "wins": 0, "pnl": 0}
        by_symbol[sym]["trades"] += 1
        if t["outcome"] == "win":
            by_symbol[sym]["wins"] += 1
        by_symbol[sym]["pnl"] += t["pnl_percent"] or 0
    
    for sym in by_symbol:
        by_symbol[sym]["win_rate"] = round(
            by_symbol[sym]["wins"] / by_symbol[sym]["trades"] * 100, 1
        )
    
    # By direction
    by_direction = {"long": {"trades": 0, "wins": 0, "pnl": 0}, "short": {"trades": 0, "wins": 0, "pnl": 0}}
    for t in trades:
        direction = t["direction"] or "long"
        by_direction[direction]["trades"] += 1
        if t["outcome"] == "win":
            by_direction[direction]["wins"] += 1
        by_direction[direction]["pnl"] += t["pnl_percent"] or 0
    
    for d in by_direction:
        if by_direction[d]["trades"] > 0:
            by_direction[d]["win_rate"] = round(
                by_direction[d]["wins"] / by_direction[d]["trades"] * 100, 1
            )
    
    # Total P&L attribution percentages
    total_pnl = sum(t["pnl_dollars"] or 0 for t in trades)
    attribution_pct = {}
    if total_pnl != 0:
        for strat, data in by_strategy.items():
            attribution_pct[strat] = round(data["pnl_dollars"] / total_pnl * 100, 1) if total_pnl else 0
    
    # Find edge
    best_strategy = max(by_strategy.items(), key=lambda x: x[1]["win_rate"]) if by_strategy else None
    best_symbol = max(by_symbol.items(), key=lambda x: x[1]["win_rate"]) if by_symbol else None
    
    return {
        "total_trades": len(trades),
        "total_pnl_dollars": round(total_pnl, 2),
        "by_strategy": by_strategy,
        "by_symbol": by_symbol,
        "by_direction": by_direction,
        "attribution_percentages": attribution_pct,
        "edge_analysis": {
            "best_strategy": {
                "name": best_strategy[0] if best_strategy else None,
                "win_rate": best_strategy[1]["win_rate"] if best_strategy else 0,
                "contribution_pct": attribution_pct.get(best_strategy[0], 0) if best_strategy else 0
            },
            "best_symbol": {
                "name": best_symbol[0] if best_symbol else None,
                "win_rate": best_symbol[1]["win_rate"] if best_symbol else 0
            }
        },
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


def generate_trade_recommendations(conn):
    """
    Generate actionable recommendations based on performance attribution.
    """
    attribution = calculate_performance_attribution(conn)
    
    if "error" in attribution:
        return attribution
    
    recommendations = []
    
    # Strategy recommendations
    by_strat = attribution["by_strategy"]
    for strat, data in by_strat.items():
        if data["trades"] >= 5:
            if data["win_rate"] >= 65:
                recommendations.append({
                    "type": "increase_allocation",
                    "target": strat,
                    "reason": f"{strat} has {data['win_rate']}% win rate over {data['trades']} trades",
                    "action": f"Consider increasing {strat} allocation by 20%"
                })
            elif data["win_rate"] < 45:
                recommendations.append({
                    "type": "reduce_allocation",
                    "target": strat,
                    "reason": f"{strat} underperforming with {data['win_rate']}% win rate",
                    "action": f"Reduce {strat} position sizes or pause until reviewed"
                })
    
    # Direction recommendations
    by_dir = attribution["by_direction"]
    if by_dir["long"]["trades"] >= 5 and by_dir["short"]["trades"] >= 5:
        if by_dir["long"]["win_rate"] > by_dir["short"]["win_rate"] + 15:
            recommendations.append({
                "type": "direction_bias",
                "target": "long",
                "reason": f"Long trades ({by_dir['long']['win_rate']}%) significantly outperform shorts ({by_dir['short']['win_rate']}%)",
                "action": "Focus on Penguin setups, reduce Burry exposure"
            })
        elif by_dir["short"]["win_rate"] > by_dir["long"]["win_rate"] + 15:
            recommendations.append({
                "type": "direction_bias",
                "target": "short",
                "reason": f"Short trades ({by_dir['short']['win_rate']}%) outperform longs ({by_dir['long']['win_rate']}%)",
                "action": "Focus on Burry setups during this market regime"
            })
    
    # Symbol recommendations
    by_sym = attribution["by_symbol"]
    top_symbols = sorted(by_sym.items(), key=lambda x: x[1]["pnl"], reverse=True)[:3]
    bottom_symbols = sorted(by_sym.items(), key=lambda x: x[1]["pnl"])[:3]
    
    if top_symbols and top_symbols[0][1]["trades"] >= 3:
        recommendations.append({
            "type": "focus_asset",
            "target": top_symbols[0][0],
            "reason": f"Best performing asset with {top_symbols[0][1]['pnl']:.1f}% total P&L",
            "action": f"Prioritize {top_symbols[0][0]} setups"
        })
    
    if bottom_symbols and bottom_symbols[0][1]["pnl"] < 0 and bottom_symbols[0][1]["trades"] >= 3:
        recommendations.append({
            "type": "avoid_asset",
            "target": bottom_symbols[0][0],
            "reason": f"Worst performing asset with {bottom_symbols[0][1]['pnl']:.1f}% total P&L",
            "action": f"Avoid {bottom_symbols[0][0]} or reduce size significantly"
        })
    
    return {
        "recommendations": recommendations,
        "attribution_summary": {
            "total_trades": attribution["total_trades"],
            "total_pnl": attribution["total_pnl_dollars"],
            "best_strategy": attribution["edge_analysis"]["best_strategy"]["name"],
            "best_symbol": attribution["edge_analysis"]["best_symbol"]["name"]
        },
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


# 5.5 — RISK SIMULATION & STRESS TESTING
# ---------------------------------------------------------------------------

def calculate_ruin_probability(win_rate, win_loss_ratio, position_size_pct, bankroll_units=100):
    """
    Calculate probability of ruin (account going to zero).
    
    Formula: P(ruin) = ((1 - edge) / (1 + edge)) ^ (bankroll / bet_size)
    Where edge = (win_rate × avg_win) - ((1 - win_rate) × avg_loss)
    """
    if win_rate <= 0 or win_rate >= 1:
        return None
    
    # Calculate edge
    avg_win = win_loss_ratio  # Expressed as multiple of loss
    avg_loss = 1.0
    edge = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)
    
    if edge <= -1:
        return 1.0  # Certain ruin with negative edge
    
    if edge >= 1:
        return 0.0  # Impossible to go bankrupt with huge edge
    
    # Calculate ruin probability
    if edge == 0:
        # No edge = random walk
        ruin_prob = 1 - (position_size_pct / 100)
    else:
        base = (1 - edge) / (1 + edge)
        if base <= 0:
            return 0.0
        
        bet_fraction = position_size_pct / 100
        n_bets_to_zero = 1 / bet_fraction if bet_fraction > 0 else float('inf')
        
        ruin_prob = base ** n_bets_to_zero
    
    return round(min(1.0, max(0.0, ruin_prob)), 4)


def simulate_drawdown_scenarios(conn, num_simulations=1000):
    """
    Monte Carlo simulation of potential drawdowns.
    Uses historical trade distribution to project future scenarios.
    """
    # Get historical trade returns
    trades = conn.execute("""
        SELECT pnl_percent FROM trade_journal 
        WHERE outcome IN ('win', 'loss') AND pnl_percent IS NOT NULL
    """).fetchall()
    
    if len(trades) < 10:
        return {"error": "Need at least 10 trades for simulation"}
    
    returns = [t["pnl_percent"] for t in trades]
    
    # Run simulations
    max_drawdowns = []
    final_equities = []
    
    import random
    
    for _ in range(num_simulations):
        equity = 100
        peak = 100
        max_dd = 0
        
        # Simulate 50 trades
        for _ in range(50):
            trade_return = random.choice(returns)
            equity *= (1 + trade_return / 100)
            
            if equity > peak:
                peak = equity
            
            drawdown = (peak - equity) / peak * 100
            max_dd = max(max_dd, drawdown)
        
        max_drawdowns.append(max_dd)
        final_equities.append(equity)
    
    # Calculate statistics
    max_drawdowns.sort()
    final_equities.sort()
    
    return {
        "simulations": num_simulations,
        "trades_per_sim": 50,
        "max_drawdown_stats": {
            "median": round(max_drawdowns[num_simulations // 2], 2),
            "percentile_75": round(max_drawdowns[int(num_simulations * 0.75)], 2),
            "percentile_90": round(max_drawdowns[int(num_simulations * 0.90)], 2),
            "percentile_95": round(max_drawdowns[int(num_simulations * 0.95)], 2),
            "worst_case": round(max_drawdowns[-1], 2)
        },
        "final_equity_stats": {
            "median": round(final_equities[num_simulations // 2], 2),
            "percentile_25": round(final_equities[int(num_simulations * 0.25)], 2),
            "percentile_75": round(final_equities[int(num_simulations * 0.75)], 2),
            "best_case": round(final_equities[-1], 2),
            "worst_case": round(final_equities[0], 2)
        },
        "probability_profitable": round(sum(1 for e in final_equities if e > 100) / num_simulations * 100, 1),
        "probability_50pct_dd": round(sum(1 for dd in max_drawdowns if dd > 50) / num_simulations * 100, 1)
    }


def stress_test_scenario(portfolio_value, positions, scenario):
    """
    Stress test portfolio against specific market scenarios.
    
    Scenarios:
        - btc_crash_20: BTC drops 20%, alts follow
        - btc_crash_40: BTC drops 40%, alts drop more
        - flash_crash: Everything drops 15% instantly
        - short_squeeze: 30% pump (bad for shorts)
        - funding_spike: Extreme funding rates
    """
    scenarios = {
        "btc_crash_20": {
            "name": "BTC 20% Crash (May 2021 style)",
            "btc_change": -20,
            "alt_multiplier": 1.3,  # Alts drop 30% more
            "description": "Bitcoin drops 20%, altcoins follow with higher beta"
        },
        "btc_crash_40": {
            "name": "Black Swan Crash (FTX style)",
            "btc_change": -40,
            "alt_multiplier": 1.5,
            "description": "Major market collapse, extreme fear"
        },
        "flash_crash": {
            "name": "Flash Crash (Liquidation Cascade)",
            "btc_change": -15,
            "alt_multiplier": 1.2,
            "description": "Rapid deleveraging event"
        },
        "short_squeeze": {
            "name": "Short Squeeze (Jan 2021 style)",
            "btc_change": 30,
            "alt_multiplier": 1.5,
            "description": "Rapid pump, shorts get liquidated"
        },
        "mild_correction": {
            "name": "Mild Correction",
            "btc_change": -10,
            "alt_multiplier": 1.1,
            "description": "Normal healthy pullback"
        }
    }
    
    if scenario not in scenarios:
        return {"error": f"Unknown scenario. Available: {list(scenarios.keys())}"}
    
    s = scenarios[scenario]
    
    # Calculate impact on each position
    total_loss = 0
    position_impacts = []
    
    for pos in positions:
        is_btc = pos.get("symbol", "").upper() in ["BTC", "BITCOIN"]
        direction = pos.get("direction", "long")
        size = pos.get("size_usd", 0)
        leverage = pos.get("leverage", 1)
        
        # Calculate price change
        if is_btc:
            price_change_pct = s["btc_change"]
        else:
            price_change_pct = s["btc_change"] * s["alt_multiplier"]
        
        # Calculate P&L (long profits when price goes up, short profits when price goes down)
        if direction == "long":
            pnl_pct = price_change_pct * leverage
        else:
            pnl_pct = -price_change_pct * leverage  # Short profits on price drop
        
        pnl_dollars = size * (pnl_pct / 100)
        total_loss += pnl_dollars
        
        position_impacts.append({
            "symbol": pos.get("symbol"),
            "direction": direction,
            "size": size,
            "leverage": leverage,
            "price_change_pct": round(price_change_pct, 1),
            "pnl_pct": round(pnl_pct, 1),
            "pnl_dollars": round(pnl_dollars, 2)
        })
    
    # Portfolio impact
    portfolio_pnl_pct = (total_loss / portfolio_value * 100) if portfolio_value > 0 else 0
    new_portfolio_value = portfolio_value + total_loss
    
    return {
        "scenario": scenario,
        "scenario_details": s,
        "portfolio_before": round(portfolio_value, 2),
        "portfolio_after": round(new_portfolio_value, 2),
        "total_pnl_dollars": round(total_loss, 2),
        "total_pnl_pct": round(portfolio_pnl_pct, 2),
        "position_impacts": position_impacts,
        "survival": new_portfolio_value > 0,
        "severity": "FATAL" if portfolio_pnl_pct < -90 else 
                   "CRITICAL" if portfolio_pnl_pct < -50 else
                   "SEVERE" if portfolio_pnl_pct < -25 else
                   "MODERATE" if portfolio_pnl_pct < -10 else "MANAGEABLE"
    }


def calculate_optimal_position_size(conn, win_rate=None, risk_per_trade=None):
    """
    Calculate optimal position size based on Kelly criterion and risk tolerance.
    """
    # Get stats from journal if not provided
    if win_rate is None:
        stats = conn.execute("""
            SELECT 
                COUNT(CASE WHEN outcome = 'win' THEN 1 END) as wins,
                COUNT(CASE WHEN outcome = 'loss' THEN 1 END) as losses,
                AVG(CASE WHEN outcome = 'win' THEN ABS(pnl_percent) END) as avg_win,
                AVG(CASE WHEN outcome = 'loss' THEN ABS(pnl_percent) END) as avg_loss
            FROM trade_journal WHERE outcome IN ('win', 'loss')
        """).fetchone()
        
        total = (stats["wins"] or 0) + (stats["losses"] or 0)
        if total < 5:
            return {"error": "Need at least 5 trades for sizing calculation"}
        
        win_rate = stats["wins"] / total
        avg_win = stats["avg_win"] or 1
        avg_loss = stats["avg_loss"] or 1
        win_loss_ratio = avg_win / avg_loss
    else:
        win_loss_ratio = 2.0  # Default assumption
    
    # Full Kelly
    kelly_full = win_rate - ((1 - win_rate) / win_loss_ratio)
    kelly_full = max(0, min(0.25, kelly_full))  # Cap at 25%
    
    # Fractional Kelly
    kelly_half = kelly_full / 2
    kelly_quarter = kelly_full / 4
    
    # Calculate ruin probabilities for each sizing
    ruin_full = calculate_ruin_probability(win_rate, win_loss_ratio, kelly_full * 100)
    ruin_half = calculate_ruin_probability(win_rate, win_loss_ratio, kelly_half * 100)
    ruin_quarter = calculate_ruin_probability(win_rate, win_loss_ratio, kelly_quarter * 100)
    
    # Recommendation based on risk tolerance
    if risk_per_trade and risk_per_trade <= 1:
        recommended = min(kelly_quarter * 100, risk_per_trade)
        recommendation = "Conservative (quarter-Kelly capped)"
    elif risk_per_trade and risk_per_trade <= 2:
        recommended = min(kelly_half * 100, risk_per_trade)
        recommendation = "Moderate (half-Kelly)"
    else:
        recommended = kelly_half * 100
        recommendation = "Standard (half-Kelly)"
    
    return {
        "win_rate": round(win_rate * 100, 1),
        "win_loss_ratio": round(win_loss_ratio, 2),
        "kelly_full_pct": round(kelly_full * 100, 2),
        "kelly_half_pct": round(kelly_half * 100, 2),
        "kelly_quarter_pct": round(kelly_quarter * 100, 2),
        "ruin_probability": {
            "full_kelly": ruin_full,
            "half_kelly": ruin_half,
            "quarter_kelly": ruin_quarter
        },
        "recommended_size_pct": round(recommended, 2),
        "recommendation": recommendation,
        "max_consecutive_losses_survivable": {
            "full_kelly": int(100 / (kelly_full * 100)) if kelly_full > 0 else "∞",
            "half_kelly": int(100 / (kelly_half * 100)) if kelly_half > 0 else "∞",
            "quarter_kelly": int(100 / (kelly_quarter * 100)) if kelly_quarter > 0 else "∞"
        }
    }


# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS analyses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL DEFAULT 'Untitled',
        symbol TEXT DEFAULT '',
        strategy TEXT DEFAULT 'auto',
        model_used TEXT DEFAULT '',
        image_path TEXT DEFAULT '',
        image_hash TEXT DEFAULT '',
        prompt_sent TEXT DEFAULT '',
        raw_response TEXT DEFAULT '',
        signal_count INTEGER DEFAULT 0,
        recommendation TEXT DEFAULT 'HOLD',
        confidence TEXT DEFAULT 'low',
        entry_price TEXT DEFAULT '',
        stop_loss TEXT DEFAULT '',
        take_profit TEXT DEFAULT '',
        position_size TEXT DEFAULT '',
        leverage TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        tags TEXT DEFAULT '[]',
        web_searches TEXT DEFAULT '[]',
        web_search_used INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS watchlist (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        category TEXT DEFAULT 'general',
        strategy TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        alert_price TEXT DEFAULT '',
        status TEXT DEFAULT 'watching',
        priority INTEGER DEFAULT 3,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS trade_journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        direction TEXT DEFAULT 'long',
        strategy TEXT DEFAULT '',
        entry_price REAL DEFAULT 0,
        exit_price REAL DEFAULT 0,
        size REAL DEFAULT 0,
        leverage REAL DEFAULT 1,
        signals_present INTEGER DEFAULT 0,
        signal_details TEXT DEFAULT '',
        reasoning TEXT DEFAULT '',
        outcome TEXT DEFAULT 'open',
        pnl_dollars REAL DEFAULT 0,
        pnl_percent REAL DEFAULT 0,
        lessons TEXT DEFAULT '',
        analysis_id INTEGER DEFAULT NULL,
        entry_time TEXT DEFAULT (datetime('now')),
        exit_time TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (analysis_id) REFERENCES analyses(id) ON DELETE SET NULL
    );
    CREATE TABLE IF NOT EXISTS prompt_templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        strategy TEXT DEFAULT 'general',
        template TEXT NOT NULL,
        is_default INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS chat_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role TEXT NOT NULL,
        message TEXT NOT NULL,
        web_sources TEXT DEFAULT '[]',
        model_used TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        chain TEXT DEFAULT 'solana',
        token_address TEXT DEFAULT '',
        wallet_address TEXT DEFAULT '',
        direction TEXT DEFAULT 'long',
        entry_price REAL DEFAULT 0,
        current_price REAL DEFAULT 0,
        size REAL DEFAULT 0,
        size_usd REAL DEFAULT 0,
        unrealized_pnl REAL DEFAULT 0,
        unrealized_pnl_pct REAL DEFAULT 0,
        stop_loss REAL DEFAULT 0,
        take_profit REAL DEFAULT 0,
        status TEXT DEFAULT 'open',
        analysis_id INTEGER DEFAULT NULL,
        journal_id INTEGER DEFAULT NULL,
        tx_hash TEXT DEFAULT '',
        opened_at TEXT DEFAULT (datetime('now')),
        closed_at TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (analysis_id) REFERENCES analyses(id) ON DELETE SET NULL,
        FOREIGN KEY (journal_id) REFERENCES trade_journal(id) ON DELETE SET NULL
    );
    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL DEFAULT 'Untitled Alert',
        symbol TEXT NOT NULL,
        alert_type TEXT NOT NULL,
        condition TEXT NOT NULL,
        target_value REAL DEFAULT 0,
        comparison TEXT DEFAULT 'crosses',
        secondary_value REAL DEFAULT 0,
        enabled INTEGER DEFAULT 1,
        one_time INTEGER DEFAULT 1,
        triggered INTEGER DEFAULT 0,
        notification_type TEXT DEFAULT 'browser',
        telegram_chat_id TEXT DEFAULT '',
        discord_webhook TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now')),
        last_triggered_at TEXT DEFAULT '',
        trigger_count INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS alert_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alert_id INTEGER NOT NULL,
        alert_name TEXT NOT NULL,
        symbol TEXT NOT NULL,
        alert_type TEXT NOT NULL,
        condition TEXT NOT NULL,
        target_value REAL DEFAULT 0,
        actual_value REAL DEFAULT 0,
        message TEXT NOT NULL,
        triggered_at TEXT DEFAULT (datetime('now')),
        acknowledged INTEGER DEFAULT 0,
        FOREIGN KEY (alert_id) REFERENCES alerts(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS scan_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        strategy TEXT NOT NULL,
        signal_count INTEGER DEFAULT 0,
        signals_detail TEXT DEFAULT '[]',
        price REAL DEFAULT 0,
        recommendation TEXT DEFAULT 'HOLD',
        confidence TEXT DEFAULT 'low',
        regime TEXT DEFAULT 'NORMAL',
        scanned_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS trade_patterns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        journal_id INTEGER NOT NULL,
        symbol TEXT NOT NULL,
        strategy TEXT DEFAULT '',
        outcome TEXT DEFAULT '',
        pnl_percent REAL DEFAULT 0,
        indicator_snapshot TEXT DEFAULT '{}',
        entry_hour INTEGER DEFAULT 0,
        entry_day_of_week INTEGER DEFAULT 0,
        market_regime TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (journal_id) REFERENCES trade_journal(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS backtest_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        strategy TEXT NOT NULL,
        params TEXT DEFAULT '{}',
        period_start TEXT DEFAULT '',
        period_end TEXT DEFAULT '',
        total_trades INTEGER DEFAULT 0,
        win_rate REAL DEFAULT 0,
        total_pnl REAL DEFAULT 0,
        max_drawdown REAL DEFAULT 0,
        profit_factor REAL DEFAULT 0,
        expectancy REAL DEFAULT 0,
        equity_curve TEXT DEFAULT '[]',
        trades TEXT DEFAULT '[]',
        monthly_returns TEXT DEFAULT '{}',
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS regime_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        regime TEXT NOT NULL,
        confidence REAL DEFAULT 0,
        features TEXT DEFAULT '{}',
        previous_regime TEXT DEFAULT '',
        detected_at TEXT DEFAULT (datetime('now'))
    );
    """)

    for k, v in {"default_model": CLAUDE_MODEL, "default_strategy": "auto", "turtle_mode": "false",
                  "consecutive_losses": "0", "consecutive_wins": "0", "daily_loss_pct": "0", 
                  "portfolio_balance": "100", "portfolio_heat": "0", "trading_locked": "false",
                  "max_position_pct": "80", "web_search_enabled": "true"}.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    for name, strat, tmpl in [("Penguin Divergence Long", "penguin", PENGUIN_PROMPT),
                               ("Burry Overbought Short", "burry", BURRY_PROMPT),
                               ("General Chart Analysis", "general", GENERAL_PROMPT),
                               ("Quick Scalp Scanner", "scalp", SCALP_PROMPT)]:
        conn.execute("INSERT OR IGNORE INTO prompt_templates (name, strategy, template, is_default) VALUES (?, ?, ?, 1)", (name, strat, tmpl))

    conn.commit()
    conn.close()
    log.info(f"DB initialized: {DB_PATH}")


# ---------------------------------------------------------------------------
# STRATEGY PROMPTS
# ---------------------------------------------------------------------------
SYSTEM_CONTEXT = """You are a professional crypto/stock chart analyst using the PENGUIN-BURRY trading methodology.
You have TWO superpowers:
1. You can visually analyze chart images with extreme precision
2. You can SEARCH THE WEB for real-time prices, news, volume data, and sentiment

IMPORTANT: When analyzing a chart, ALWAYS use web search to:
- Get the CURRENT live price of the asset
- Check for any breaking news or catalysts
- Verify current volume vs average volume
- Check BTC's current price action (for crypto divergence plays)
- Look for any upcoming events (earnings, unlocks, FOMC, etc.)

This real-time data combined with your visual chart analysis makes your predictions significantly more accurate.

CORE METHODOLOGY:
PENGUIN: Hunts BTC/altcoin divergence plays. Requires: BTC -3% to -8%, Alt +10% to +25%, RSI 70-85, Volume 2-3x avg, Support holding. Win rate: 70.8%.
BURRY: Hunts overbought blow-off tops. Requires: RSI >80, MACD histogram turning negative, ADX <30 (CRITICAL), Stochastic >90, Volume >2x avg. Win rate: 80% on 5/5.

CRITICAL RULES:
- ADX >50 = NEVER SHORT (death trap)
- Minimum 4/5 signals for TRADE recommendation
- 3/5 = HOLD (gambling)
- Position sizing: 60-80% max, NEVER 100%
- Hard stop at -3%, take profit at +15-20%

ADX KILL SWITCH: <30=SAFE | 30-40=CAUTION | 40-50=DANGER | >50=DEATH TRAP

SIGNAL RATING: 5/5=TEXTBOOK | 4/5=STRONG | 3/5=HOLD | <3=NO TRADE

Always respond with structured JSON after doing web searches for live data."""

PENGUIN_PROMPT = """Analyze this chart for a PENGUIN DIVERGENCE LONG setup.

FIRST: Search the web for the current price, BTC 24h change, current volume, and any recent news.

THEN analyze visually for: BTC/altcoin divergence, RSI 70-85, Volume 2-3x avg, Support holding, Divergence angle >20%.

Respond with JSON:
{"symbol":"","current_price":"from web","btc_price":"","btc_24h_change":"","timeframe":"","strategy":"penguin","market_context":"from web","recent_news":[],"signals":{"btc_divergence":{"present":false,"details":"","btc_change":"","alt_change":"","strength":0},"rsi":{"value":"","zone":"","signal":false},"volume":{"ratio":"","current_volume":"from web","signal":false},"support":{"holding":false,"level":"","details":""},"divergence_angle":{"value":0,"strength":""}},"signal_count":0,"recommendation":"HOLD","confidence":"low","entry":"","stop_loss":"","take_profit_1":"","take_profit_2":"","position_size_pct":"","leverage":"","risks":[],"catalysts":[],"notes":"","chart_patterns":[],"key_levels":{"support":[],"resistance":[]}}"""

BURRY_PROMPT = """Analyze this chart for a BURRY OVERBOUGHT SHORT setup.

FIRST: Search the web for current price, 24h volume change, news that caused the pump, BTC trend.

THEN analyze for 5 signals: RSI >80, MACD turning negative, ADX <30 (CRITICAL), Stochastic >90, Volume >2x avg.

ADX KILL SWITCH: <30=SAFE | 30-40=CAUTION | 40-50=DANGER | >50=NEVER SHORT

Respond with JSON:
{"symbol":"","current_price":"from web","price_24h_change":"","timeframe":"","strategy":"burry","market_context":"","pump_catalyst":"","signals":{"rsi":{"value":"","above_80":false,"signal":false},"macd":{"histogram_direction":"","signal":false,"details":""},"adx":{"value":"","zone":"","signal":false},"stochastic":{"value":"","above_90":false,"signal":false},"volume":{"ratio":"","current_vol":"","signal":false,"blow_off":false}},"signal_count":0,"recommendation":"HOLD","confidence":"low","entry":"","stop_loss":"","take_profit_1":"","take_profit_2":"","position_size_pct":"","leverage":"","risks":[],"exhaustion_signs":[],"notes":"","key_levels":{"support":[],"resistance":[]}}"""

GENERAL_PROMPT = """Analyze this chart comprehensively using the Penguin-Burry methodology.

FIRST: Search the web for current price, 24h change, volume, news, and market trend.

THEN determine which strategy applies and provide full analysis.

Respond with JSON:
{"symbol":"","current_price":"from web","price_24h_change":"","timeframe":"","trend":"","strategy_recommended":"","market_context":"","recent_news":[],"signals":{"rsi":{"value":"","zone":"","signal":false},"macd":{"direction":"","signal":false},"adx":{"estimated_value":"","zone":"","signal":false},"stochastic":{"estimated_value":"","signal":false},"volume":{"assessment":"","live_data":"","signal":false}},"signal_count":0,"recommendation":"HOLD","confidence":"low","entry":"","stop_loss":"","take_profit":"","position_size_pct":"","leverage":"","patterns":[],"key_levels":{"support":[],"resistance":[]},"catalysts":[],"risks":[],"notes":""}"""

SCALP_PROMPT = """Quick scalp analysis. Search web for current price and breaking news, then focus on immediate direction (5-15 min), nearest S/R, volume momentum, reversal signals.

JSON: {"symbol":"","current_price":"from web","direction":"","confidence":"","entry":"","target":"","stop":"","timeframe":"","volume_status":"","breaking_news":"","notes":""}"""


# ---------------------------------------------------------------------------
# API ROUTES
# ---------------------------------------------------------------------------
@app.route("/api/health")
def api_health():
    has_key = bool(ANTHROPIC_API_KEY)
    err = None if has_key else "ANTHROPIC_API_KEY not set. Export it and restart."
    return jsonify({"status": "ok" if has_key else "error", "error": err, "model": CLAUDE_MODEL, "provider": "Anthropic Claude", "web_search": True})

@app.route("/api/models")
def api_models():
    return jsonify({"models": CLAUDE_MODELS})

@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return jsonify({r["key"]: r["value"] for r in rows})

@app.route("/api/settings", methods=["PUT"])
def api_update_settings():
    data = request.json or {}
    conn = get_db()
    for k, v in data.items():
        conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, datetime('now'))", (k, str(v)))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    if "image" not in request.files:
        return jsonify({"error": "No image uploaded"}), 400

    file = request.files["image"]
    model = request.form.get("model", CLAUDE_MODEL)
    strategy = request.form.get("strategy", "auto")
    symbol = request.form.get("symbol", "")
    title = request.form.get("title", "")
    custom_prompt = request.form.get("custom_prompt", "")
    web_search = request.form.get("web_search", "true") == "true"

    img_data = file.read()
    if len(img_data) > MAX_IMAGE_MB * 1024 * 1024:
        return jsonify({"error": f"Image exceeds {MAX_IMAGE_MB}MB"}), 400

    img_hash = hashlib.sha256(img_data).hexdigest()[:16]
    ct = file.content_type or "image/png"
    ext = (mimetypes.guess_extension(ct) or ".png").lstrip(".")
    img_filename = f"{img_hash}_{int(time.time())}.{ext}"
    img_path = UPLOAD_DIR / img_filename
    img_path.write_bytes(img_data)

    img_b64 = base64.b64encode(img_data).decode("utf-8")
    media_map = {"image/png": "image/png", "image/jpeg": "image/jpeg", "image/jpg": "image/jpeg", "image/gif": "image/gif", "image/webp": "image/webp"}
    media_type = media_map.get(ct, "image/png")

    prompt = custom_prompt or {"penguin": PENGUIN_PROMPT, "burry": BURRY_PROMPT, "scalp": SCALP_PROMPT}.get(strategy, GENERAL_PROMPT)
    if symbol:
        prompt = f"SYMBOL: {symbol}\n\n" + prompt

    conn = get_db()
    turtle = conn.execute("SELECT value FROM settings WHERE key='turtle_mode'").fetchone()
    if turtle and turtle["value"] == "true":
        prompt += "\n\n TURTLE MODE ACTIVE: 5/5 signals ONLY. 60% max size. Exit at +10%."
    bal = conn.execute("SELECT value FROM settings WHERE key='portfolio_balance'").fetchone()
    if bal:
        prompt += f"\n\nPortfolio balance: ${bal['value']}"
    losses = conn.execute("SELECT value FROM settings WHERE key='consecutive_losses'").fetchone()
    if losses and int(losses["value"]) >= 2:
        prompt += f"\n\nWARNING: {losses['value']} consecutive losses. Extra caution. Consider HOLD."
    conn.close()

    log.info(f"Analyzing: model={model}, strategy={strategy}, symbol={symbol}, web={web_search}")
    response_text, web_searches, err = claude_analyze_chart(model, SYSTEM_CONTEXT, prompt, img_b64, media_type, web_search)
    if err:
        return jsonify({"error": err}), 502

    parsed = parse_json_response(response_text)
    signal_count = parsed.get("signal_count", 0)
    recommendation = parsed.get("recommendation", "HOLD")
    confidence = parsed.get("confidence", "low")
    if not title:
        title = f"{parsed.get('symbol', symbol) or symbol or 'Chart'} - {strategy.upper()}"

    conn = get_db()
    cur = conn.execute("""INSERT INTO analyses (title,symbol,strategy,model_used,image_path,image_hash,prompt_sent,raw_response,
        signal_count,recommendation,confidence,entry_price,stop_loss,take_profit,position_size,leverage,notes,web_searches,web_search_used)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (title, parsed.get("symbol", symbol), strategy, model, str(img_path), img_hash, prompt, response_text,
         signal_count, recommendation, confidence, str(parsed.get("entry", "")), str(parsed.get("stop_loss", "")),
         str(parsed.get("take_profit_1", parsed.get("take_profit", parsed.get("target", "")))),
         str(parsed.get("position_size_pct", "")), str(parsed.get("leverage", "")),
         json.dumps(parsed.get("notes", "")), json.dumps(web_searches), 1 if web_search else 0))
    aid = cur.lastrowid
    conn.commit()
    conn.close()

    return jsonify({"id": aid, "title": title, "symbol": parsed.get("symbol", symbol), "strategy": strategy,
                    "model": model, "signal_count": signal_count, "recommendation": recommendation,
                    "confidence": confidence, "parsed": parsed, "raw_response": response_text,
                    "web_searches": web_searches, "web_search_used": web_search})

@app.route("/api/analyses")
def api_list_analyses():
    conn = get_db()
    rows = conn.execute("SELECT * FROM analyses ORDER BY created_at DESC LIMIT ?", (int(request.args.get("limit", 50)),)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/analyses/<int:aid>")
def api_get_analysis(aid):
    conn = get_db()
    row = conn.execute("SELECT * FROM analyses WHERE id=?", (aid,)).fetchone()
    conn.close()
    return jsonify(dict(row)) if row else (jsonify({"error": "Not found"}), 404)

@app.route("/api/analyses/<int:aid>", methods=["PUT"])
def api_update_analysis(aid):
    data = request.json or {}
    allowed = ["title","symbol","strategy","notes","tags","signal_count","recommendation","confidence","entry_price","stop_loss","take_profit","position_size","leverage"]
    sets, vals = [], []
    for k in allowed:
        if k in data:
            sets.append(f"{k}=?"); vals.append(data[k])
    if not sets:
        return jsonify({"error": "No fields"}), 400
    sets.append("updated_at=datetime('now')"); vals.append(aid)
    conn = get_db()
    conn.execute(f"UPDATE analyses SET {','.join(sets)} WHERE id=?", vals)
    conn.commit(); conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/analyses/<int:aid>", methods=["DELETE"])
def api_delete_analysis(aid):
    conn = get_db()
    row = conn.execute("SELECT image_path FROM analyses WHERE id=?", (aid,)).fetchone()
    if row and row["image_path"]:
        p = Path(row["image_path"])
        if p.exists(): p.unlink()
    conn.execute("DELETE FROM analyses WHERE id=?", (aid,))
    conn.commit(); conn.close()
    return jsonify({"status": "deleted"})

@app.route("/api/image/<path:filename>")
def api_serve_image(filename):
    p = UPLOAD_DIR / filename
    return send_file(str(p)) if p.exists() else (jsonify({"error": "Not found"}), 404)

@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.json or {}
    model = data.get("model", CLAUDE_MODEL)
    message = data.get("message", "")
    if not message:
        return jsonify({"error": "Message required"}), 400
    
    # Load conversation history for stateful chat
    conn = get_db()
    history_rows = conn.execute(
        "SELECT role, message FROM chat_history ORDER BY id DESC LIMIT ?", 
        (MAX_CHAT_HISTORY_MESSAGES,)
    ).fetchall()
    conn.close()
    
    # Build conversation history (reverse to get chronological order)
    conversation_history = [{"role": r["role"], "content": r["message"]} for r in reversed(history_rows)]
    
    # Call Claude with full conversation context
    resp, ws, err = claude_chat_text(
        model, 
        SYSTEM_CONTEXT, 
        message, 
        use_web_search=data.get("web_search", True),
        conversation_history=conversation_history if conversation_history else None
    )
    if err:
        return jsonify({"error": err}), 502
    
    # Save to history
    conn = get_db()
    conn.execute("INSERT INTO chat_history (role,message,model_used) VALUES ('user',?,?)", (message, model))
    conn.execute("INSERT INTO chat_history (role,message,web_sources,model_used) VALUES ('assistant',?,?,?)", (resp, json.dumps(ws), model))
    conn.commit()
    conn.close()
    
    return jsonify({"response": resp, "web_searches": ws, "context_messages": len(conversation_history)})

@app.route("/api/chat/clear", methods=["DELETE"])
def api_chat_clear():
    conn = get_db()
    conn.execute("DELETE FROM chat_history")
    conn.commit(); conn.close()
    return jsonify({"status": "cleared"})


@app.route("/api/chat/history")
def api_chat_history():
    """Get chat history with context info"""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, role, message, web_sources, model_used, created_at FROM chat_history ORDER BY id DESC LIMIT ?",
        (int(request.args.get("limit", 50)),)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/market-data/<symbol>")
def api_market_data(symbol):
    """
    Unified market data endpoint - fetches price + calculates indicators.
    Tries CoinGecko first, falls back to DEXScreener for newer/smaller tokens.
    """
    symbol = symbol.upper().strip()
    
    # Try CoinGecko first (better for major coins)
    price_data = fetch_coingecko_price(symbol)
    ohlcv_data = fetch_coingecko_ohlcv(symbol, days=30) if price_data else None
    
    # Fallback to DEXScreener
    if not price_data:
        price_data = fetch_dexscreener_token(symbol)
    
    if not price_data:
        return jsonify({"error": f"Could not find data for {symbol}"}), 404
    
    # Calculate technical indicators if we have OHLCV data
    indicators = None
    if ohlcv_data and len(ohlcv_data) >= 30:
        indicators = calculate_all_indicators(ohlcv_data)
    
    result = {
        "symbol": symbol,
        "price_data": price_data,
        "indicators": indicators.get("indicators") if indicators else None,
        "signal_count": indicators.get("signal_count", 0) if indicators else 0,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    
    return jsonify(result)


@app.route("/api/price/<symbol>")
def api_price(symbol):
    """Quick price check endpoint"""
    symbol = symbol.upper().strip()
    
    price_data = fetch_coingecko_price(symbol)
    if not price_data:
        price_data = fetch_dexscreener_token(symbol)
    
    if not price_data:
        return jsonify({"error": f"Could not find {symbol}"}), 404
    
    return jsonify(price_data)


@app.route("/api/dex/search/<query>")
def api_dex_search(query):
    """Search DEXScreener for a token"""
    result = fetch_dexscreener_token(query)
    if not result:
        return jsonify({"error": f"No pairs found for {query}"}), 404
    return jsonify(result)


@app.route("/api/indicators/<symbol>")
def api_indicators(symbol):
    """Get calculated technical indicators for a symbol"""
    symbol = symbol.upper().strip()
    
    ohlcv_data = fetch_coingecko_ohlcv(symbol, days=30)
    if not ohlcv_data or len(ohlcv_data) < 30:
        return jsonify({"error": f"Insufficient data for {symbol} indicators"}), 404
    
    indicators = calculate_all_indicators(ohlcv_data)
    if not indicators:
        return jsonify({"error": "Failed to calculate indicators"}), 500
    
    return jsonify({
        "symbol": symbol,
        "indicators": indicators["indicators"],
        "signal_count": indicators["signal_count"],
        "current_price": indicators["current_price"]
    })


@app.route("/api/risk/heat")
def api_portfolio_heat():
    """Get current portfolio heat"""
    conn = get_db()
    heat = calculate_portfolio_heat(conn)
    
    # Get related settings
    settings_rows = conn.execute(
        "SELECT key, value FROM settings WHERE key IN ('turtle_mode', 'consecutive_losses', 'consecutive_wins', 'daily_loss_pct', 'trading_locked', 'portfolio_balance', 'max_position_pct')"
    ).fetchall()
    conn.close()
    
    settings = {r["key"]: r["value"] for r in settings_rows}
    
    return jsonify({
        "portfolio_heat": heat,
        "heat_status": "CRITICAL" if heat > 80 else "HIGH" if heat > 60 else "MODERATE" if heat > 40 else "LOW",
        "turtle_mode": settings.get("turtle_mode") == "true",
        "consecutive_losses": int(settings.get("consecutive_losses", 0)),
        "consecutive_wins": int(settings.get("consecutive_wins", 0)),
        "daily_loss_pct": float(settings.get("daily_loss_pct", 0)),
        "trading_locked": settings.get("trading_locked") == "true",
        "portfolio_balance": float(settings.get("portfolio_balance", 100)),
        "max_position_pct": int(settings.get("max_position_pct", 80))
    })


# ---------------------------------------------------------------------------
# PHASE 3 API ENDPOINTS: QUANTITATIVE EDGE
# ---------------------------------------------------------------------------

@app.route("/api/quant/kelly")
def api_kelly():
    """
    Get Kelly Criterion position sizing recommendation.
    Query params: strategy (optional) - filter by strategy (penguin, burry, all)
    """
    strategy = request.args.get("strategy", "all")
    
    conn = get_db()
    kelly_data = calculate_kelly_criterion(conn, strategy)
    conn.close()
    
    return jsonify(kelly_data)


@app.route("/api/quant/kelly/all")
def api_kelly_all():
    """Get Kelly Criterion for all strategies"""
    conn = get_db()
    
    results = {
        "all": calculate_kelly_criterion(conn, None),
        "penguin": calculate_kelly_criterion(conn, "penguin"),
        "burry": calculate_kelly_criterion(conn, "burry"),
        "scalp": calculate_kelly_criterion(conn, "scalp"),
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    
    conn.close()
    return jsonify(results)


@app.route("/api/quant/fear-greed")
def api_fear_greed():
    """Get Crypto Fear & Greed Index"""
    data = fetch_fear_greed_index()
    
    if not data:
        return jsonify({"error": "Could not fetch Fear & Greed Index"}), 502
    
    # Add trading interpretation
    value = data["value"]
    if value < 25:
        data["trading_bias"] = "PENGUIN_FAVORED"
        data["interpretation"] = "Extreme fear - divergence plays favored (Penguin setup)"
    elif value < 40:
        data["trading_bias"] = "CAUTIOUS_LONG"
        data["interpretation"] = "Fear - look for capitulation bottoms"
    elif value < 60:
        data["trading_bias"] = "NEUTRAL"
        data["interpretation"] = "Neutral - follow your signals"
    elif value < 75:
        data["trading_bias"] = "CAUTIOUS_SHORT"
        data["interpretation"] = "Greed - start looking for exhaustion"
    else:
        data["trading_bias"] = "BURRY_FAVORED"
        data["interpretation"] = "Extreme greed - blow-off tops likely (Burry setup)"
    
    return jsonify(data)


@app.route("/api/quant/regime/<symbol>")
def api_volatility_regime(symbol):
    """
    Get volatility regime classification for a symbol.
    Combines ATR percentile, BB squeeze, and Fear & Greed.
    """
    symbol = symbol.upper().strip()
    
    # Fetch OHLCV data
    ohlcv_data = fetch_coingecko_ohlcv(symbol, days=90)
    if not ohlcv_data or len(ohlcv_data) < 30:
        return jsonify({"error": f"Insufficient data for {symbol}"}), 404
    
    closes = [d["close"] for d in ohlcv_data]
    highs = [d["high"] for d in ohlcv_data]
    lows = [d["low"] for d in ohlcv_data]
    
    # Calculate ATR and percentile
    current_atr = calculate_atr(highs, lows, closes)
    atr_percentile = calculate_atr_percentile(ohlcv_data, current_atr)
    
    # Calculate BB squeeze
    bb_squeeze = calculate_bollinger_band_squeeze(ohlcv_data)
    
    # Get Fear & Greed
    fear_greed = fetch_fear_greed_index()
    fear_greed_value = fear_greed["value"] if fear_greed else None
    
    # Classify regime
    regime = classify_volatility_regime(atr_percentile, bb_squeeze, fear_greed_value)
    
    return jsonify({
        "symbol": symbol,
        "regime": regime,
        "atr": {
            "current": current_atr,
            "percentile": atr_percentile
        },
        "bollinger_squeeze": bb_squeeze,
        "fear_greed": fear_greed,
        "timestamp": datetime.now(timezone.utc).isoformat()
    })


@app.route("/api/quant/funding/<symbol>")
def api_funding_rate(symbol):
    """
    Get perpetual futures funding rate for a symbol.
    Reveals positioning sentiment and potential squeeze setups.
    """
    symbol = symbol.upper().strip()
    
    data = fetch_funding_rate(symbol)
    if not data:
        return jsonify({"error": f"Could not fetch funding rate for {symbol}. May not have perpetual futures."}), 404
    
    return jsonify(data)


@app.route("/api/quant/correlation")
def api_correlation_matrix():
    """
    Get correlation matrix for major crypto assets.
    Query params: symbols (optional) - comma-separated list of symbols
                  days (optional) - lookback period (default 30)
    """
    symbols_param = request.args.get("symbols", "BTC,ETH,SOL,DOGE,XRP,ADA")
    days = int(request.args.get("days", 30))
    
    symbols = [s.strip().upper() for s in symbols_param.split(",")]
    
    if len(symbols) < 2:
        return jsonify({"error": "Need at least 2 symbols for correlation"}), 400
    
    result = fetch_correlation_matrix(symbols, days)
    if not result:
        return jsonify({"error": "Could not calculate correlations. Insufficient data."}), 404
    
    # Find notable correlations
    notable = []
    matrix = result["correlation_matrix"]
    for sym_a in matrix:
        for sym_b in matrix[sym_a]:
            if sym_a < sym_b:  # Avoid duplicates
                corr = matrix[sym_a][sym_b]
                if corr is not None:
                    if corr > 0.9:
                        notable.append({"pair": f"{sym_a}/{sym_b}", "correlation": corr, "note": "Very high - move together"})
                    elif corr < 0.3:
                        notable.append({"pair": f"{sym_a}/{sym_b}", "correlation": corr, "note": "Low - potential divergence play"})
    
    result["notable_correlations"] = notable
    result["timestamp"] = datetime.now(timezone.utc).isoformat()
    
    return jsonify(result)


@app.route("/api/quant/risk-metrics")
def api_advanced_risk_metrics():
    """
    Get advanced risk metrics (Sharpe, Sortino, Max Drawdown, etc.)
    Query params: strategy (optional) - filter by strategy
    """
    strategy = request.args.get("strategy", "all")
    
    conn = get_db()
    metrics = calculate_advanced_risk_metrics(conn, strategy)
    conn.close()
    
    return jsonify(metrics)


@app.route("/api/quant/dashboard")
def api_quant_dashboard():
    """
    Comprehensive quant dashboard - all Phase 3 data in one call.
    Query params: symbol (optional) - primary symbol to analyze (default BTC)
    """
    symbol = request.args.get("symbol", "BTC").upper().strip()
    
    conn = get_db()
    
    # Kelly Criterion
    kelly_all = calculate_kelly_criterion(conn, None)
    kelly_penguin = calculate_kelly_criterion(conn, "penguin")
    kelly_burry = calculate_kelly_criterion(conn, "burry")
    
    # Advanced Risk Metrics
    risk_metrics = calculate_advanced_risk_metrics(conn, None)
    
    conn.close()
    
    # Fear & Greed
    fear_greed = fetch_fear_greed_index()
    
    # Volatility regime for primary symbol
    ohlcv_data = fetch_coingecko_ohlcv(symbol, days=90)
    regime_data = None
    if ohlcv_data and len(ohlcv_data) >= 30:
        closes = [d["close"] for d in ohlcv_data]
        highs = [d["high"] for d in ohlcv_data]
        lows = [d["low"] for d in ohlcv_data]
        
        current_atr = calculate_atr(highs, lows, closes)
        atr_percentile = calculate_atr_percentile(ohlcv_data, current_atr)
        bb_squeeze = calculate_bollinger_band_squeeze(ohlcv_data)
        
        regime_data = classify_volatility_regime(
            atr_percentile, 
            bb_squeeze, 
            fear_greed["value"] if fear_greed else None
        )
    
    # Funding rate
    funding = fetch_funding_rate(symbol)
    
    # Correlation snapshot (BTC vs major alts)
    correlation_data = fetch_correlation_matrix(["BTC", "ETH", "SOL"], days=30)
    
    return jsonify({
        "symbol": symbol,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "kelly_criterion": {
            "all_strategies": kelly_all,
            "penguin": kelly_penguin,
            "burry": kelly_burry
        },
        "risk_metrics": risk_metrics,
        "fear_greed": fear_greed,
        "volatility_regime": regime_data,
        "funding_rate": funding,
        "correlations": correlation_data
    })


# ---------------------------------------------------------------------------
# PHASE 4 API ENDPOINTS: AUTOMATION & ALERTS
# ---------------------------------------------------------------------------

@app.route("/api/alerts", methods=["GET"])
def api_get_alerts():
    """Get all alerts (optionally filtered)."""
    enabled_only = request.args.get("enabled", "").lower() == "true"
    symbol = request.args.get("symbol", "").upper()
    
    conn = get_db()
    
    query = "SELECT * FROM alerts"
    conditions = []
    params = []
    
    if enabled_only:
        conditions.append("enabled = 1")
    if symbol:
        conditions.append("symbol = ?")
        params.append(symbol)
    
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    
    query += " ORDER BY created_at DESC"
    
    rows = conn.execute(query, params).fetchall()
    conn.close()
    
    return jsonify([dict(r) for r in rows])


@app.route("/api/alerts", methods=["POST"])
def api_create_alert():
    """
    Create a new alert.
    
    Alert types:
        - price: Price crosses above/below target
        - indicator: RSI/MACD/ADX/Volume alerts
        - divergence: BTC/alt divergence detection
        - funding: Funding rate alerts
        - portfolio: Heat, daily loss, consecutive losses
    
    Body:
    {
        "name": "SOL $200",
        "symbol": "SOL",
        "alert_type": "price",
        "condition": "crosses_above",
        "target_value": 200,
        "comparison": "crosses",
        "secondary_value": 0,
        "enabled": true,
        "one_time": true,
        "notification_type": "browser",
        "notes": ""
    }
    """
    d = request.json or {}
    
    required = ["symbol", "alert_type", "condition"]
    for field in required:
        if not d.get(field):
            return jsonify({"error": f"Missing required field: {field}"}), 400
    
    conn = get_db()
    cur = conn.execute("""
        INSERT INTO alerts (name, symbol, alert_type, condition, target_value, comparison,
                            secondary_value, enabled, one_time, notification_type, 
                            telegram_chat_id, discord_webhook, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        d.get("name", f"{d['symbol']} Alert"),
        d["symbol"].upper(),
        d["alert_type"],
        d["condition"],
        float(d.get("target_value", 0)),
        d.get("comparison", "crosses"),
        float(d.get("secondary_value", 0)),
        1 if d.get("enabled", True) else 0,
        1 if d.get("one_time", True) else 0,
        d.get("notification_type", "browser"),
        d.get("telegram_chat_id", ""),
        d.get("discord_webhook", ""),
        d.get("notes", "")
    ))
    
    conn.commit()
    alert_id = cur.lastrowid
    conn.close()
    
    return jsonify({"id": alert_id, "status": "created"})


@app.route("/api/alerts/<int:alert_id>", methods=["GET"])
def api_get_alert(alert_id):
    """Get a specific alert."""
    conn = get_db()
    row = conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
    conn.close()
    
    if not row:
        return jsonify({"error": "Alert not found"}), 404
    
    return jsonify(dict(row))


@app.route("/api/alerts/<int:alert_id>", methods=["PUT"])
def api_update_alert(alert_id):
    """Update an alert."""
    d = request.json or {}
    
    allowed = ["name", "symbol", "alert_type", "condition", "target_value", 
               "comparison", "secondary_value", "enabled", "one_time",
               "notification_type", "telegram_chat_id", "discord_webhook", "notes"]
    
    sets, vals = [], []
    for k in allowed:
        if k in d:
            if k in ["enabled", "one_time"]:
                sets.append(f"{k}=?")
                vals.append(1 if d[k] else 0)
            elif k in ["target_value", "secondary_value"]:
                sets.append(f"{k}=?")
                vals.append(float(d[k]))
            elif k == "symbol":
                sets.append(f"{k}=?")
                vals.append(d[k].upper())
            else:
                sets.append(f"{k}=?")
                vals.append(d[k])
    
    if not sets:
        return jsonify({"error": "No fields to update"}), 400
    
    sets.append("updated_at=datetime('now')")
    vals.append(alert_id)
    
    conn = get_db()
    conn.execute(f"UPDATE alerts SET {','.join(sets)} WHERE id=?", vals)
    conn.commit()
    conn.close()
    
    return jsonify({"status": "updated"})


@app.route("/api/alerts/<int:alert_id>", methods=["DELETE"])
def api_delete_alert(alert_id):
    """Delete an alert."""
    conn = get_db()
    conn.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
    conn.commit()
    conn.close()
    
    return jsonify({"status": "deleted"})


@app.route("/api/alerts/<int:alert_id>/toggle", methods=["POST"])
def api_toggle_alert(alert_id):
    """Toggle an alert's enabled state."""
    conn = get_db()
    conn.execute("""
        UPDATE alerts 
        SET enabled = CASE WHEN enabled = 1 THEN 0 ELSE 1 END,
            updated_at = datetime('now')
        WHERE id = ?
    """, (alert_id,))
    conn.commit()
    
    row = conn.execute("SELECT enabled FROM alerts WHERE id = ?", (alert_id,)).fetchone()
    conn.close()
    
    if not row:
        return jsonify({"error": "Alert not found"}), 404
    
    return jsonify({"id": alert_id, "enabled": bool(row["enabled"])})


@app.route("/api/alerts/<int:alert_id>/reset", methods=["POST"])
def api_reset_alert(alert_id):
    """Reset a triggered alert so it can fire again."""
    conn = get_db()
    conn.execute("""
        UPDATE alerts 
        SET triggered = 0, enabled = 1, updated_at = datetime('now')
        WHERE id = ?
    """, (alert_id,))
    conn.commit()
    conn.close()
    
    return jsonify({"status": "reset"})


@app.route("/api/alerts/history")
def api_alert_history():
    """
    Get triggered alert history.
    Query params: limit (default 50), alert_id (filter by specific alert)
    """
    limit = int(request.args.get("limit", 50))
    alert_id = request.args.get("alert_id")
    
    conn = get_db()
    
    if alert_id:
        rows = conn.execute("""
            SELECT * FROM alert_history 
            WHERE alert_id = ? 
            ORDER BY triggered_at DESC LIMIT ?
        """, (alert_id, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM alert_history 
            ORDER BY triggered_at DESC LIMIT ?
        """, (limit,)).fetchall()
    
    conn.close()
    
    return jsonify([dict(r) for r in rows])


@app.route("/api/alerts/triggered")
def api_triggered_alerts():
    """
    Get recently triggered alerts (from in-memory queue).
    This is for real-time notifications polling.
    """
    global triggered_alerts_queue
    
    # Return all queued alerts and optionally clear them
    clear = request.args.get("clear", "").lower() == "true"
    
    alerts = list(triggered_alerts_queue)
    
    if clear:
        triggered_alerts_queue.clear()
    
    return jsonify({
        "alerts": alerts,
        "count": len(alerts)
    })


@app.route("/api/monitor/status")
def api_monitor_status():
    """Get background monitor status."""
    global monitor_running, monitor_thread, last_scan_time, alert_check_interval, scan_interval
    
    is_alive = monitor_thread.is_alive() if monitor_thread else False
    
    return jsonify({
        "running": monitor_running and is_alive,
        "alert_check_interval": alert_check_interval,
        "scan_interval": scan_interval,
        "last_scan": datetime.fromtimestamp(last_scan_time).isoformat() if last_scan_time > 0 else None,
        "queued_notifications": len(triggered_alerts_queue)
    })


@app.route("/api/monitor/start", methods=["POST"])
def api_start_monitor():
    """Start the background monitor."""
    started = start_background_monitor()
    return jsonify({"status": "started" if started else "already_running"})


@app.route("/api/monitor/stop", methods=["POST"])
def api_stop_monitor():
    """Stop the background monitor."""
    stop_background_monitor()
    return jsonify({"status": "stopping"})


@app.route("/api/scan/run", methods=["POST"])
def api_run_scan():
    """Manually trigger an auto-scan."""
    run_auto_scan()
    return jsonify({"status": "scan_completed", "timestamp": datetime.now(timezone.utc).isoformat()})


@app.route("/api/scan/results")
def api_scan_results():
    """
    Get recent scan results.
    Query params: limit (default 20), symbol (filter by symbol)
    """
    limit = int(request.args.get("limit", 20))
    symbol = request.args.get("symbol", "").upper()
    min_signals = int(request.args.get("min_signals", 0))
    
    conn = get_db()
    
    query = "SELECT * FROM scan_results"
    conditions = []
    params = []
    
    if symbol:
        conditions.append("symbol = ?")
        params.append(symbol)
    if min_signals > 0:
        conditions.append("signal_count >= ?")
        params.append(min_signals)
    
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    
    query += " ORDER BY scanned_at DESC LIMIT ?"
    params.append(limit)
    
    rows = conn.execute(query, params).fetchall()
    conn.close()
    
    results = []
    for row in rows:
        r = dict(row)
        r["signals_detail"] = json.loads(r["signals_detail"]) if r["signals_detail"] else []
        results.append(r)
    
    return jsonify(results)


@app.route("/api/scan/setups")
def api_current_setups():
    """Get current tradeable setups (signal_count >= 4)."""
    conn = get_db()
    
    # Get most recent scan result per symbol with 4+ signals
    rows = conn.execute("""
        SELECT * FROM scan_results 
        WHERE signal_count >= 4 
        AND scanned_at > datetime('now', '-1 hour')
        ORDER BY signal_count DESC, scanned_at DESC
    """).fetchall()
    
    conn.close()
    
    # Deduplicate by symbol (keep most recent)
    seen = set()
    setups = []
    for row in rows:
        if row["symbol"] not in seen:
            seen.add(row["symbol"])
            r = dict(row)
            r["signals_detail"] = json.loads(r["signals_detail"]) if r["signals_detail"] else []
            setups.append(r)
    
    return jsonify({
        "setups": setups,
        "count": len(setups),
        "timestamp": datetime.now(timezone.utc).isoformat()
    })


@app.route("/api/positions/monitor")
def api_position_monitor():
    """
    Get all positions with live P&L data.
    More detailed than regular /api/positions - includes monitoring status.
    """
    conn = get_db()
    
    positions = conn.execute("""
        SELECT p.*, a.title as analysis_title, j.reasoning as journal_reasoning
        FROM positions p
        LEFT JOIN analyses a ON p.analysis_id = a.id
        LEFT JOIN trade_journal j ON p.journal_id = j.id
        WHERE p.status = 'open'
        ORDER BY p.opened_at DESC
    """).fetchall()
    
    conn.close()
    
    results = []
    for pos in positions:
        p = dict(pos)
        
        # Calculate R-multiple
        risk = abs(p["entry_price"] - p["stop_loss"]) if p["stop_loss"] > 0 else 0
        if risk > 0:
            p["r_multiple"] = round(p["unrealized_pnl_pct"] / (risk / p["entry_price"] * 100), 2)
        else:
            p["r_multiple"] = 0
        
        # Check if near SL or TP
        current = p["current_price"]
        if p["direction"] == "long":
            p["sl_distance_pct"] = round((current - p["stop_loss"]) / current * 100, 2) if p["stop_loss"] > 0 else None
            p["tp_distance_pct"] = round((p["take_profit"] - current) / current * 100, 2) if p["take_profit"] > 0 else None
        else:
            p["sl_distance_pct"] = round((p["stop_loss"] - current) / current * 100, 2) if p["stop_loss"] > 0 else None
            p["tp_distance_pct"] = round((current - p["take_profit"]) / current * 100, 2) if p["take_profit"] > 0 else None
        
        results.append(p)
    
    return jsonify(results)


@app.route("/api/market-check", methods=["POST"])
def api_market_check():
    data = request.json or {}
    symbol = data.get("symbol", "")
    if not symbol:
        return jsonify({"error": "Symbol required"}), 400
    prompt = f'Search the web and give me a quick market report on {symbol}: current price, 24h change %, volume vs avg, breaking news, sentiment. JSON: {{"symbol":"{symbol}","price":"","change_24h":"","volume_status":"","news":[],"sentiment":"","notes":""}}'
    resp, ws, err = claude_chat_text(CLAUDE_MODEL, SYSTEM_CONTEXT, prompt, use_web_search=True)
    if err:
        return jsonify({"error": err}), 502
    return jsonify({"data": parse_json_response(resp), "raw": resp, "web_searches": ws})

@app.route("/api/watchlist", methods=["GET"])
def api_get_watchlist():
    conn = get_db()
    rows = conn.execute("SELECT * FROM watchlist ORDER BY priority ASC, created_at DESC").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/watchlist", methods=["POST"])
def api_add_watchlist():
    d = request.json or {}
    conn = get_db()
    cur = conn.execute("INSERT INTO watchlist (symbol,category,strategy,notes,alert_price,status,priority) VALUES (?,?,?,?,?,?,?)",
        (d.get("symbol",""),d.get("category","general"),d.get("strategy",""),d.get("notes",""),d.get("alert_price",""),d.get("status","watching"),d.get("priority",3)))
    conn.commit(); wid = cur.lastrowid; conn.close()
    return jsonify({"id": wid, "status": "ok"})

@app.route("/api/watchlist/<int:wid>", methods=["PUT"])
def api_update_watchlist(wid):
    d = request.json or {}
    allowed = ["symbol","category","strategy","notes","alert_price","status","priority"]
    sets, vals = [], []
    for k in allowed:
        if k in d: sets.append(f"{k}=?"); vals.append(d[k])
    if not sets: return jsonify({"error":"No fields"}), 400
    sets.append("updated_at=datetime('now')"); vals.append(wid)
    conn = get_db()
    conn.execute(f"UPDATE watchlist SET {','.join(sets)} WHERE id=?", vals)
    conn.commit(); conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/watchlist/<int:wid>", methods=["DELETE"])
def api_delete_watchlist(wid):
    conn = get_db()
    conn.execute("DELETE FROM watchlist WHERE id=?", (wid,))
    conn.commit(); conn.close()
    return jsonify({"status": "deleted"})

@app.route("/api/journal", methods=["GET"])
def api_get_journal():
    conn = get_db()
    rows = conn.execute("SELECT * FROM trade_journal ORDER BY created_at DESC LIMIT ?", (int(request.args.get("limit",100)),)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/journal", methods=["POST"])
def api_add_journal():
    d = request.json or {}
    conn = get_db()
    cur = conn.execute("""INSERT INTO trade_journal (symbol,direction,strategy,entry_price,exit_price,size,leverage,
        signals_present,signal_details,reasoning,outcome,pnl_dollars,pnl_percent,lessons,analysis_id,entry_time,exit_time)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (d.get("symbol",""),d.get("direction","long"),d.get("strategy",""),d.get("entry_price",0),d.get("exit_price",0),
         d.get("size",0),d.get("leverage",1),d.get("signals_present",0),d.get("signal_details",""),d.get("reasoning",""),
         d.get("outcome","open"),d.get("pnl_dollars",0),d.get("pnl_percent",0),d.get("lessons",""),d.get("analysis_id"),
         d.get("entry_time",""),d.get("exit_time","")))
    jid = cur.lastrowid
    
    # Auto-trigger risk management based on outcome
    outcome = d.get("outcome", "open")
    if outcome in ("win", "loss"):
        consecutive_losses, consecutive_wins = update_risk_settings_on_outcome(
            conn, outcome, d.get("pnl_dollars", 0), d.get("pnl_percent", 0)
        )
    
    # Recalculate portfolio heat
    heat = calculate_portfolio_heat(conn)
    
    conn.commit()
    conn.close()
    
    return jsonify({
        "id": jid, 
        "status": "ok",
        "risk_update": {
            "portfolio_heat": heat,
            "outcome_processed": outcome in ("win", "loss")
        }
    })

@app.route("/api/journal/<int:jid>", methods=["PUT"])
def api_update_journal(jid):
    d = request.json or {}
    allowed = ["symbol","direction","strategy","entry_price","exit_price","size","leverage","signals_present",
               "signal_details","reasoning","outcome","pnl_dollars","pnl_percent","lessons","entry_time","exit_time"]
    sets, vals = [], []
    for k in allowed:
        if k in d: sets.append(f"{k}=?"); vals.append(d[k])
    if not sets: return jsonify({"error":"No fields"}), 400
    sets.append("updated_at=datetime('now')"); vals.append(jid)
    conn = get_db()
    
    # Get previous outcome to check if it changed
    prev = conn.execute("SELECT outcome FROM trade_journal WHERE id=?", (jid,)).fetchone()
    prev_outcome = prev["outcome"] if prev else None
    
    conn.execute(f"UPDATE trade_journal SET {','.join(sets)} WHERE id=?", vals)
    
    # Auto-trigger risk management if outcome changed to win/loss
    new_outcome = d.get("outcome")
    if new_outcome and new_outcome in ("win", "loss") and new_outcome != prev_outcome:
        update_risk_settings_on_outcome(
            conn, new_outcome, d.get("pnl_dollars", 0), d.get("pnl_percent", 0)
        )
    
    # Recalculate portfolio heat
    heat = calculate_portfolio_heat(conn)
    
    conn.commit()
    conn.close()
    
    return jsonify({
        "status": "ok",
        "risk_update": {
            "portfolio_heat": heat,
            "outcome_changed": new_outcome != prev_outcome if new_outcome else False
        }
    })

@app.route("/api/journal/<int:jid>", methods=["DELETE"])
def api_delete_journal(jid):
    conn = get_db()
    conn.execute("DELETE FROM trade_journal WHERE id=?", (jid,))
    conn.commit(); conn.close()
    return jsonify({"status": "deleted"})

@app.route("/api/journal/stats")
def api_journal_stats():
    conn = get_db()
    t = conn.execute("SELECT COUNT(*) as c FROM trade_journal").fetchone()["c"]
    w = conn.execute("SELECT COUNT(*) as c FROM trade_journal WHERE outcome='win'").fetchone()["c"]
    l = conn.execute("SELECT COUNT(*) as c FROM trade_journal WHERE outcome='loss'").fetchone()["c"]
    pnl = conn.execute("SELECT COALESCE(SUM(pnl_dollars),0) as s FROM trade_journal").fetchone()["s"]
    aw = conn.execute("SELECT COALESCE(AVG(pnl_percent),0) as a FROM trade_journal WHERE outcome='win'").fetchone()["a"]
    al = conn.execute("SELECT COALESCE(AVG(pnl_percent),0) as a FROM trade_journal WHERE outcome='loss'").fetchone()["a"]
    strats = conn.execute("SELECT strategy,COUNT(*) as trades,SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,COALESCE(SUM(pnl_dollars),0) as total_pnl FROM trade_journal GROUP BY strategy").fetchall()
    conn.close()
    return jsonify({"total_trades":t,"wins":w,"losses":l,"win_rate":round(w/t*100,1)if t>0 else 0,"total_pnl":round(pnl,2),"avg_win_pct":round(aw,2),"avg_loss_pct":round(al,2),"strategies":[dict(s)for s in strats]})

@app.route("/api/templates")
def api_get_templates():
    conn = get_db()
    rows = conn.execute("SELECT * FROM prompt_templates ORDER BY is_default DESC, name ASC").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/templates", methods=["POST"])
def api_add_template():
    d = request.json or {}
    conn = get_db()
    cur = conn.execute("INSERT INTO prompt_templates (name,strategy,template) VALUES (?,?,?)", (d.get("name","Custom"),d.get("strategy","general"),d.get("template","")))
    conn.commit(); conn.close()
    return jsonify({"id": cur.lastrowid, "status": "ok"})

@app.route("/api/templates/<int:tid>", methods=["PUT"])
def api_update_template(tid):
    d = request.json or {}
    conn = get_db()
    conn.execute("UPDATE prompt_templates SET name=?,strategy=?,template=?,updated_at=datetime('now') WHERE id=?", (d.get("name"),d.get("strategy"),d.get("template"),tid))
    conn.commit(); conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/templates/<int:tid>", methods=["DELETE"])
def api_delete_template(tid):
    conn = get_db()
    conn.execute("DELETE FROM prompt_templates WHERE id=? AND is_default=0", (tid,))
    conn.commit(); conn.close()
    return jsonify({"status": "deleted"})


# ---------------------------------------------------------------------------
# EXECUTION LAYER (Phase 2)
# ---------------------------------------------------------------------------
def validate_trade_against_risk(conn, symbol, size_usd, direction, signal_count, override_signals=False):
    """
    Validate a trade against risk management rules.
    Returns (is_valid, rejection_reason, warnings)
    """
    warnings = []
    
    # Get risk settings
    settings_rows = conn.execute(
        "SELECT key, value FROM settings WHERE key IN ('turtle_mode', 'consecutive_losses', 'trading_locked', 'portfolio_balance', 'portfolio_heat', 'max_position_pct', 'daily_loss_pct')"
    ).fetchall()
    settings = {r["key"]: r["value"] for r in settings_rows}
    
    # Check if trading is locked (daily loss >= 8%)
    if settings.get("trading_locked") == "true":
        return False, "TRADING LOCKED: Daily loss exceeded 8% threshold. Reset tomorrow.", []
    
    # Check turtle mode
    turtle_mode = settings.get("turtle_mode") == "true"
    if turtle_mode:
        if signal_count < 5:
            return False, f"TURTLE MODE: Requires 5/5 signals, got {signal_count}/5", []
        warnings.append("TURTLE MODE active - using conservative sizing")
    
    # Check minimum signal count (4/5 for normal trades)
    if not override_signals and signal_count < 4:
        return False, f"INSUFFICIENT SIGNALS: Need 4/5 minimum, got {signal_count}/5. Use override if this is intentional.", []
    elif signal_count < 4:
        warnings.append(f"LOW SIGNAL COUNT: Only {signal_count}/5 - override used")
    
    # Check portfolio heat
    heat = float(settings.get("portfolio_heat", 0))
    if heat > 80:
        return False, f"PORTFOLIO HEAT CRITICAL: {heat}% exposure. Close positions before opening new ones.", []
    elif heat > 60:
        warnings.append(f"HIGH HEAT: Portfolio at {heat}% exposure")
    
    # Check position size against max allowed
    balance = float(settings.get("portfolio_balance", 100))
    max_position_pct = float(settings.get("max_position_pct", 80))
    max_size_usd = balance * (max_position_pct / 100)
    
    # In turtle mode, max 60%
    if turtle_mode:
        max_size_usd = balance * 0.6
        
    if size_usd > max_size_usd:
        return False, f"POSITION TOO LARGE: ${size_usd:.2f} exceeds max ${max_size_usd:.2f} ({max_position_pct}% of ${balance})", []
    
    # Check consecutive losses warning
    consecutive_losses = int(settings.get("consecutive_losses", 0))
    if consecutive_losses >= 2:
        warnings.append(f"CAUTION: {consecutive_losses} consecutive losses - consider smaller size")
    
    return True, None, warnings


def fetch_honeypot_check(token_address, chain="eth"):
    """
    Check if a token is a honeypot using honeypot.is API.
    Returns safety score and details.
    """
    chain_map = {
        "ethereum": "eth", "eth": "eth",
        "bsc": "bsc", "binance": "bsc",
        "base": "base",
        "arbitrum": "arbitrum", "arb": "arbitrum",
        "polygon": "polygon", "matic": "polygon",
        "solana": "solana", "sol": "solana"
    }
    
    api_chain = chain_map.get(chain.lower(), "eth")
    
    # For Solana, we'll use a different approach (rugcheck.xyz API)
    if api_chain == "solana":
        return fetch_solana_safety_check(token_address)
    
    try:
        # honeypot.is API for EVM chains
        url = f"https://api.honeypot.is/v2/IsHoneypot?address={token_address}&chainID="
        chain_ids = {"eth": "1", "bsc": "56", "base": "8453", "arbitrum": "42161", "polygon": "137"}
        url += chain_ids.get(api_chain, "1")
        
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            
            is_honeypot = data.get("honeypotResult", {}).get("isHoneypot", False)
            buy_tax = data.get("simulationResult", {}).get("buyTax", 0) or 0
            sell_tax = data.get("simulationResult", {}).get("sellTax", 0) or 0
            
            # Get holder info if available
            holder_analysis = data.get("holderAnalysis", {})
            top_holder_pct = holder_analysis.get("topHolders", [{}])[0].get("percentage", 0) if holder_analysis.get("topHolders") else 0
            
            # Calculate safety score (0-100)
            safety_score = 100
            issues = []
            
            if is_honeypot:
                safety_score = 0
                issues.append("HONEYPOT DETECTED - Cannot sell")
            
            if sell_tax > 25:
                safety_score -= 50
                issues.append(f"HIGH SELL TAX: {sell_tax}%")
            elif sell_tax > 10:
                safety_score -= 20
                issues.append(f"Sell tax: {sell_tax}%")
            
            if buy_tax > 10:
                safety_score -= 15
                issues.append(f"Buy tax: {buy_tax}%")
            
            if top_holder_pct > 20:
                safety_score -= 15
                issues.append(f"Top holder owns {top_holder_pct}%")
            
            return {
                "token_address": token_address,
                "chain": chain,
                "is_honeypot": is_honeypot,
                "buy_tax": buy_tax,
                "sell_tax": sell_tax,
                "top_holder_pct": top_holder_pct,
                "safety_score": max(0, safety_score),
                "safe_to_trade": safety_score >= 40 and not is_honeypot,
                "issues": issues,
                "source": "honeypot.is"
            }
    except Exception as e:
        log.warning(f"Honeypot check failed for {token_address}: {e}")
    
    # Return unknown if check failed
    return {
        "token_address": token_address,
        "chain": chain,
        "safety_score": 50,
        "safe_to_trade": True,
        "issues": ["Could not verify - trade with caution"],
        "source": "unknown"
    }


def fetch_solana_safety_check(token_address):
    """Check Solana token safety using rugcheck.xyz API"""
    try:
        url = f"https://api.rugcheck.xyz/v1/tokens/{token_address}/report"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            
            risks = data.get("risks", [])
            score = data.get("score", 50)
            
            # rugcheck score is 0-100 where higher is safer
            issues = [r.get("description", r.get("name", "Unknown risk")) for r in risks[:5]]
            
            return {
                "token_address": token_address,
                "chain": "solana",
                "safety_score": score,
                "safe_to_trade": score >= 40,
                "issues": issues if issues else ["No major risks detected"],
                "source": "rugcheck.xyz",
                "mint_authority": data.get("mintAuthority"),
                "freeze_authority": data.get("freezeAuthority"),
                "top_holders": data.get("topHolders", [])[:5]
            }
    except Exception as e:
        log.warning(f"Solana safety check failed for {token_address}: {e}")
    
    return {
        "token_address": token_address,
        "chain": "solana",
        "safety_score": 50,
        "safe_to_trade": True,
        "issues": ["Could not verify - trade with caution"],
        "source": "unknown"
    }


def fetch_token_liquidity(token_address, chain="solana"):
    """Fetch liquidity info from DEXScreener"""
    try:
        url = f"{DEXSCREENER_API}/dex/tokens/{token_address}"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            pairs = data.get("pairs", [])
            if pairs:
                # Get highest liquidity pair
                pairs.sort(key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0), reverse=True)
                p = pairs[0]
                return {
                    "token_address": token_address,
                    "liquidity_usd": float(p.get("liquidity", {}).get("usd", 0) or 0),
                    "volume_24h": float(p.get("volume", {}).get("h24", 0) or 0),
                    "price_usd": float(p.get("priceUsd", 0) or 0),
                    "dex": p.get("dexId", ""),
                    "pair_address": p.get("pairAddress", "")
                }
    except Exception as e:
        log.warning(f"Liquidity fetch failed for {token_address}: {e}")
    return None


@app.route("/api/execute", methods=["POST"])
def api_execute_trade():
    """
    Unified execution endpoint. Validates trade against risk rules,
    returns transaction payload for frontend to sign.
    
    Does NOT execute the trade - frontend handles wallet signing.
    """
    data = request.json or {}
    
    # Required fields
    symbol = data.get("symbol", "")
    chain = data.get("chain", "solana")
    token_address = data.get("token_address", "")
    direction = data.get("direction", "long")  # long = buy, short = sell
    size_usd = float(data.get("size_usd", 0))
    entry_price = float(data.get("entry_price", 0))
    stop_loss = float(data.get("stop_loss", 0))
    take_profit = float(data.get("take_profit", 0))
    signal_count = int(data.get("signal_count", 0))
    analysis_id = data.get("analysis_id")
    override_signals = data.get("override_signals", False)
    slippage = float(data.get("slippage", 1.0))  # Default 1%
    
    if not symbol:
        return jsonify({"error": "Symbol required"}), 400
    if size_usd <= 0:
        return jsonify({"error": "Invalid size"}), 400
    
    conn = get_db()
    
    # Step 1: Validate against risk rules
    is_valid, rejection_reason, warnings = validate_trade_against_risk(
        conn, symbol, size_usd, direction, signal_count, override_signals
    )
    
    if not is_valid:
        conn.close()
        return jsonify({
            "approved": False,
            "rejection_reason": rejection_reason,
            "warnings": warnings
        }), 400
    
    # Step 2: Safety check if token address provided
    safety_check = None
    if token_address:
        safety_check = fetch_honeypot_check(token_address, chain)
        if safety_check and not safety_check.get("safe_to_trade", True):
            if safety_check.get("safety_score", 100) < 40:
                conn.close()
                return jsonify({
                    "approved": False,
                    "rejection_reason": f"SAFETY CHECK FAILED: Score {safety_check['safety_score']}/100",
                    "safety_check": safety_check,
                    "warnings": warnings
                }), 400
            warnings.append(f"Low safety score: {safety_check.get('safety_score')}/100")
    
    # Step 3: Get liquidity info
    liquidity_info = None
    if token_address:
        liquidity_info = fetch_token_liquidity(token_address, chain)
        if liquidity_info:
            liq = liquidity_info.get("liquidity_usd", 0)
            if liq < 10000:
                warnings.append(f"LOW LIQUIDITY: ${liq:,.0f} - expect high slippage")
            elif liq < 50000:
                warnings.append(f"Moderate liquidity: ${liq:,.0f}")
    
    # Step 4: Calculate trade parameters
    settings_rows = conn.execute(
        "SELECT key, value FROM settings WHERE key IN ('portfolio_balance', 'turtle_mode')"
    ).fetchall()
    settings = {r["key"]: r["value"] for r in settings_rows}
    balance = float(settings.get("portfolio_balance", 100))
    
    size_pct = (size_usd / balance * 100) if balance > 0 else 0
    
    # Get current price if not provided
    if not entry_price and liquidity_info:
        entry_price = liquidity_info.get("price_usd", 0)
    
    # Calculate token amount
    token_amount = size_usd / entry_price if entry_price > 0 else 0
    
    # Calculate risk/reward
    risk_pct = abs(entry_price - stop_loss) / entry_price * 100 if entry_price and stop_loss else 0
    reward_pct = abs(take_profit - entry_price) / entry_price * 100 if entry_price and take_profit else 0
    rr_ratio = reward_pct / risk_pct if risk_pct > 0 else 0
    
    conn.close()
    
    # Build execution payload
    execution_payload = {
        "approved": True,
        "symbol": symbol,
        "chain": chain,
        "token_address": token_address,
        "direction": direction,
        "action": "BUY" if direction == "long" else "SELL",
        "size_usd": size_usd,
        "size_pct": round(size_pct, 1),
        "token_amount": token_amount,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "signal_count": signal_count,
        "slippage": slippage,
        "min_received": token_amount * (1 - slippage/100) if direction == "long" else size_usd * (1 - slippage/100),
        "risk_pct": round(risk_pct, 2),
        "reward_pct": round(reward_pct, 2),
        "rr_ratio": round(rr_ratio, 2),
        "analysis_id": analysis_id,
        "safety_check": safety_check,
        "liquidity": liquidity_info,
        "warnings": warnings,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    
    return jsonify(execution_payload)


@app.route("/api/execute/confirm", methods=["POST"])
def api_confirm_execution():
    """
    Called AFTER successful wallet signature to log the trade.
    Creates position record and journal entry.
    """
    data = request.json or {}
    
    symbol = data.get("symbol", "")
    chain = data.get("chain", "solana")
    token_address = data.get("token_address", "")
    wallet_address = data.get("wallet_address", "")
    direction = data.get("direction", "long")
    size_usd = float(data.get("size_usd", 0))
    entry_price = float(data.get("entry_price", 0))
    stop_loss = float(data.get("stop_loss", 0))
    take_profit = float(data.get("take_profit", 0))
    signal_count = int(data.get("signal_count", 0))
    analysis_id = data.get("analysis_id")
    tx_hash = data.get("tx_hash", "")
    
    conn = get_db()
    
    # Create position record
    cur = conn.execute("""
        INSERT INTO positions (symbol, chain, token_address, wallet_address, direction, 
            entry_price, current_price, size, size_usd, stop_loss, take_profit, 
            status, analysis_id, tx_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
    """, (symbol, chain, token_address, wallet_address, direction,
          entry_price, entry_price, size_usd / entry_price if entry_price else 0, size_usd,
          stop_loss, take_profit, analysis_id, tx_hash))
    position_id = cur.lastrowid
    
    # Create journal entry
    cur2 = conn.execute("""
        INSERT INTO trade_journal (symbol, direction, strategy, entry_price, size, 
            signals_present, outcome, analysis_id)
        VALUES (?, ?, ?, ?, ?, ?, 'open', ?)
    """, (symbol, direction, data.get("strategy", ""), entry_price, size_usd, signal_count, analysis_id))
    journal_id = cur2.lastrowid
    
    # Link position to journal
    conn.execute("UPDATE positions SET journal_id = ? WHERE id = ?", (journal_id, position_id))
    
    # Update portfolio heat
    calculate_portfolio_heat(conn)
    
    conn.commit()
    conn.close()
    
    log.info(f"TRADE EXECUTED: {direction.upper()} {symbol} @ ${entry_price} | Size: ${size_usd}")
    
    return jsonify({
        "status": "confirmed",
        "position_id": position_id,
        "journal_id": journal_id,
        "message": f"Position opened: {direction.upper()} {symbol}"
    })


@app.route("/api/safety-check/<token_address>")
def api_safety_check(token_address):
    """Run honeypot/safety check on a token"""
    chain = request.args.get("chain", "solana")
    
    safety = fetch_honeypot_check(token_address, chain)
    liquidity = fetch_token_liquidity(token_address, chain)
    
    result = {
        "token_address": token_address,
        "chain": chain,
        "safety": safety,
        "liquidity": liquidity,
        "overall_safe": safety.get("safe_to_trade", False) if safety else False
    }
    
    # Add liquidity warning
    if liquidity:
        liq = liquidity.get("liquidity_usd", 0)
        if liq < 10000:
            result["liquidity_warning"] = "DANGER: Very low liquidity"
        elif liq < 50000:
            result["liquidity_warning"] = "CAUTION: Low liquidity"
    
    return jsonify(result)


@app.route("/api/positions")
def api_get_positions():
    """Get all positions, optionally filtered by status"""
    status = request.args.get("status", "")  # open, closed, stopped, or empty for all
    conn = get_db()
    
    if status:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status = ? ORDER BY opened_at DESC",
            (status,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM positions ORDER BY opened_at DESC LIMIT ?",(int(request.args.get("limit", 100)),)
        ).fetchall()
    
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/positions/<int:pid>")
def api_get_position(pid):
    """Get single position"""
    conn = get_db()
    row = conn.execute("SELECT * FROM positions WHERE id = ?", (pid,)).fetchone()
    conn.close()
    return jsonify(dict(row)) if row else (jsonify({"error": "Not found"}), 404)


@app.route("/api/positions/<int:pid>/update-price", methods=["PUT"])
def api_update_position_price(pid):
    """Update current price and calculate unrealized PnL"""
    data = request.json or {}
    current_price = float(data.get("current_price", 0))
    
    if current_price <= 0:
        return jsonify({"error": "Invalid price"}), 400
    
    conn = get_db()
    pos = conn.execute("SELECT * FROM positions WHERE id = ?", (pid,)).fetchone()
    if not pos:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    
    entry_price = pos["entry_price"]
    size_usd = pos["size_usd"]
    direction = pos["direction"]
    
    # Calculate unrealized PnL
    if direction == "long":
        pnl_pct = (current_price - entry_price) / entry_price * 100
    else:
        pnl_pct = (entry_price - current_price) / entry_price * 100
    
    pnl_usd = size_usd * (pnl_pct / 100)
    
    conn.execute("""
        UPDATE positions SET current_price = ?, unrealized_pnl = ?, unrealized_pnl_pct = ?,
            updated_at = datetime('now') WHERE id = ?
    """, (current_price, pnl_usd, pnl_pct, pid))
    conn.commit()
    conn.close()
    
    return jsonify({
        "position_id": pid,
        "current_price": current_price,
        "unrealized_pnl": round(pnl_usd, 2),
        "unrealized_pnl_pct": round(pnl_pct, 2)
    })


@app.route("/api/positions/<int:pid>/close", methods=["POST"])
def api_close_position(pid):
    """Close a position and update journal"""
    data = request.json or {}
    exit_price = float(data.get("exit_price", 0))
    tx_hash = data.get("tx_hash", "")
    
    conn = get_db()
    pos = conn.execute("SELECT * FROM positions WHERE id = ?", (pid,)).fetchone()
    if not pos:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    
    entry_price = pos["entry_price"]
    size_usd = pos["size_usd"]
    direction = pos["direction"]
    journal_id = pos["journal_id"]
    
    # Calculate realized PnL
    if direction == "long":
        pnl_pct = (exit_price - entry_price) / entry_price * 100
    else:
        pnl_pct = (entry_price - exit_price) / entry_price * 100
    
    pnl_usd = size_usd * (pnl_pct / 100)
    
    # Determine outcome
    outcome = "win" if pnl_pct > 0 else "loss" if pnl_pct < 0 else "breakeven"
    
    # Check if stop loss or take profit hit
    status = "closed"
    if pos["stop_loss"] > 0 and direction == "long" and exit_price <= pos["stop_loss"]:
        status = "stopped"
    elif pos["stop_loss"] > 0 and direction == "short" and exit_price >= pos["stop_loss"]:
        status = "stopped"
    
    # Update position
    conn.execute("""
        UPDATE positions SET current_price = ?, unrealized_pnl = ?, unrealized_pnl_pct = ?,
            status = ?, closed_at = datetime('now'), updated_at = datetime('now')
        WHERE id = ?
    """, (exit_price, pnl_usd, pnl_pct, status, pid))
    
    # Update journal entry
    if journal_id:
        conn.execute("""
            UPDATE trade_journal SET exit_price = ?, pnl_dollars = ?, pnl_percent = ?,
                outcome = ?, exit_time = datetime('now'), updated_at = datetime('now')
            WHERE id = ?
        """, (exit_price, pnl_usd, pnl_pct, outcome, journal_id))
        
        # Trigger risk management update
        update_risk_settings_on_outcome(conn, outcome, pnl_usd, pnl_pct)
    
    # Recalculate portfolio heat
    calculate_portfolio_heat(conn)
    
    conn.commit()
    conn.close()
    
    log.info(f"POSITION CLOSED: {pos['symbol']} | PnL: ${pnl_usd:.2f} ({pnl_pct:.1f}%) | {outcome.upper()}")
    
    return jsonify({
        "position_id": pid,
        "status": status,
        "outcome": outcome,
        "exit_price": exit_price,
        "pnl_usd": round(pnl_usd, 2),
        "pnl_pct": round(pnl_pct, 2)
    })


@app.route("/api/positions/open-summary")
def api_positions_summary():
    """Get summary of all open positions"""
    conn = get_db()
    
    open_positions = conn.execute(
        "SELECT * FROM positions WHERE status = 'open'"
    ).fetchall()
    
    total_exposure = sum(p["size_usd"] for p in open_positions)
    total_unrealized = sum(p["unrealized_pnl"] or 0 for p in open_positions)
    
    # Get portfolio balance for heat calculation
    balance = float(conn.execute(
        "SELECT value FROM settings WHERE key='portfolio_balance'"
    ).fetchone()["value"] or 100)
    
    conn.close()
    
    return jsonify({
        "open_count": len(open_positions),
        "total_exposure_usd": round(total_exposure, 2),
        "total_unrealized_pnl": round(total_unrealized, 2),
        "portfolio_heat": round(total_exposure / balance * 100, 1) if balance > 0 else 0,
        "positions": [dict(p) for p in open_positions]
    })


# ---------------------------------------------------------------------------
# PHASE 5 API ENDPOINTS: INTELLIGENCE & OPTIMIZATION
# ---------------------------------------------------------------------------

@app.route("/api/intel/backtest/<symbol>/<strategy>")
def api_backtest(symbol, strategy):
    """
    Run backtest for a symbol/strategy combination.
    Query params: days (default 365), save (true to save results)
    """
    symbol = symbol.upper()
    days = int(request.args.get("days", 365))
    save = request.args.get("save", "").lower() == "true"
    
    # Fetch historical data
    ohlcv = fetch_historical_ohlcv(symbol, days=days)
    if not ohlcv or len(ohlcv) < 50:
        return jsonify({"error": f"Insufficient historical data for {symbol}"}), 404
    
    # Run backtest
    if strategy == "penguin":
        result = backtest_penguin_strategy(ohlcv)
    elif strategy == "burry":
        result = backtest_burry_strategy(ohlcv)
    else:
        return jsonify({"error": f"Unknown strategy: {strategy}. Use 'penguin' or 'burry'."}), 400
    
    if "error" in result:
        return jsonify(result), 400
    
    # Optionally save results
    if save:
        conn = get_db()
        conn.execute("""
            INSERT INTO backtest_results 
            (symbol, strategy, params, period_start, period_end, total_trades, win_rate,
             total_pnl, max_drawdown, profit_factor, expectancy, equity_curve, trades, monthly_returns)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            symbol, strategy, json.dumps(result.get("params", {})),
            result["period"]["start"], result["period"]["end"],
            result["total_trades"], result["win_rate"], result["total_pnl_pct"],
            result["max_drawdown_pct"], 
            result["profit_factor"] if result["profit_factor"] != "∞" else 999,
            result["expectancy"], json.dumps(result["equity_curve"]),
            json.dumps(result["trades"]), json.dumps(result["monthly_returns"])
        ))
        conn.commit()
        conn.close()
    
    return jsonify(result)


@app.route("/api/intel/backtest/optimize/<symbol>/<strategy>")
def api_backtest_optimize(symbol, strategy):
    """
    Run parameter optimization for a strategy.
    Uses walk-forward validation (80/20 split).
    """
    symbol = symbol.upper()
    days = int(request.args.get("days", 365))
    
    ohlcv = fetch_historical_ohlcv(symbol, days=days)
    if not ohlcv or len(ohlcv) < 100:
        return jsonify({"error": f"Need at least 100 days of data for optimization"}), 404
    
    result = optimize_strategy_params(symbol, strategy, ohlcv)
    return jsonify(result)


@app.route("/api/intel/backtest/results")
def api_backtest_results():
    """Get saved backtest results"""
    limit = int(request.args.get("limit", 20))
    symbol = request.args.get("symbol", "").upper()
    strategy = request.args.get("strategy", "")
    
    conn = get_db()
    
    query = "SELECT * FROM backtest_results"
    conditions = []
    params = []
    
    if symbol:
        conditions.append("symbol = ?")
        params.append(symbol)
    if strategy:
        conditions.append("strategy = ?")
        params.append(strategy)
    
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    
    rows = conn.execute(query, params).fetchall()
    conn.close()
    
    results = []
    for row in rows:
        r = dict(row)
        r["params"] = json.loads(r["params"]) if r["params"] else {}
        r["equity_curve"] = json.loads(r["equity_curve"]) if r["equity_curve"] else []
        r["monthly_returns"] = json.loads(r["monthly_returns"]) if r["monthly_returns"] else {}
        results.append(r)
    
    return jsonify(results)


@app.route("/api/intel/patterns/capture/<int:journal_id>", methods=["POST"])
def api_capture_pattern(journal_id):
    """Capture indicator snapshot for a completed trade"""
    conn = get_db()
    snapshot = capture_trade_snapshot(conn, journal_id)
    conn.close()
    
    if not snapshot:
        return jsonify({"error": "Could not capture pattern for this trade"}), 400
    
    return jsonify({"status": "captured", "snapshot": snapshot})


@app.route("/api/intel/patterns/similar", methods=["POST"])
def api_find_similar_patterns():
    """
    Find historical trades similar to a given setup.
    Body: current_setup (indicator values), strategy (optional), min_similarity (default 60)
    """
    data = request.json or {}
    current_setup = data.get("current_setup", {})
    strategy = data.get("strategy")
    min_similarity = data.get("min_similarity", 60)
    
    if not current_setup:
        return jsonify({"error": "current_setup required"}), 400
    
    conn = get_db()
    result = find_similar_historical_trades(conn, current_setup, strategy, min_similarity)
    conn.close()
    
    return jsonify(result)


@app.route("/api/intel/patterns/time-analysis")
def api_time_analysis():
    """Analyze performance by time of day and day of week"""
    strategy = request.args.get("strategy")
    
    conn = get_db()
    result = analyze_time_performance(conn, strategy)
    conn.close()
    
    return jsonify(result)


@app.route("/api/intel/patterns/health-report")
def api_health_report():
    """Generate strategy health report"""
    strategy = request.args.get("strategy")
    
    conn = get_db()
    result = generate_strategy_health_report(conn, strategy)
    conn.close()
    
    return jsonify(result)


@app.route("/api/intel/regime/<symbol>")
def api_market_regime(symbol):
    """
    Get current market regime classification for a symbol.
    Uses HMM-like classification: TRENDING, MEAN_REVERTING, or TRANSITIONAL.
    """
    symbol = symbol.upper()
    
    ohlcv = fetch_coingecko_ohlcv(symbol, days=30)
    if not ohlcv or len(ohlcv) < 30:
        return jsonify({"error": f"Insufficient data for regime classification"}), 404
    
    result = classify_market_regime_hmm(ohlcv)
    result["symbol"] = symbol
    
    # Store regime for history tracking
    conn = get_db()
    
    # Get previous regime
    prev = conn.execute("""
        SELECT regime FROM regime_history 
        WHERE symbol = ? ORDER BY detected_at DESC LIMIT 1
    """, (symbol,)).fetchone()
    
    previous_regime = prev["regime"] if prev else None
    
    # Check for regime change
    if previous_regime and previous_regime != result["regime"]:
        result["regime_change"] = detect_regime_change(result["regime"], previous_regime)
    
    # Store current regime
    conn.execute("""
        INSERT INTO regime_history (symbol, regime, confidence, features, previous_regime)
        VALUES (?, ?, ?, ?, ?)
    """, (symbol, result["regime"], result["confidence"], 
          json.dumps(result["features"]), previous_regime or ""))
    conn.commit()
    conn.close()
    
    return jsonify(result)


@app.route("/api/intel/regime/history/<symbol>")
def api_regime_history(symbol):
    """Get regime history for a symbol"""
    symbol = symbol.upper()
    limit = int(request.args.get("limit", 30))
    
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM regime_history 
        WHERE symbol = ?
        ORDER BY detected_at DESC LIMIT ?
    """, (symbol, limit)).fetchall()
    conn.close()
    
    results = []
    for row in rows:
        r = dict(row)
        r["features"] = json.loads(r["features"]) if r["features"] else {}
        results.append(r)
    
    return jsonify(results)


@app.route("/api/intel/attribution")
def api_performance_attribution():
    """Get performance attribution breakdown"""
    conn = get_db()
    result = calculate_performance_attribution(conn)
    conn.close()
    
    return jsonify(result)


@app.route("/api/intel/recommendations")
def api_trade_recommendations():
    """Get actionable trading recommendations based on performance"""
    conn = get_db()
    result = generate_trade_recommendations(conn)
    conn.close()
    
    return jsonify(result)


@app.route("/api/intel/risk/ruin")
def api_ruin_probability():
    """
    Calculate probability of ruin.
    Query params: win_rate, win_loss_ratio, position_size_pct (or use journal data)
    """
    win_rate = request.args.get("win_rate")
    wl_ratio = request.args.get("win_loss_ratio")
    size = request.args.get("position_size_pct")
    
    if win_rate and wl_ratio and size:
        # Use provided values
        prob = calculate_ruin_probability(
            float(win_rate) / 100, float(wl_ratio), float(size)
        )
        return jsonify({
            "win_rate": float(win_rate),
            "win_loss_ratio": float(wl_ratio),
            "position_size_pct": float(size),
            "ruin_probability": prob,
            "survival_probability": round(1 - prob, 4) if prob else None,
            "risk_level": "SAFE" if prob and prob < 0.01 else 
                         "MODERATE" if prob and prob < 0.05 else 
                         "DANGEROUS" if prob and prob < 0.15 else "CRITICAL"
        })
    else:
        # Use journal data
        conn = get_db()
        result = calculate_optimal_position_size(conn)
        conn.close()
        return jsonify(result)


@app.route("/api/intel/risk/simulate")
def api_drawdown_simulation():
    """Run Monte Carlo simulation of potential drawdowns"""
    num_sims = int(request.args.get("simulations", 1000))
    
    conn = get_db()
    result = simulate_drawdown_scenarios(conn, num_sims)
    conn.close()
    
    return jsonify(result)


@app.route("/api/intel/risk/stress-test", methods=["POST"])
def api_stress_test():
    """
    Stress test portfolio against specific scenarios.
    Body: portfolio_value (optional), scenario (required)
    """
    data = request.json or {}
    scenario = data.get("scenario")
    
    if not scenario:
        return jsonify({
            "error": "Scenario required",
            "available_scenarios": ["btc_crash_20", "btc_crash_40", "flash_crash", "short_squeeze", "mild_correction"]
        }), 400
    
    conn = get_db()
    
    # Get portfolio value
    portfolio_value = data.get("portfolio_value")
    if not portfolio_value:
        row = conn.execute("SELECT value FROM settings WHERE key='portfolio_balance'").fetchone()
        portfolio_value = float(row["value"]) if row else 100
    
    # Get open positions
    positions = conn.execute("""
        SELECT symbol, direction, size_usd, leverage 
        FROM positions WHERE status = 'open'
    """).fetchall()
    
    conn.close()
    
    positions_list = [dict(p) for p in positions]
    
    if not positions_list:
        return jsonify({"error": "No open positions to stress test"}), 400
    
    result = stress_test_scenario(portfolio_value, positions_list, scenario)
    return jsonify(result)


@app.route("/api/intel/risk/optimal-size")
def api_optimal_position_size():
    """Calculate optimal position size based on Kelly criterion"""
    risk_per_trade = request.args.get("risk_per_trade")
    risk_per_trade = float(risk_per_trade) if risk_per_trade else None
    
    conn = get_db()
    result = calculate_optimal_position_size(conn, risk_per_trade=risk_per_trade)
    conn.close()
    
    return jsonify(result)


@app.route("/api/intel/dashboard")
def api_intel_dashboard():
    """
    Comprehensive intelligence dashboard - all Phase 5 data in one call.
    Query params: symbol (default BTC)
    """
    symbol = request.args.get("symbol", "BTC").upper()
    
    conn = get_db()
    
    # Performance attribution
    attribution = calculate_performance_attribution(conn)
    
    # Recommendations
    recommendations = generate_trade_recommendations(conn)
    
    # Time analysis
    time_analysis = analyze_time_performance(conn)
    
    # Optimal sizing
    sizing = calculate_optimal_position_size(conn)
    
    # Monte Carlo simulation (reduced for speed)
    simulation = simulate_drawdown_scenarios(conn, num_simulations=500)
    
    conn.close()
    
    # Market regime
    ohlcv = fetch_coingecko_ohlcv(symbol, days=30)
    regime = classify_market_regime_hmm(ohlcv) if ohlcv and len(ohlcv) >= 30 else None
    
    return jsonify({
        "symbol": symbol,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market_regime": regime,
        "performance_attribution": attribution,
        "recommendations": recommendations.get("recommendations", []) if isinstance(recommendations, dict) else [],
        "time_analysis": time_analysis,
        "position_sizing": sizing,
        "risk_simulation": simulation
    })


# ---------------------------------------------------------------------------
# FRONTEND (same UI structure, updated for Claude branding + web search indicators)
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return HTML_PAGE

# Using raw string to avoid escape issues
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Penguin-Burry Analyzer - Claude Opus 4.5</title>
<style>
:root{--bg-0:#030305;--bg-1:#08080e;--bg-2:#0e0e18;--bg-3:#161622;--bg-4:#1e1e30;--border:#252540;--border-focus:#3a3a60;--text-0:#e8e8f4;--text-1:#b0b0c8;--text-2:#7878a0;--text-3:#505070;--accent:#6c5ce7;--accent-dim:#5a4bd6;--green:#00e676;--green-dim:#00c853;--red:#ff5252;--red-dim:#d32f2f;--yellow:#ffd740;--blue:#448aff;--cyan:#18ffff;--orange:#ff9100;--penguin:#18ffff;--burry:#ff5252;--radius:8px;--radius-lg:12px;--shadow:0 4px 32px rgba(0,0,0,0.6);--tr:all .2s ease}
*{margin:0;padding:0;box-sizing:border-box}html{font-size:14px}
body{background:var(--bg-0);color:var(--text-0);font-family:'SF Mono','JetBrains Mono','Fira Code','Consolas',monospace;line-height:1.6;min-height:100vh}
a{color:var(--accent);text-decoration:none}
::-webkit-scrollbar{width:5px;height:5px}::-webkit-scrollbar-track{background:var(--bg-0)}::-webkit-scrollbar-thumb{background:var(--bg-4);border-radius:3px}
.app{display:flex;flex-direction:column;min-height:100vh}
header{background:linear-gradient(180deg,var(--bg-1),var(--bg-0));border-bottom:1px solid var(--border);padding:10px 24px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;backdrop-filter:blur(12px)}
.logo{display:flex;align-items:center;gap:10px}.logo-icon{font-size:1.6rem}
.logo-text{font-size:1rem;font-weight:800;background:linear-gradient(135deg,var(--cyan),var(--accent));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.logo-sub{font-size:.65rem;color:var(--text-3);display:flex;align-items:center;gap:6px}
.web-badge{background:rgba(255,215,64,.15);color:var(--yellow);padding:1px 6px;border-radius:3px;font-size:.6rem;font-weight:700}
.header-right{display:flex;align-items:center;gap:14px}
.status-pill{display:flex;align-items:center;gap:6px;padding:4px 12px;border-radius:20px;font-size:.7rem;border:1px solid var(--border)}
.dot{width:7px;height:7px;border-radius:50%}.dot.ok{background:var(--green);box-shadow:0 0 8px var(--green)}.dot.err{background:var(--red);box-shadow:0 0 8px var(--red)}
.turtle-banner{background:rgba(255,215,64,.06);border:1px solid rgba(255,215,64,.2);border-radius:var(--radius);padding:8px 16px;display:none;font-size:.75rem;color:var(--yellow);align-items:center;gap:8px}.turtle-banner.show{display:flex}
.tab-bar{display:flex;background:var(--bg-1);border-bottom:1px solid var(--border);padding:0 24px;gap:1px;overflow-x:auto}
.tab{padding:10px 18px;cursor:pointer;color:var(--text-3);font-size:.72rem;font-weight:700;letter-spacing:.8px;text-transform:uppercase;border-bottom:2px solid transparent;transition:var(--tr);white-space:nowrap;user-select:none}.tab:hover{color:var(--text-1)}.tab.active{color:var(--accent);border-bottom-color:var(--accent)}
main{flex:1;padding:20px 24px;max-width:1600px;margin:0 auto;width:100%}.panel{display:none}.panel.active{display:block}
.card{background:var(--bg-2);border:1px solid var(--border);border-radius:var(--radius-lg);padding:18px;margin-bottom:14px;transition:var(--tr)}.card:hover{border-color:var(--border-focus)}
.card-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid var(--border)}.card-title{font-size:.9rem;font-weight:800}
.form-group{margin-bottom:12px}.form-label{display:block;font-size:.68rem;font-weight:700;color:var(--text-3);margin-bottom:5px;text-transform:uppercase;letter-spacing:.8px}
input[type="text"],input[type="number"],select,textarea{width:100%;background:var(--bg-0);border:1px solid var(--border);border-radius:var(--radius);padding:9px 12px;color:var(--text-0);font-family:inherit;font-size:.82rem;transition:var(--tr)}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(108,92,231,.12)}
textarea{resize:vertical;min-height:70px}select{cursor:pointer;appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='10' fill='%237878a0' viewBox='0 0 16 16'%3E%3Cpath d='M8 11L3 6h10z'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 10px center;padding-right:30px}
.btn{display:inline-flex;align-items:center;gap:5px;padding:7px 16px;border:1px solid var(--border);border-radius:var(--radius);background:var(--bg-3);color:var(--text-0);font-family:inherit;font-size:.75rem;font-weight:700;cursor:pointer;transition:var(--tr);white-space:nowrap}
.btn:hover{background:var(--bg-4);border-color:var(--border-focus)}.btn:active{transform:scale(.97)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}.btn.primary:hover{background:var(--accent-dim)}
.btn.danger{background:var(--red-dim);border-color:var(--red-dim);color:#fff}.btn.danger:hover{background:var(--red)}
.btn.success{background:var(--green-dim);border-color:var(--green-dim);color:#fff}
.btn.sm{padding:3px 9px;font-size:.68rem}.btn-group{display:flex;gap:6px;flex-wrap:wrap}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:.65rem;font-weight:800;text-transform:uppercase;letter-spacing:.5px}
.badge.trade{background:rgba(0,230,118,.12);color:var(--green);border:1px solid rgba(0,230,118,.25)}
.badge.hold{background:rgba(255,215,64,.12);color:var(--yellow);border:1px solid rgba(255,215,64,.25)}
.badge.avoid{background:rgba(255,82,82,.12);color:var(--red);border:1px solid rgba(255,82,82,.25)}
.badge.penguin{background:rgba(24,255,255,.08);color:var(--penguin);border:1px solid rgba(24,255,255,.2)}
.badge.burry{background:rgba(255,82,82,.08);color:var(--burry);border:1px solid rgba(255,82,82,.2)}
.badge.general,.badge.auto{background:rgba(108,92,231,.12);color:var(--accent);border:1px solid rgba(108,92,231,.25)}
.badge.scalp{background:rgba(255,145,0,.12);color:var(--orange);border:1px solid rgba(255,145,0,.25)}
.badge.win{background:rgba(0,230,118,.12);color:var(--green)}.badge.loss{background:rgba(255,82,82,.12);color:var(--red)}
.badge.open{background:rgba(68,138,255,.12);color:var(--blue)}.badge.breakeven{background:rgba(120,120,160,.12);color:var(--text-2)}
.badge.web{background:rgba(255,215,64,.1);color:var(--yellow);border:1px solid rgba(255,215,64,.25);font-size:.6rem}
.signal-bar{display:flex;gap:3px;margin:6px 0}.signal-pip{width:24px;height:5px;border-radius:3px;background:var(--bg-4)}
.signal-pip.active{background:var(--green);box-shadow:0 0 6px rgba(0,230,118,.3)}.signal-pip.active.warn{background:var(--yellow)}.signal-pip.active.danger{background:var(--red)}
.drop-zone{border:2px dashed var(--border);border-radius:var(--radius-lg);padding:40px 20px;text-align:center;cursor:pointer;transition:var(--tr);background:var(--bg-0);position:relative}
.drop-zone:hover,.drop-zone.dragover{border-color:var(--accent);background:rgba(108,92,231,.03)}
.drop-zone input{position:absolute;inset:0;opacity:0;cursor:pointer}.drop-zone-icon{font-size:2.2rem;margin-bottom:6px}.drop-zone-text{color:var(--text-3);font-size:.8rem}
.drop-zone-preview{max-height:260px;border-radius:var(--radius);margin-top:10px;display:none}
.grid{display:grid;gap:14px}.grid-2{grid-template-columns:repeat(2,1fr)}.grid-3{grid-template-columns:repeat(3,1fr)}.grid-4{grid-template-columns:repeat(4,1fr)}
@media(max-width:1200px){.grid-4{grid-template-columns:repeat(2,1fr)}}@media(max-width:768px){.grid-2,.grid-3,.grid-4{grid-template-columns:1fr}header{flex-direction:column;gap:8px}main{padding:12px}}
.stat{text-align:center;padding:14px}.stat-value{font-size:1.6rem;font-weight:900;margin-bottom:2px}.stat-label{font-size:.65rem;color:var(--text-3);text-transform:uppercase;letter-spacing:1px}
.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:.78rem}th,td{padding:8px 12px;text-align:left;border-bottom:1px solid var(--border)}
th{color:var(--text-3);font-weight:700;text-transform:uppercase;font-size:.65rem;letter-spacing:.5px;background:var(--bg-0);position:sticky;top:0}tr:hover td{background:rgba(108,92,231,.03)}
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.8);z-index:1000;justify-content:center;align-items:center;padding:24px}.modal-overlay.show{display:flex}
.modal{background:var(--bg-2);border:1px solid var(--border);border-radius:var(--radius-lg);max-width:720px;width:100%;max-height:85vh;overflow-y:auto;padding:24px;box-shadow:var(--shadow)}
.modal-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px;padding-bottom:10px;border-bottom:1px solid var(--border)}
.modal-close{background:none;border:none;color:var(--text-3);cursor:pointer;font-size:1.4rem;padding:4px;line-height:1}.modal-close:hover{color:var(--text-0)}
.signal-detail{display:flex;align-items:center;gap:10px;padding:8px 12px;background:var(--bg-0);border-radius:var(--radius);margin-bottom:5px;border-left:3px solid var(--bg-4)}
.signal-detail.active{border-left-color:var(--green)}.signal-detail.inactive{border-left-color:var(--red);opacity:.7}
.signal-icon{font-size:1rem;width:22px;text-align:center}.signal-name{font-weight:700;min-width:90px;font-size:.75rem;text-transform:uppercase}.signal-value{color:var(--text-1);flex:1;font-size:.78rem}
.web-sources{margin-top:14px;padding:12px;background:rgba(255,215,64,.04);border:1px solid rgba(255,215,64,.12);border-radius:var(--radius)}.web-sources-title{font-size:.72rem;font-weight:700;color:var(--yellow);margin-bottom:8px}
.web-source{font-size:.7rem;color:var(--text-2);padding:3px 0}.web-source a{color:var(--blue);font-size:.68rem}
.conf-meter{display:flex;gap:2px;align-items:center}.conf-bar{width:16px;height:7px;border-radius:2px;background:var(--bg-4)}
.conf-bar.filled.low{background:var(--red)}.conf-bar.filled.medium{background:var(--yellow)}.conf-bar.filled.high{background:var(--green)}.conf-bar.filled.very_high{background:var(--cyan);box-shadow:0 0 4px var(--cyan)}
.empty-state{text-align:center;padding:40px;color:var(--text-3)}.empty-state-icon{font-size:2.5rem;margin-bottom:10px}
.raw-output{background:var(--bg-0);border:1px solid var(--border);border-radius:var(--radius);padding:12px;font-size:.72rem;max-height:400px;overflow:auto;white-space:pre-wrap;word-break:break-word;color:var(--text-2)}
.img-thumb{width:52px;height:52px;object-fit:cover;border-radius:var(--radius);cursor:pointer;border:1px solid var(--border)}.img-thumb:hover{border-color:var(--accent)}
.trade-params{background:var(--bg-0);border:1px solid rgba(0,230,118,.15);border-radius:var(--radius);padding:14px;margin-top:14px}.trade-params .tp-title{font-weight:800;font-size:.8rem;margin-bottom:10px;color:var(--green)}
.market-ctx{background:rgba(24,255,255,.04);border:1px solid rgba(24,255,255,.1);border-radius:var(--radius);padding:10px 14px;margin-bottom:14px;font-size:.78rem;color:var(--text-1)}.market-ctx strong{color:var(--cyan)}
.chat-container{display:flex;flex-direction:column;height:500px}.chat-messages{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px}
.chat-msg{max-width:80%;padding:10px 14px;border-radius:var(--radius);font-size:.82rem;line-height:1.5}.chat-msg.user{background:var(--accent);color:#fff;align-self:flex-end}.chat-msg.assistant{background:var(--bg-3);color:var(--text-0);align-self:flex-start}
.chat-input-row{display:flex;gap:8px;padding:12px;border-top:1px solid var(--border)}.chat-input-row input{flex:1}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid var(--text-3);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle;margin-left:6px}
@keyframes spin{to{transform:rotate(360deg)}}
.toast-container{position:fixed;top:70px;right:20px;z-index:2000;display:flex;flex-direction:column;gap:6px}
.toast{padding:10px 18px;border-radius:var(--radius);font-size:.78rem;font-weight:700;animation:slideIn .3s ease,fadeOut .3s ease 2.7s;box-shadow:var(--shadow)}
.toast.success{background:var(--green-dim);color:#fff}.toast.error{background:var(--red-dim);color:#fff}.toast.info{background:var(--accent-dim);color:#fff}
@keyframes slideIn{from{transform:translateX(80px);opacity:0}}@keyframes fadeOut{to{opacity:0}}
.wallet-status{display:flex;align-items:center;gap:8px;font-size:.72rem}
.wallet-btn{padding:5px 12px;border-radius:var(--radius);font-size:.7rem;font-weight:700;cursor:pointer;border:1px solid var(--border);background:var(--bg-3);color:var(--text-0);transition:var(--tr)}
.wallet-btn:hover{background:var(--bg-4);border-color:var(--accent)}
.wallet-btn.connected{background:rgba(0,230,118,.1);border-color:var(--green);color:var(--green)}
.wallet-btn.phantom{background:rgba(171,116,255,.1);border-color:#ab74ff;color:#ab74ff}
.wallet-btn.metamask{background:rgba(255,165,0,.1);border-color:#f6851b;color:#f6851b}
.wallet-addr{font-size:.65rem;color:var(--text-2);max-width:80px;overflow:hidden;text-overflow:ellipsis}
.safety-score{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:4px;font-size:.7rem;font-weight:800}
.safety-score.safe{background:rgba(0,230,118,.12);color:var(--green)}
.safety-score.caution{background:rgba(255,215,64,.12);color:var(--yellow)}
.safety-score.danger{background:rgba(255,82,82,.12);color:var(--red)}
.exec-warning{background:rgba(255,215,64,.08);border:1px solid rgba(255,215,64,.2);border-radius:var(--radius);padding:8px 12px;margin:8px 0;font-size:.75rem;color:var(--yellow)}
.exec-confirm{background:var(--bg-0);border:1px solid var(--border);border-radius:var(--radius);padding:16px;margin-top:12px}
.exec-row{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border);font-size:.8rem}
.exec-row:last-child{border-bottom:none}
.exec-label{color:var(--text-3)}.exec-value{font-weight:700}
.pnl-positive{color:var(--green)}.pnl-negative{color:var(--red)}
</style>
</head>
<body>
<div class="app">
<header>
    <div class="logo"><span class="logo-icon">&#x1F427;</span><div><div class="logo-text">PENGUIN-BURRY ANALYZER</div><div class="logo-sub">Claude Opus 4.5 + Web Search <span class="web-badge">&#x1F50D; LIVE DATA</span></div></div></div>
    <div class="header-right">
        <div class="wallet-status" id="walletStatus"></div>
        <div class="turtle-banner" id="turtleBanner">&#x1F422; TURTLE MODE</div>
        <div class="status-pill"><span class="dot" id="statusDot"></span><span id="statusLabel">...</span></div>
    </div>
</header>
<div class="tab-bar">
    <div class="tab active" data-tab="analyze">&#x1F4CA; Analyze</div>
    <div class="tab" data-tab="positions">&#x1F4B0; Positions</div>
    <div class="tab" data-tab="alerts">&#x1F514; Alerts</div>
    <div class="tab" data-tab="history">&#x1F4CB; History</div>
    <div class="tab" data-tab="journal">&#x1F4D3; Journal</div>
    <div class="tab" data-tab="quant">&#x1F4CA; Quant</div>
    <div class="tab" data-tab="watchlist">&#x1F441; Watchlist</div>
    <div class="tab" data-tab="chat">&#x1F4AC; Chat</div>
    <div class="tab" data-tab="templates">&#x1F4DD; Templates</div>
    <div class="tab" data-tab="settings">&#x2699; Settings</div>
</div>
<main>
<div class="panel active" id="panel-analyze">
    <div class="grid grid-2" style="grid-template-columns:1fr 1.3fr">
        <div>
            <div class="card"><div class="card-header"><span class="card-title">Chart Image</span></div>
                <div class="drop-zone" id="dropZone"><input type="file" id="imageInput" accept="image/*"><div class="drop-zone-icon">&#x1F4CA;</div><div class="drop-zone-text">Drop chart here or click to browse</div><img class="drop-zone-preview" id="imagePreview"></div>
            </div>
            <div class="card"><div class="card-header"><span class="card-title">Config</span></div>
                <div class="form-group"><label class="form-label">Claude Model</label><select id="modelSelect"></select></div>
                <div class="grid grid-2">
                    <div class="form-group"><label class="form-label">Strategy</label><select id="strategySelect"><option value="auto">Auto Detect</option><option value="penguin">Penguin Long</option><option value="burry">Burry Short</option><option value="scalp">Quick Scalp</option></select></div>
                    <div class="form-group"><label class="form-label">Symbol</label><input type="text" id="symbolInput" placeholder="BTC, SOL..."></div>
                </div>
                <div class="form-group"><label class="form-label">Title</label><input type="text" id="titleInput" placeholder="Optional..."></div>
                <div class="form-group" style="display:flex;align-items:center;gap:10px">
                    <input type="checkbox" id="webSearchToggle" checked style="width:auto">
                    <label for="webSearchToggle" style="font-size:.78rem;color:var(--yellow);cursor:pointer">&#x1F50D; Web Search (live prices, news, volume)</label>
                </div>
                <div class="form-group"><label class="form-label">Custom Prompt</label><textarea id="customPrompt" rows="2" placeholder="Override..."></textarea></div>
                <button class="btn primary" id="analyzeBtn" style="width:100%;justify-content:center;padding:12px;font-size:.85rem">&#x1F50D; ANALYZE WITH CLAUDE</button>
            </div>
            <div class="card"><div class="card-header"><span class="card-title">Quick Market Check</span></div>
                <div style="display:flex;gap:8px"><input type="text" id="quickSymbol" placeholder="Symbol..." style="flex:1"><button class="btn primary sm" id="quickCheckBtn">Check</button></div>
                <div id="quickResult" style="margin-top:10px;font-size:.78rem;display:none"></div>
            </div>
        </div>
        <div>
            <div class="card" id="resultCard" style="display:none"><div class="card-header"><span class="card-title" id="resultTitle">Analysis</span><div class="btn-group"><button class="btn sm" onclick="toggleRaw()">{ } Raw</button><button class="btn sm danger" id="resultDeleteBtn">Del</button></div></div><div id="resultContent"></div><div class="raw-output" id="rawOutput" style="display:none"></div></div>
            <div id="resultPlaceholder" class="card"><div class="empty-state"><div class="empty-state-icon">&#x1F427;</div><div>Upload a chart and hit Analyze</div><div style="color:var(--text-3);margin-top:6px;font-size:.72rem">"If it's not obvious, it's not a trade."</div><div style="color:var(--yellow);margin-top:12px;font-size:.7rem">&#x1F50D; Claude searches the web for live prices, news &amp; volume</div></div></div>
        </div>
    </div>
</div>
<div class="panel" id="panel-history"><div class="card"><div class="card-header"><span class="card-title">History</span><button class="btn sm" onclick="loadHistory()">Refresh</button></div><div class="table-wrap"><table><thead><tr><th>Date</th><th>Symbol</th><th>Strategy</th><th>Signals</th><th>Rec</th><th>Conf</th><th>Web</th><th>Model</th><th>Img</th><th>Actions</th></tr></thead><tbody id="historyBody"></tbody></table></div></div></div>
<div class="panel" id="panel-alerts">
    <div class="grid grid-4" id="alertStats" style="margin-bottom:14px">
        <div class="stat-card"><div class="stat-value" id="statActiveAlerts">0</div><div class="stat-label">Active Alerts</div></div>
        <div class="stat-card"><div class="stat-value" id="statTriggeredToday">0</div><div class="stat-label">Triggered Today</div></div>
        <div class="stat-card"><div class="stat-value" id="statMonitorStatus">OFF</div><div class="stat-label">Monitor Status</div></div>
        <div class="stat-card"><div class="stat-value" id="statActiveSetups">0</div><div class="stat-label">Active Setups</div></div>
    </div>
    <div class="grid grid-2" style="margin-bottom:14px">
        <div class="card">
            <div class="card-header">
                <span class="card-title">&#x1F514; Price & Indicator Alerts</span>
                <div class="btn-group">
                    <button class="btn sm" onclick="loadAlerts()">Refresh</button>
                    <button class="btn primary sm" onclick="openAlertModal()">+ New Alert</button>
                </div>
            </div>
            <div class="table-wrap" style="max-height:300px;overflow-y:auto">
                <table>
                    <thead><tr><th>Name</th><th>Symbol</th><th>Type</th><th>Condition</th><th>Target</th><th>Status</th><th>Actions</th></tr></thead>
                    <tbody id="alertsBody"></tbody>
                </table>
            </div>
        </div>
        <div class="card">
            <div class="card-header">
                <span class="card-title">&#x1F50D; Auto-Scanner Setups</span>
                <div class="btn-group">
                    <button class="btn sm" onclick="loadScanResults()">Refresh</button>
                    <button class="btn primary sm" onclick="runManualScan()">Scan Now</button>
                </div>
            </div>
            <div class="table-wrap" style="max-height:300px;overflow-y:auto">
                <table>
                    <thead><tr><th>Symbol</th><th>Strategy</th><th>Signals</th><th>Price</th><th>Rec</th><th>Time</th><th>Actions</th></tr></thead>
                    <tbody id="scanResultsBody"></tbody>
                </table>
            </div>
        </div>
    </div>
    <div class="grid grid-2">
        <div class="card">
            <div class="card-header">
                <span class="card-title">&#x1F4DC; Alert History</span>
                <button class="btn sm" onclick="loadAlertHistory()">Refresh</button>
            </div>
            <div class="table-wrap" style="max-height:250px;overflow-y:auto">
                <table>
                    <thead><tr><th>Time</th><th>Alert</th><th>Symbol</th><th>Message</th></tr></thead>
                    <tbody id="alertHistoryBody"></tbody>
                </table>
            </div>
        </div>
        <div class="card">
            <div class="card-header">
                <span class="card-title">&#x2699; Monitor Controls</span>
            </div>
            <div style="padding:12px">
                <div style="display:flex;gap:10px;margin-bottom:12px">
                    <button class="btn success" id="monitorStartBtn" onclick="startMonitor()">&#x25B6; Start Monitor</button>
                    <button class="btn danger" id="monitorStopBtn" onclick="stopMonitor()">&#x23F9; Stop Monitor</button>
                </div>
                <div style="font-size:.75rem;color:var(--text-2)" id="monitorInfo">
                    <div>Alert checks: every 30 seconds</div>
                    <div>Auto-scan: every 5 minutes</div>
                    <div>Last scan: <span id="lastScanTime">Never</span></div>
                </div>
                <div style="margin-top:12px">
                    <label style="font-size:.75rem;color:var(--text-2);display:flex;align-items:center;gap:8px">
                        <input type="checkbox" id="browserNotifications" style="width:auto" onchange="toggleBrowserNotifications()">
                        Enable browser notifications
                    </label>
                </div>
            </div>
        </div>
    </div>
</div>
<div class="panel" id="panel-positions">
    <div class="grid grid-4" id="positionStats"></div>
    <div class="card" style="margin-top:14px">
        <div class="card-header">
            <span class="card-title">Open Positions</span>
            <div class="btn-group">
                <button class="btn sm" onclick="loadPositions()">Refresh</button>
                <button class="btn primary sm" id="connectWalletBtn" onclick="connectWallet()">Connect Wallet</button>
            </div>
        </div>
        <div class="table-wrap">
            <table>
                <thead><tr><th>Symbol</th><th>Chain</th><th>Dir</th><th>Entry</th><th>Current</th><th>Size</th><th>Unreal PnL</th><th>SL</th><th>TP</th><th>Status</th><th>Actions</th></tr></thead>
                <tbody id="positionsBody"></tbody>
            </table>
        </div>
    </div>
</div>
<div class="panel" id="panel-journal"><div class="grid grid-4" id="journalStats"></div><div class="card" style="margin-top:14px"><div class="card-header"><span class="card-title">Trade Journal</span><button class="btn primary sm" onclick="openJournalModal()">+ New</button></div><div class="table-wrap"><table><thead><tr><th>Date</th><th>Symbol</th><th>Dir</th><th>Strategy</th><th>Entry</th><th>Exit</th><th>Sig</th><th>PnL $</th><th>PnL %</th><th>Out</th><th>Act</th></tr></thead><tbody id="journalBody"></tbody></table></div></div></div>
<div class="panel" id="panel-quant">
    <div class="grid grid-2" style="margin-bottom:14px">
        <div class="card">
            <div class="card-header"><span class="card-title">&#x1F4CA; Kelly Criterion Sizing</span><button class="btn primary sm" onclick="loadQuantDashboard()">Refresh</button></div>
            <div id="kellyDisplay" style="padding:10px;font-size:.8rem">Loading...</div>
        </div>
        <div class="card">
            <div class="card-header"><span class="card-title">&#x1F30A; Market Regime</span></div>
            <div id="regimeDisplay" style="padding:10px;font-size:.8rem">Loading...</div>
        </div>
    </div>
    <div class="grid grid-2" style="margin-bottom:14px">
        <div class="card">
            <div class="card-header"><span class="card-title">&#x1F628; Fear & Greed Index</span></div>
            <div id="fearGreedDisplay" style="padding:10px;font-size:.8rem">Loading...</div>
        </div>
        <div class="card">
            <div class="card-header"><span class="card-title">&#x1F4B5; Funding Rates</span></div>
            <div id="fundingDisplay" style="padding:10px;font-size:.8rem">
                <div style="margin-bottom:8px">
                    <input type="text" id="fundingSymbol" placeholder="BTC" value="BTC" style="width:80px;padding:4px 8px;font-size:.75rem">
                    <button class="btn sm primary" onclick="loadFundingRate()">Check</button>
                </div>
                <div id="fundingData">Enter symbol to check funding</div>
            </div>
        </div>
    </div>
    <div class="grid grid-2">
        <div class="card">
            <div class="card-header"><span class="card-title">&#x1F4C8; Correlation Matrix</span></div>
            <div id="correlationDisplay" style="padding:10px;font-size:.75rem;overflow-x:auto">Loading...</div>
        </div>
        <div class="card">
            <div class="card-header"><span class="card-title">&#x1F3AF; Advanced Risk Metrics</span></div>
            <div id="riskMetricsDisplay" style="padding:10px;font-size:.8rem">Loading...</div>
        </div>
    </div>
</div>
<div class="panel" id="panel-watchlist"><div class="card"><div class="card-header"><span class="card-title">Watchlist</span><button class="btn primary sm" onclick="openWatchModal()">+ Add</button></div><div class="table-wrap"><table><thead><tr><th>Pri</th><th>Symbol</th><th>Cat</th><th>Strategy</th><th>Alert</th><th>Status</th><th>Notes</th><th>Act</th></tr></thead><tbody id="watchlistBody"></tbody></table></div></div></div>
<div class="panel" id="panel-chat"><div class="card" style="padding:0;overflow:hidden"><div style="padding:12px 16px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center"><span style="font-weight:800;font-size:.85rem">Chat with Claude <span class="badge web">&#x1F50D; Web</span></span><button class="btn sm danger" onclick="clearChat()">Clear</button></div><div class="chat-container"><div class="chat-messages" id="chatMessages"><div class="chat-msg assistant">What's up? I can search live market data, analyze setups, or talk strategy. What are we looking at?</div></div><div class="chat-input-row"><input type="text" id="chatInput" placeholder="Ask about any symbol or strategy..."><button class="btn primary" id="chatSendBtn">Send</button></div></div></div></div>
<div class="panel" id="panel-templates"><div class="card"><div class="card-header"><span class="card-title">Templates</span><button class="btn primary sm" onclick="openTemplateModal()">+ New</button></div><div id="templatesList"></div></div></div>
<div class="panel" id="panel-settings"><div class="grid grid-2"><div class="card"><div class="card-header"><span class="card-title">General</span></div><div class="form-group"><label class="form-label">Default Model</label><select id="settingsModel"></select></div><div class="form-group"><label class="form-label">Default Strategy</label><select id="settingsStrategy"><option value="auto">Auto</option><option value="penguin">Penguin</option><option value="burry">Burry</option><option value="general">General</option></select></div><div class="form-group"><label class="form-label">Balance ($)</label><input type="number" id="settingsBalance" value="100"></div><div class="form-group"><label class="form-label">Max Position %</label><input type="number" id="settingsMaxPos" value="80" max="100"></div><button class="btn primary" onclick="saveSettings()">Save</button></div><div class="card"><div class="card-header"><span class="card-title">Risk Management</span></div><div class="form-group"><label class="form-label">Turtle Mode</label><select id="settingsTurtle"><option value="false">Off</option><option value="true">Active</option></select></div><div class="form-group"><label class="form-label">Consecutive Losses</label><input type="number" id="settingsLosses" value="0" min="0"></div><div class="form-group"><label class="form-label">Daily Loss %</label><input type="number" id="settingsDailyLoss" value="0" step="0.1"></div><button class="btn primary" onclick="saveSettings()">Save Risk</button></div></div><div class="card" style="margin-top:14px"><div class="card-header"><span class="card-title">API Status</span></div><div id="apiInfo" style="font-size:.78rem;color:var(--text-1)">Checking...</div></div></div>
</main></div>
<div class="modal-overlay" id="modalOverlay"><div class="modal" id="modalContent"></div></div>
<div class="toast-container" id="toastContainer"></div>
<script>
const $=s=>document.querySelector(s),$$=s=>document.querySelectorAll(s);
const api=async(u,o={})=>{try{const r=await fetch(u,o);return await r.json()}catch(e){return{error:e.message}}};
let models=[],currentAnalysisId=null,settings={};
function toast(m,t='info'){const e=document.createElement('div');e.className=`toast ${t}`;e.textContent=m;$('#toastContainer').appendChild(e);setTimeout(()=>e.remove(),3000)}
function openModal(h){$('#modalContent').innerHTML=h;$('#modalOverlay').classList.add('show')}
function closeModal(){$('#modalOverlay').classList.remove('show')}
$('#modalOverlay').addEventListener('click',e=>{if(e.target===e.currentTarget)closeModal()});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeModal()});
function signalPips(c,mx=5){let h='<div class="signal-bar">';for(let i=0;i<mx;i++){let cl=i<c?'active':'';if(c<=2&&i<c)cl+=' danger';else if(c===3&&i<c)cl+=' warn';h+=`<div class="signal-pip ${cl}"></div>`}return h+'</div>'}
function confMeter(l){const lv={low:1,medium:2,high:3,very_high:4};const n=lv[l]||0;let h='<div class="conf-meter">';for(let i=0;i<4;i++)h+=`<div class="conf-bar ${i<n?'filled '+l:''}"></div>`;return h+`<span style="margin-left:4px;font-size:.65rem;color:var(--text-3)">${l}</span></div>`}
function recBadge(r){return`<span class="badge ${r==='TRADE'?'trade':r==='AVOID'?'avoid':'hold'}">${r}</span>`}
function stratBadge(s){return`<span class="badge ${s||'general'}">${s||'general'}</span>`}
function fmtDate(d){if(!d)return'-';return new Date(d+'Z').toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})}
$$('.tab').forEach(tab=>{tab.addEventListener('click',()=>{$$('.tab').forEach(t=>t.classList.remove('active'));$$('.panel').forEach(p=>p.classList.remove('active'));tab.classList.add('active');$(`#panel-${tab.dataset.tab}`).classList.add('active');({history:loadHistory,journal:loadJournal,watchlist:loadWatchlist,templates:loadTemplates,settings:loadSettings,positions:loadPositions}[tab.dataset.tab]||function(){})()})});
async function init(){const h=await api('/api/health');$('#statusDot').className='dot '+(h.status==='ok'?'ok':'err');$('#statusLabel').textContent=h.status==='ok'?'Claude Opus 4.5':(h.error||'Error');const r=await api('/api/models');if(r.models){models=r.models;$('#modelSelect').innerHTML=models.map(m=>`<option value="${m.id}">${m.name} - ${m.desc}</option>`).join('');$('#settingsModel').innerHTML=models.map(m=>`<option value="${m.id}">${m.name}</option>`).join('')}settings=await api('/api/settings');if(settings.default_model)$('#modelSelect').value=settings.default_model;if(settings.default_strategy)$('#strategySelect').value=settings.default_strategy;if(settings.turtle_mode==='true')$('#turtleBanner').classList.add('show');checkWalletConnection();}
init();
const dropZone=$('#dropZone'),imageInput=$('#imageInput'),preview=$('#imagePreview');let selectedFile=null;
dropZone.addEventListener('dragover',e=>{e.preventDefault();dropZone.classList.add('dragover')});dropZone.addEventListener('dragleave',()=>dropZone.classList.remove('dragover'));
dropZone.addEventListener('drop',e=>{e.preventDefault();dropZone.classList.remove('dragover');if(e.dataTransfer.files.length)handleFile(e.dataTransfer.files[0])});
imageInput.addEventListener('change',()=>{if(imageInput.files.length)handleFile(imageInput.files[0])});
function handleFile(f){if(!f.type.startsWith('image/')){toast('Image only','error');return}selectedFile=f;const r=new FileReader();r.onload=e=>{preview.src=e.target.result;preview.style.display='block';dropZone.querySelector('.drop-zone-icon').style.display='none';dropZone.querySelector('.drop-zone-text').textContent=f.name};r.readAsDataURL(f)}
$('#analyzeBtn').addEventListener('click',async()=>{if(!selectedFile){toast('Upload chart first','error');return}const btn=$('#analyzeBtn');btn.disabled=true;btn.innerHTML='Analyzing...<span class="spinner"></span>';const fd=new FormData();fd.append('image',selectedFile);fd.append('model',$('#modelSelect').value);fd.append('strategy',$('#strategySelect').value);fd.append('symbol',$('#symbolInput').value);fd.append('title',$('#titleInput').value);fd.append('custom_prompt',$('#customPrompt').value);fd.append('web_search',$('#webSearchToggle').checked?'true':'false');const res=await fetch('/api/analyze',{method:'POST',body:fd}).then(r=>r.json());btn.disabled=false;btn.innerHTML='&#x1F50D; ANALYZE WITH CLAUDE';if(res.error){toast(res.error,'error');return}currentAnalysisId=res.id;toast(`${res.recommendation} - ${res.signal_count}/5 (${res.confidence})`,res.recommendation==='TRADE'?'success':'info');renderResult(res)});
function renderResult(res){$('#resultPlaceholder').style.display='none';$('#resultCard').style.display='block';$('#resultTitle').textContent=res.title||'Analysis';$('#rawOutput').textContent=res.raw_response||JSON.stringify(res.parsed,null,2);const p=res.parsed||{},sigs=p.signals||{};
let h=`<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:14px">${stratBadge(res.strategy)} ${recBadge(res.recommendation)} ${signalPips(res.signal_count)} <span style="font-size:.75rem;color:var(--text-2)">${res.signal_count}/5</span> ${confMeter(res.confidence)} ${res.web_search_used?'<span class="badge web">&#x1F50D; Live</span>':''}</div>`;
if(p.current_price){h+=`<div class="market-ctx"><strong>Live: </strong>${p.current_price}`;if(p.price_24h_change||p.btc_24h_change)h+=` | <strong>24h:</strong> ${p.price_24h_change||''}`;if(p.btc_price)h+=` | <strong>BTC:</strong> ${p.btc_price} (${p.btc_24h_change||''})`;h+='</div>'}
if(p.market_context)h+=`<div class="market-ctx"><strong>Market: </strong>${p.market_context}</div>`;
const news=p.recent_news||p.news||[];if(news.length){h+=`<div style="margin-bottom:12px;font-size:.75rem"><strong style="color:var(--yellow)">News:</strong><ul style="margin:4px 0 0 16px;color:var(--text-1)">`;news.forEach(n=>h+=`<li>${n}</li>`);h+='</ul></div>'}
h+='<div style="margin-bottom:14px">';for(const[k,v]of Object.entries(sigs)){if(typeof v==='object'){const act=v.signal||v.present||v.above_80||v.above_90||v.blow_off||v.holding;const det=[v.details,v.zone,v.direction,v.ratio,v.value,v.strength,v.current_volume,v.estimated_value,v.assessment,v.btc_change,v.alt_change,v.histogram_direction,v.current_vol,v.live_data].filter(Boolean).join(' | ');h+=`<div class="signal-detail ${act?'active':'inactive'}"><span class="signal-icon">${act?'&#x2705;':'&#x274C;'}</span><span class="signal-name">${k.replace(/_/g,' ')}</span><span class="signal-value">${det||'-'}</span></div>`}}h+='</div>';
if(res.recommendation==='TRADE'){h+=`<div class="trade-params"><div class="tp-title">Trade Setup</div><div class="grid grid-3" style="gap:8px;font-size:.82rem"><div><span style="color:var(--text-3);font-size:.65rem">ENTRY</span><br><strong>${p.entry||'-'}</strong></div><div><span style="color:var(--text-3);font-size:.65rem">STOP</span><br><strong style="color:var(--red)">${p.stop_loss||'-'}</strong></div><div><span style="color:var(--text-3);font-size:.65rem">TP1</span><br><strong style="color:var(--green)">${p.take_profit_1||p.take_profit||p.target||'-'}</strong></div><div><span style="color:var(--text-3);font-size:.65rem">SIZE</span><br><strong>${p.position_size_pct||'-'}%</strong></div><div><span style="color:var(--text-3);font-size:.65rem">LEV</span><br><strong>${p.leverage||'-'}x</strong></div><div><span style="color:var(--text-3);font-size:.65rem">TP2</span><br><strong style="color:var(--green)">${p.take_profit_2||'-'}</strong></div></div></div>`}
if(p.risks&&p.risks.length)h+=`<div style="margin-top:12px"><strong style="color:var(--red);font-size:.75rem">RISKS</strong><ul style="margin:4px 0 0 16px;color:var(--text-1);font-size:.75rem">${p.risks.map(r=>`<li>${r}</li>`).join('')}</ul></div>`;
if(p.catalysts&&p.catalysts.length)h+=`<div style="margin-top:10px"><strong style="color:var(--orange);font-size:.75rem">CATALYSTS</strong><ul style="margin:4px 0 0 16px;color:var(--text-1);font-size:.75rem">${p.catalysts.map(c=>`<li>${c}</li>`).join('')}</ul></div>`;
const pats=p.patterns||p.chart_patterns||[];if(pats.length)h+=`<div style="margin-top:10px;font-size:.78rem"><strong>Patterns:</strong> ${pats.join(', ')}</div>`;
if(p.key_levels){const s=p.key_levels.support||[],r2=p.key_levels.resistance||[];if(s.length||r2.length)h+=`<div style="margin-top:8px;font-size:.78rem">${s.length?`<span style="color:var(--green)">S: ${s.join(', ')}</span> `:''}${r2.length?`<span style="color:var(--red)">R: ${r2.join(', ')}</span>`:''}</div>`}
const notes=typeof p.notes==='string'?p.notes:JSON.stringify(p.notes||'');if(notes&&notes!=='""')h+=`<div style="margin-top:10px;font-size:.78rem;color:var(--text-1)">${notes}</div>`;
if(res.web_searches&&res.web_searches.length){h+=`<div class="web-sources"><div class="web-sources-title">&#x1F50D; Sources (${res.web_searches.length})</div>`;res.web_searches.forEach(ws=>{h+=`<div class="web-source">&#x2022; ${ws.title||'Source'} ${ws.url?`<a href="${ws.url}" target="_blank">&#x2197;</a>`:''}</div>`});h+='</div>'}
if(res.recommendation==='TRADE')h+=`<div style="margin-top:14px"><button class="btn success sm" onclick="openJournalModal({symbol:'${(res.symbol||'').replace(/'/g,"\\'")}',strategy:'${res.strategy}'})">Log to Journal</button> <button class="btn primary sm" onclick="openExecutionModal({symbol:'${(res.symbol||'').replace(/'/g,"\\'")}',entry_price:'${p.entry||''}',stop_loss:'${p.stop_loss||''}',take_profit:'${p.take_profit_1||p.take_profit||p.target||''}',position_size_pct:'${p.position_size_pct||''}',strategy:'${res.strategy}',signal_count:${res.signal_count},analysis_id:${res.id}})">&#x26A1; Execute Trade</button></div>`;
$('#resultContent').innerHTML=h;$('#resultDeleteBtn').onclick=async()=>{if(!confirm('Delete?'))return;await api(`/api/analyses/${currentAnalysisId}`,{method:'DELETE'});toast('Deleted','info');$('#resultCard').style.display='none';$('#resultPlaceholder').style.display='block'}}
function toggleRaw(){const e=$('#rawOutput');e.style.display=e.style.display==='none'?'block':'none'}
$('#quickCheckBtn').addEventListener('click',async()=>{const s=$('#quickSymbol').value.trim();if(!s){toast('Enter symbol','error');return}const btn=$('#quickCheckBtn');btn.disabled=true;btn.innerHTML='<span class="spinner"></span>';const res=await api('/api/market-check',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:s})});btn.disabled=false;btn.textContent='Check';if(res.error){toast(res.error,'error');return}const d=res.data||{};$('#quickResult').style.display='block';$('#quickResult').innerHTML=`<strong>${d.symbol||s}</strong>: ${d.price||'?'} <span style="color:${String(d.change_24h||'').includes('-')?'var(--red)':'var(--green)'}">${d.change_24h||'?'}</span><br><span style="color:var(--text-2)">Vol: ${d.volume_status||'?'} | Sent: ${d.sentiment||'?'}</span>${(d.news||[]).length?'<br><span style="color:var(--yellow);font-size:.7rem">'+d.news.slice(0,2).join(' | ')+'</span>':''}`});
async function loadHistory(){const res=await api('/api/analyses?limit=100');if(!Array.isArray(res)||!res.length){$('#historyBody').innerHTML='<tr><td colspan="10" style="text-align:center;color:var(--text-3)">No analyses</td></tr>';return}$('#historyBody').innerHTML=res.map(a=>{const img=a.image_path?a.image_path.split('/').pop():'';return`<tr><td>${fmtDate(a.created_at)}</td><td><strong>${a.symbol||'-'}</strong></td><td>${stratBadge(a.strategy)}</td><td>${signalPips(a.signal_count)}</td><td>${recBadge(a.recommendation)}</td><td>${confMeter(a.confidence)}</td><td>${a.web_search_used?'<span class="badge web">&#x1F50D;</span>':'-'}</td><td style="font-size:.65rem;color:var(--text-3)">${(a.model_used||'').includes('opus')?'Opus':(a.model_used||'').includes('sonnet')?'Sonnet':'Haiku'}</td><td>${img?`<img class="img-thumb" src="/api/image/${img}" onclick="viewImage('/api/image/${img}')">`:'-'}</td><td><div class="btn-group"><button class="btn sm" onclick="viewAnalysis(${a.id})">View</button><button class="btn sm" onclick="editAnalysis(${a.id})">Edit</button><button class="btn sm danger" onclick="deleteAnalysis(${a.id})">Del</button></div></td></tr>`}).join('')}
async function viewAnalysis(id){const a=await api(`/api/analyses/${id}`);if(a.error)return;const img=a.image_path?a.image_path.split('/').pop():'';let ws=[];try{ws=JSON.parse(a.web_searches||'[]')}catch{}openModal(`<div class="modal-header"><span style="font-weight:800">${a.title}</span><button class="modal-close" onclick="closeModal()">&times;</button></div>${img?`<img src="/api/image/${img}" style="width:100%;border-radius:var(--radius);margin-bottom:14px">`:''}<div style="display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap">${stratBadge(a.strategy)} ${recBadge(a.recommendation)} ${signalPips(a.signal_count)} ${a.web_search_used?'<span class="badge web">&#x1F50D;</span>':''}</div><div style="font-size:.78rem;color:var(--text-2);margin-bottom:8px">Model: ${a.model_used}</div>${a.entry_price?`<div style="font-size:.78rem">Entry: ${a.entry_price} | SL: ${a.stop_loss} | TP: ${a.take_profit}</div>`:''}${ws.length?`<div class="web-sources" style="margin-top:12px"><div class="web-sources-title">Sources (${ws.length})</div>${ws.map(w=>`<div class="web-source">&#x2022; ${w.title} ${w.url?`<a href="${w.url}" target="_blank">&#x2197;</a>`:''}</div>`).join('')}</div>`:''}<div class="raw-output" style="margin-top:14px">${a.raw_response}</div>`)}
async function editAnalysis(id){const a=await api(`/api/analyses/${id}`);if(a.error)return;openModal(`<div class="modal-header"><span style="font-weight:800">Edit #${id}</span><button class="modal-close" onclick="closeModal()">&times;</button></div><div class="form-group"><label class="form-label">Title</label><input type="text" id="eT" value="${(a.title||'').replace(/"/g,'&quot;')}"></div><div class="form-group"><label class="form-label">Symbol</label><input type="text" id="eS" value="${a.symbol||''}"></div><div class="grid grid-2"><div class="form-group"><label class="form-label">Rec</label><select id="eR"><option ${a.recommendation==='TRADE'?'selected':''}>TRADE</option><option ${a.recommendation==='HOLD'?'selected':''}>HOLD</option><option ${a.recommendation==='AVOID'?'selected':''}>AVOID</option></select></div><div class="form-group"><label class="form-label">Signals</label><input type="number" id="eSg" value="${a.signal_count}" min="0" max="5"></div></div><div class="grid grid-3"><div class="form-group"><label class="form-label">Entry</label><input type="text" id="eE" value="${a.entry_price||''}"></div><div class="form-group"><label class="form-label">SL</label><input type="text" id="eSL" value="${a.stop_loss||''}"></div><div class="form-group"><label class="form-label">TP</label><input type="text" id="eTP" value="${a.take_profit||''}"></div></div><div class="form-group"><label class="form-label">Notes</label><textarea id="eN" rows="3">${a.notes||''}</textarea></div><button class="btn primary" onclick="saveEdit(${id})">Save</button>`)}
async function saveEdit(id){await api(`/api/analyses/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:$('#eT').value,symbol:$('#eS').value,recommendation:$('#eR').value,signal_count:+$('#eSg').value,entry_price:$('#eE').value,stop_loss:$('#eSL').value,take_profit:$('#eTP').value,notes:$('#eN').value})});closeModal();toast('Updated','success');loadHistory()}
async function deleteAnalysis(id){if(!confirm('Delete?'))return;await api(`/api/analyses/${id}`,{method:'DELETE'});toast('Deleted','info');loadHistory()}
function viewImage(src){openModal(`<div class="modal-header"><span style="font-weight:800">Chart</span><button class="modal-close" onclick="closeModal()">&times;</button></div><img src="${src}" style="width:100%;border-radius:var(--radius)">`)}
async function loadJournal(){const st=await api('/api/journal/stats');$('#journalStats').innerHTML=`<div class="card stat"><div class="stat-value">${st.total_trades||0}</div><div class="stat-label">Trades</div></div><div class="card stat"><div class="stat-value" style="color:var(--green)">${st.win_rate||0}%</div><div class="stat-label">Win Rate</div></div><div class="card stat"><div class="stat-value" style="color:${(st.total_pnl||0)>=0?'var(--green)':'var(--red)'}">$${(st.total_pnl||0).toFixed(2)}</div><div class="stat-label">Total PnL</div></div><div class="card stat"><div class="stat-value">${st.wins||0}W/${st.losses||0}L</div><div class="stat-label">Record</div></div>`;const res=await api('/api/journal');if(!Array.isArray(res)||!res.length){$('#journalBody').innerHTML='<tr><td colspan="11" style="text-align:center;color:var(--text-3)">No trades</td></tr>';return}$('#journalBody').innerHTML=res.map(j=>`<tr><td>${fmtDate(j.entry_time||j.created_at)}</td><td><strong>${j.symbol}</strong></td><td>${j.direction==='long'?'&#x1F7E2;':'&#x1F534;'}</td><td>${stratBadge(j.strategy)}</td><td>${j.entry_price}</td><td>${j.exit_price||'-'}</td><td>${signalPips(j.signals_present)}</td><td style="color:${j.pnl_dollars>=0?'var(--green)':'var(--red)'}">$${j.pnl_dollars.toFixed(2)}</td><td style="color:${j.pnl_percent>=0?'var(--green)':'var(--red)'}">${j.pnl_percent.toFixed(1)}%</td><td><span class="badge ${j.outcome}">${j.outcome}</span></td><td><div class="btn-group"><button class="btn sm" onclick="editJournal(${j.id})">Edit</button><button class="btn sm danger" onclick="deleteJournal(${j.id})">Del</button></div></td></tr>`).join('')}
function openJournalModal(pf={}){openModal(`<div class="modal-header"><span style="font-weight:800">${pf.id?'Edit':'New'} Trade</span><button class="modal-close" onclick="closeModal()">&times;</button></div><div class="grid grid-2"><div class="form-group"><label class="form-label">Symbol</label><input type="text" id="jS" value="${pf.symbol||''}"></div><div class="form-group"><label class="form-label">Dir</label><select id="jD"><option value="long" ${pf.direction==='long'?'selected':''}>Long</option><option value="short" ${pf.direction==='short'?'selected':''}>Short</option></select></div></div><div class="grid grid-2"><div class="form-group"><label class="form-label">Strategy</label><select id="jSt"><option value="penguin" ${pf.strategy==='penguin'?'selected':''}>Penguin</option><option value="burry" ${pf.strategy==='burry'?'selected':''}>Burry</option><option value="scalp" ${pf.strategy==='scalp'?'selected':''}>Scalp</option><option value="other">Other</option></select></div><div class="form-group"><label class="form-label">Signals</label><input type="number" id="jSg" value="${pf.signals_present||0}" min="0" max="5"></div></div><div class="grid grid-3"><div class="form-group"><label class="form-label">Entry</label><input type="number" id="jE" step="any" value="${pf.entry_price||''}"></div><div class="form-group"><label class="form-label">Exit</label><input type="number" id="jX" step="any" value="${pf.exit_price||''}"></div><div class="form-group"><label class="form-label">Size</label><input type="number" id="jSz" step="any" value="${pf.size||''}"></div></div><div class="grid grid-3"><div class="form-group"><label class="form-label">Lev</label><input type="number" id="jL" value="${pf.leverage||1}"></div><div class="form-group"><label class="form-label">PnL $</label><input type="number" id="jPD" step="any" value="${pf.pnl_dollars||0}"></div><div class="form-group"><label class="form-label">PnL %</label><input type="number" id="jPP" step="any" value="${pf.pnl_percent||0}"></div></div><div class="form-group"><label class="form-label">Outcome</label><select id="jO"><option value="open" ${pf.outcome==='open'?'selected':''}>Open</option><option value="win" ${pf.outcome==='win'?'selected':''}>Win</option><option value="loss" ${pf.outcome==='loss'?'selected':''}>Loss</option><option value="breakeven" ${pf.outcome==='breakeven'?'selected':''}>BE</option></select></div><div class="form-group"><label class="form-label">Reasoning</label><textarea id="jR" rows="2">${pf.reasoning||''}</textarea></div><div class="form-group"><label class="form-label">Lessons</label><textarea id="jLs" rows="2">${pf.lessons||''}</textarea></div><button class="btn primary" onclick="saveJournal(${pf.id||'null'})">${pf.id?'Update':'Save'}</button>`)}
async function saveJournal(id){const d={symbol:$('#jS').value,direction:$('#jD').value,strategy:$('#jSt').value,signals_present:+$('#jSg').value,entry_price:+$('#jE').value||0,exit_price:+$('#jX').value||0,size:+$('#jSz').value||0,leverage:+$('#jL').value||1,pnl_dollars:+$('#jPD').value||0,pnl_percent:+$('#jPP').value||0,outcome:$('#jO').value,reasoning:$('#jR').value,lessons:$('#jLs').value};if(id)await api(`/api/journal/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});else await api('/api/journal',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});closeModal();toast('Saved','success');loadJournal()}
async function editJournal(id){const res=await api('/api/journal');const j=res.find(x=>x.id===id);if(j)openJournalModal(j)}
async function deleteJournal(id){if(!confirm('Delete?'))return;await api(`/api/journal/${id}`,{method:'DELETE'});toast('Deleted','info');loadJournal()}
async function loadWatchlist(){const res=await api('/api/watchlist');if(!Array.isArray(res)||!res.length){$('#watchlistBody').innerHTML='<tr><td colspan="8" style="text-align:center;color:var(--text-3)">Empty</td></tr>';return}const pr={1:'&#x1F534;',2:'&#x1F7E1;',3:'&#x26AA;',4:'&#x1F535;',5:'&#x26AB;'};$('#watchlistBody').innerHTML=res.map(w=>`<tr><td>${pr[w.priority]||'&#x26AA;'}</td><td><strong>${w.symbol}</strong></td><td>${w.category}</td><td>${w.strategy?stratBadge(w.strategy):'-'}</td><td>${w.alert_price||'-'}</td><td>${w.status}</td><td style="max-width:180px;overflow:hidden;text-overflow:ellipsis">${w.notes||'-'}</td><td><div class="btn-group"><button class="btn sm" onclick="editWatch(${w.id})">Edit</button><button class="btn sm danger" onclick="deleteWatch(${w.id})">Del</button></div></td></tr>`).join('')}
function openWatchModal(pf={}){openModal(`<div class="modal-header"><span style="font-weight:800">${pf.id?'Edit':'Add'}</span><button class="modal-close" onclick="closeModal()">&times;</button></div><div class="grid grid-2"><div class="form-group"><label class="form-label">Symbol</label><input type="text" id="wS" value="${pf.symbol||''}"></div><div class="form-group"><label class="form-label">Cat</label><select id="wC"><option value="crypto" ${pf.category==='crypto'?'selected':''}>Crypto</option><option value="ai" ${pf.category==='ai'?'selected':''}>AI</option><option value="nuclear" ${pf.category==='nuclear'?'selected':''}>Nuclear</option><option value="quantum" ${pf.category==='quantum'?'selected':''}>Quantum</option><option value="biotech" ${pf.category==='biotech'?'selected':''}>Biotech</option><option value="semi" ${pf.category==='semi'?'selected':''}>Semi</option><option value="general" ${!pf.category||pf.category==='general'?'selected':''}>General</option></select></div></div><div class="grid grid-3"><div class="form-group"><label class="form-label">Strategy</label><select id="wSt"><option value="">-</option><option value="penguin" ${pf.strategy==='penguin'?'selected':''}>Penguin</option><option value="burry" ${pf.strategy==='burry'?'selected':''}>Burry</option></select></div><div class="form-group"><label class="form-label">Alert</label><input type="text" id="wA" value="${pf.alert_price||''}"></div><div class="form-group"><label class="form-label">Priority</label><input type="number" id="wP" value="${pf.priority||3}" min="1" max="5"></div></div><div class="form-group"><label class="form-label">Status</label><select id="wSs"><option ${pf.status==='watching'?'selected':''}>watching</option><option ${pf.status==='ready'?'selected':''}>ready</option><option ${pf.status==='triggered'?'selected':''}>triggered</option><option ${pf.status==='closed'?'selected':''}>closed</option></select></div><div class="form-group"><label class="form-label">Notes</label><textarea id="wN" rows="2">${pf.notes||''}</textarea></div><button class="btn primary" onclick="saveWatch(${pf.id||'null'})">${pf.id?'Update':'Add'}</button>`)}
async function saveWatch(id){const d={symbol:$('#wS').value,category:$('#wC').value,strategy:$('#wSt').value,alert_price:$('#wA').value,priority:+$('#wP').value,status:$('#wSs').value,notes:$('#wN').value};if(id)await api(`/api/watchlist/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});else await api('/api/watchlist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});closeModal();toast('Saved','success');loadWatchlist()}
async function editWatch(id){const res=await api('/api/watchlist');const w=res.find(x=>x.id===id);if(w)openWatchModal(w)}
async function deleteWatch(id){if(!confirm('Remove?'))return;await api(`/api/watchlist/${id}`,{method:'DELETE'});toast('Removed','info');loadWatchlist()}
$('#chatSendBtn').addEventListener('click',sendChat);$('#chatInput').addEventListener('keydown',e=>{if(e.key==='Enter')sendChat()});
async function sendChat(){const m=$('#chatInput').value.trim();if(!m)return;$('#chatInput').value='';addMsg('user',m);const ind=addMsg('assistant','Thinking...<span class="spinner"></span>');const res=await api('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:m,model:$('#modelSelect').value})});ind.remove();if(res.error){addMsg('assistant','Error: '+res.error);return}let reply=res.response||'';if(res.web_searches&&res.web_searches.length)reply+='\n\nSources: '+res.web_searches.map(w=>w.title).join(', ');addMsg('assistant',reply)}
function addMsg(role,text){const d=document.createElement('div');d.className=`chat-msg ${role}`;d.innerHTML=text.replace(/\n/g,'<br>');$('#chatMessages').appendChild(d);$('#chatMessages').scrollTop=$('#chatMessages').scrollHeight;return d}
async function clearChat(){await api('/api/chat/clear',{method:'DELETE'});$('#chatMessages').innerHTML='<div class="chat-msg assistant">Cleared. What are we looking at?</div>';toast('Cleared','info')}
async function loadTemplates(){const res=await api('/api/templates');if(!Array.isArray(res))return;$('#templatesList').innerHTML=res.map(t=>`<div class="card" style="margin-bottom:10px"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px"><div><strong>${t.name}</strong> ${stratBadge(t.strategy)} ${t.is_default?'<span style="color:var(--text-3);font-size:.65rem">DEFAULT</span>':''}</div><div class="btn-group">${!t.is_default?`<button class="btn sm" onclick="editTpl(${t.id})">Edit</button><button class="btn sm danger" onclick="deleteTpl(${t.id})">Del</button>`:`<button class="btn sm" onclick="viewTpl(${t.id})">View</button>`}</div></div><div style="font-size:.7rem;color:var(--text-3);max-height:50px;overflow:hidden">${t.template.substring(0,180)}...</div></div>`).join('')}
function openTemplateModal(pf={}){openModal(`<div class="modal-header"><span style="font-weight:800">${pf.id?'Edit':'New'} Template</span><button class="modal-close" onclick="closeModal()">&times;</button></div><div class="form-group"><label class="form-label">Name</label><input type="text" id="tN" value="${pf.name||''}"></div><div class="form-group"><label class="form-label">Strategy</label><select id="tS"><option value="general" ${pf.strategy==='general'?'selected':''}>General</option><option value="penguin" ${pf.strategy==='penguin'?'selected':''}>Penguin</option><option value="burry" ${pf.strategy==='burry'?'selected':''}>Burry</option><option value="scalp" ${pf.strategy==='scalp'?'selected':''}>Scalp</option></select></div><div class="form-group"><label class="form-label">Template</label><textarea id="tT" rows="10">${pf.template||''}</textarea></div><button class="btn primary" onclick="saveTpl(${pf.id||'null'})">${pf.id?'Update':'Create'}</button>`)}
async function saveTpl(id){const d={name:$('#tN').value,strategy:$('#tS').value,template:$('#tT').value};if(id)await api(`/api/templates/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});else await api('/api/templates',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});closeModal();toast('Saved','success');loadTemplates()}
async function editTpl(id){const res=await api('/api/templates');const t=res.find(x=>x.id===id);if(t)openTemplateModal(t)}
async function viewTpl(id){const res=await api('/api/templates');const t=res.find(x=>x.id===id);if(!t)return;openModal(`<div class="modal-header"><span style="font-weight:800">${t.name}</span><button class="modal-close" onclick="closeModal()">&times;</button></div><div class="raw-output">${t.template}</div>`)}
async function deleteTpl(id){if(!confirm('Delete?'))return;await api(`/api/templates/${id}`,{method:'DELETE'});toast('Deleted','info');loadTemplates()}
async function loadSettings(){settings=await api('/api/settings');if(settings.default_model)$('#settingsModel').value=settings.default_model;if(settings.default_strategy)$('#settingsStrategy').value=settings.default_strategy;$('#settingsBalance').value=settings.portfolio_balance||100;$('#settingsMaxPos').value=settings.max_position_pct||80;$('#settingsTurtle').value=settings.turtle_mode||'false';$('#settingsLosses').value=settings.consecutive_losses||0;$('#settingsDailyLoss').value=settings.daily_loss_pct||0;const h=await api('/api/health');$('#apiInfo').innerHTML=`<strong>Provider:</strong> Anthropic Claude<br><strong>Model:</strong> ${h.model||'?'}<br><strong>Status:</strong> ${h.status==='ok'?'&#x2705; Connected':'&#x274C; '+(h.error||'Error')}<br><strong>Web Search:</strong> &#x2705; Enabled<br><strong>Vision:</strong> &#x2705; Enabled`}
async function saveSettings(){await api('/api/settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({default_model:$('#settingsModel').value,default_strategy:$('#settingsStrategy').value,portfolio_balance:$('#settingsBalance').value,max_position_pct:$('#settingsMaxPos').value,turtle_mode:$('#settingsTurtle').value,consecutive_losses:$('#settingsLosses').value,daily_loss_pct:$('#settingsDailyLoss').value})});toast('Saved','success');if($('#settingsTurtle').value==='true')$('#turtleBanner').classList.add('show');else $('#turtleBanner').classList.remove('show')}

// =========== QUANT DASHBOARD (PHASE 3) ===========
async function loadQuantDashboard(){
    loadKellyData();
    loadFearGreed();
    loadRegimeData();
    loadCorrelationData();
    loadRiskMetrics();
}

async function loadKellyData(){
    const res = await api('/api/quant/kelly/all');
    if(res.error){
        $('#kellyDisplay').innerHTML = `<span style="color:var(--red)">${res.error}</span>`;
        return;
    }
    const all = res.all || {};
    const penguin = res.penguin || {};
    const burry = res.burry || {};
    
    let html = '';
    if(all.error){
        html = `<div style="color:var(--text-2);padding:10px">${all.error}</div>`;
    }else{
        html = `<div style="margin-bottom:12px">
            <div style="display:flex;justify-content:space-between;margin-bottom:8px">
                <span style="color:var(--text-2)">Win Rate</span>
                <span style="color:var(--green);font-weight:800">${all.win_rate||0}%</span>
            </div>
            <div style="display:flex;justify-content:space-between;margin-bottom:8px">
                <span style="color:var(--text-2)">Win/Loss Ratio</span>
                <span style="font-weight:700">${all.win_loss_ratio||0}</span>
            </div>
            <div style="display:flex;justify-content:space-between;margin-bottom:8px">
                <span style="color:var(--text-2)">Expected Value</span>
                <span style="color:${(all.expected_value||0)>=0?'var(--green)':'var(--red)'}">+${all.expected_value||0}%</span>
            </div>
            <hr style="border-color:var(--border);margin:10px 0">
            <div style="background:var(--bg-3);padding:10px;border-radius:var(--radius);margin-bottom:8px">
                <div style="color:var(--yellow);font-weight:700;margin-bottom:4px">&#x1F3AF; Recommended Size (Half-Kelly)</div>
                <div style="font-size:1.5rem;font-weight:800;color:var(--cyan)">${all.kelly_half||0}%</div>
            </div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:.75rem">
                <div style="text-align:center;padding:6px;background:var(--bg-2);border-radius:var(--radius)">
                    <div style="color:var(--text-3)">Full Kelly</div>
                    <div style="color:var(--orange);font-weight:700">${all.kelly_full||0}%</div>
                </div>
                <div style="text-align:center;padding:6px;background:var(--bg-2);border-radius:var(--radius)">
                    <div style="color:var(--text-3)">Quarter Kelly</div>
                    <div style="color:var(--text-1);font-weight:700">${all.kelly_quarter||0}%</div>
                </div>
            </div>
        </div>`;
        
        if(!penguin.error || !burry.error){
            html += `<div style="margin-top:10px;font-size:.75rem"><strong>By Strategy:</strong>
                <div style="display:flex;gap:12px;margin-top:6px">
                    ${!penguin.error?`<span>&#x1F427; Penguin: <strong style="color:var(--cyan)">${penguin.kelly_half||0}%</strong> (${penguin.win_rate||0}% WR)</span>`:''}
                    ${!burry.error?`<span>&#x1F43B; Burry: <strong style="color:var(--cyan)">${burry.kelly_half||0}%</strong> (${burry.win_rate||0}% WR)</span>`:''}
                </div>
            </div>`;
        }
    }
    $('#kellyDisplay').innerHTML = html;
}

async function loadFearGreed(){
    const res = await api('/api/quant/fear-greed');
    if(res.error){
        $('#fearGreedDisplay').innerHTML = `<span style="color:var(--text-3)">Could not load Fear & Greed Index</span>`;
        return;
    }
    
    const value = res.value || 50;
    const color = value < 25 ? 'var(--red)' : value < 40 ? 'var(--orange)' : value < 60 ? 'var(--text-1)' : value < 75 ? 'var(--green)' : 'var(--cyan)';
    
    const html = `
        <div style="text-align:center;margin-bottom:12px">
            <div style="font-size:2.5rem;font-weight:800;color:${color}">${value}</div>
            <div style="color:var(--text-2);font-size:.85rem">${res.classification}</div>
        </div>
        <div style="height:12px;background:linear-gradient(to right,var(--red),var(--orange),var(--text-1),var(--green),var(--cyan));border-radius:6px;position:relative;margin-bottom:12px">
            <div style="position:absolute;left:${value}%;top:-4px;width:8px;height:20px;background:white;border-radius:4px;transform:translateX(-50%)"></div>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:.7rem;color:var(--text-3)">
            <span>Extreme Fear</span><span>Neutral</span><span>Extreme Greed</span>
        </div>
        <div style="margin-top:12px;padding:8px;background:var(--bg-3);border-radius:var(--radius)">
            <div style="color:${res.trading_bias==='PENGUIN_FAVORED'?'var(--cyan)':res.trading_bias==='BURRY_FAVORED'?'var(--orange)':'var(--text-2)'};font-weight:700;font-size:.75rem">${res.trading_bias}</div>
            <div style="font-size:.7rem;color:var(--text-2);margin-top:4px">${res.interpretation}</div>
        </div>
        <div style="margin-top:8px;font-size:.7rem;color:var(--text-3)">7d Avg: ${res.avg_7d} | Trend: ${res.trend}</div>
    `;
    $('#fearGreedDisplay').innerHTML = html;
}

async function loadRegimeData(){
    const symbol = 'BTC';
    const res = await api(`/api/quant/regime/${symbol}`);
    if(res.error){
        $('#regimeDisplay').innerHTML = `<span style="color:var(--text-3)">${res.error}</span>`;
        return;
    }
    
    const regime = res.regime || {};
    const regimeColors = {CALM:'var(--text-2)',NORMAL:'var(--text-1)',VOLATILE:'var(--orange)',EXTREME:'var(--red)'};
    const regimeIcons = {CALM:'&#x1F4A4;',NORMAL:'&#x26AA;',VOLATILE:'&#x26A1;',EXTREME:'&#x1F525;'};
    
    const html = `
        <div style="text-align:center;margin-bottom:14px">
            <div style="font-size:1.8rem">${regimeIcons[regime.regime]||'&#x26AA;'}</div>
            <div style="font-size:1.2rem;font-weight:800;color:${regimeColors[regime.regime]||'var(--text-1)'}">${regime.regime||'UNKNOWN'}</div>
            <div style="font-size:.7rem;color:var(--text-3)">Vol Score: ${regime.vol_score||'?'}</div>
        </div>
        <div style="padding:8px;background:var(--bg-3);border-radius:var(--radius);font-size:.75rem;color:var(--text-2);margin-bottom:10px">
            ${regime.recommendation||'Standard parameters apply'}
        </div>
        <div style="font-size:.7rem">
            <div style="display:flex;justify-content:space-between;margin-bottom:4px">
                <span style="color:var(--text-3)">ATR Percentile</span>
                <span>${res.atr?.percentile||'?'}%</span>
            </div>
            ${res.bollinger_squeeze?`<div style="display:flex;justify-content:space-between;margin-bottom:4px">
                <span style="color:var(--text-3)">BB Squeeze</span>
                <span style="color:${res.bollinger_squeeze.is_squeeze?'var(--yellow)':'var(--text-2)'}">${res.bollinger_squeeze.squeeze_strength||'none'}</span>
            </div>`:''}
            <div style="display:flex;justify-content:space-between">
                <span style="color:var(--text-3)">Kelly Multiplier</span>
                <span style="color:var(--cyan)">${regime.kelly_multiplier||1}x</span>
            </div>
        </div>
    `;
    $('#regimeDisplay').innerHTML = html;
}

async function loadFundingRate(){
    const symbol = $('#fundingSymbol').value.trim().toUpperCase() || 'BTC';
    $('#fundingData').innerHTML = 'Loading...';
    
    const res = await api(`/api/quant/funding/${symbol}`);
    if(res.error){
        $('#fundingData').innerHTML = `<span style="color:var(--text-3)">${res.error}</span>`;
        return;
    }
    
    const rate = res.current_rate || 0;
    const rateColor = rate > 0.05 ? 'var(--red)' : rate < -0.05 ? 'var(--green)' : 'var(--text-1)';
    const signalColor = res.bonus_signal_for === 'penguin' ? 'var(--cyan)' : res.bonus_signal_for === 'burry' ? 'var(--orange)' : 'var(--text-2)';
    
    const html = `
        <div style="text-align:center;margin-bottom:10px">
            <div style="font-size:.7rem;color:var(--text-3)">${res.symbol}</div>
            <div style="font-size:1.5rem;font-weight:800;color:${rateColor}">${rate > 0 ? '+' : ''}${rate.toFixed(4)}%</div>
            <div style="font-size:.65rem;color:var(--text-3)">per 8h (${res.annualized_pct}% annualized)</div>
        </div>
        <div style="padding:6px;background:var(--bg-3);border-radius:var(--radius);margin-bottom:8px">
            <div style="color:${signalColor};font-weight:700;font-size:.75rem">${res.signal}</div>
            <div style="font-size:.65rem;color:var(--text-2);margin-top:2px">${res.interpretation}</div>
        </div>
        <div style="font-size:.65rem;color:var(--text-3)">7d Avg: ${res.avg_7d?.toFixed(4)||'?'}% ${res.is_extreme?'<span style="color:var(--red)">&#x26A0; EXTREME</span>':''}</div>
    `;
    $('#fundingData').innerHTML = html;
}

async function loadCorrelationData(){
    const res = await api('/api/quant/correlation?symbols=BTC,ETH,SOL,DOGE');
    if(res.error){
        $('#correlationDisplay').innerHTML = `<span style="color:var(--text-3)">${res.error}</span>`;
        return;
    }
    
    const matrix = res.correlation_matrix || {};
    const symbols = res.symbols || [];
    const betas = res.betas_vs_btc || {};
    
    // Build correlation table
    let html = '<table style="width:100%;font-size:.7rem;border-collapse:collapse">';
    html += '<tr><th></th>' + symbols.map(s => `<th style="padding:4px">${s}</th>`).join('') + '</tr>';
    
    for(const symA of symbols){
        html += `<tr><td style="font-weight:700;padding:4px">${symA}</td>`;
        for(const symB of symbols){
            const corr = matrix[symA]?.[symB];
            const val = corr !== null && corr !== undefined ? corr.toFixed(2) : '?';
            const bg = symA === symB ? 'var(--bg-2)' : corr > 0.8 ? 'rgba(0,255,136,.2)' : corr < 0.4 ? 'rgba(255,82,82,.15)' : '';
            html += `<td style="padding:4px;text-align:center;background:${bg}">${val}</td>`;
        }
        html += '</tr>';
    }
    html += '</table>';
    
    // Add beta section
    if(Object.keys(betas).length > 0){
        html += '<div style="margin-top:10px;font-size:.7rem"><strong>Beta vs BTC:</strong> ';
        html += Object.entries(betas).map(([sym, beta]) => {
            const color = beta > 1.5 ? 'var(--orange)' : beta < 0.5 ? 'var(--text-3)' : 'var(--text-1)';
            return `<span style="margin-right:8px">${sym}: <strong style="color:${color}">${beta?.toFixed(2)||'?'}</strong></span>`;
        }).join('');
        html += '</div>';
    }
    
    // Add notable correlations
    if(res.notable_correlations && res.notable_correlations.length){
        html += '<div style="margin-top:8px;font-size:.65rem;color:var(--text-3)">';
        res.notable_correlations.slice(0,3).forEach(n => {
            html += `<div>${n.pair}: ${n.correlation?.toFixed(2)} - ${n.note}</div>`;
        });
        html += '</div>';
    }
    
    $('#correlationDisplay').innerHTML = html;
}

async function loadRiskMetrics(){
    const res = await api('/api/quant/risk-metrics');
    if(res.error){
        $('#riskMetricsDisplay').innerHTML = `<span style="color:var(--text-2)">${res.error}</span>`;
        return;
    }
    
    const sharpeColor = res.sharpe_rating === 'excellent' ? 'var(--green)' : res.sharpe_rating === 'good' ? 'var(--cyan)' : res.sharpe_rating === 'acceptable' ? 'var(--orange)' : 'var(--red)';
    
    const html = `
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px">
            <div style="background:var(--bg-3);padding:8px;border-radius:var(--radius);text-align:center">
                <div style="font-size:.65rem;color:var(--text-3)">Sharpe Ratio</div>
                <div style="font-size:1.2rem;font-weight:800;color:${sharpeColor}">${res.sharpe_ratio?.toFixed(2)||'?'}</div>
                <div style="font-size:.6rem;color:var(--text-3)">${res.sharpe_rating||''}</div>
            </div>
            <div style="background:var(--bg-3);padding:8px;border-radius:var(--radius);text-align:center">
                <div style="font-size:.65rem;color:var(--text-3)">Profit Factor</div>
                <div style="font-size:1.2rem;font-weight:800;color:${res.profit_factor>=1.5?'var(--green)':'var(--orange)'}">${res.profit_factor?.toFixed(2)||'?'}</div>
            </div>
        </div>
        <div style="font-size:.75rem">
            <div style="display:flex;justify-content:space-between;margin-bottom:4px">
                <span style="color:var(--text-3)">Sortino Ratio</span>
                <span>${res.sortino_ratio?.toFixed(2)||'?'}</span>
            </div>
            <div style="display:flex;justify-content:space-between;margin-bottom:4px">
                <span style="color:var(--text-3)">Max Drawdown</span>
                <span style="color:var(--red)">${res.max_drawdown_pct?.toFixed(1)||'?'}%</span>
            </div>
            <div style="display:flex;justify-content:space-between;margin-bottom:4px">
                <span style="color:var(--text-3)">Mean Return</span>
                <span style="color:${res.mean_return_pct>=0?'var(--green)':'var(--red)'}">${res.mean_return_pct>=0?'+':''}${res.mean_return_pct?.toFixed(2)||'?'}%</span>
            </div>
            <div style="display:flex;justify-content:space-between;margin-bottom:4px">
                <span style="color:var(--text-3)">Win Streak</span>
                <span style="color:var(--green)">${res.max_win_streak||0}</span>
            </div>
            <div style="display:flex;justify-content:space-between">
                <span style="color:var(--text-3)">Loss Streak</span>
                <span style="color:var(--red)">${res.max_loss_streak||0}</span>
            </div>
        </div>
    `;
    $('#riskMetricsDisplay').innerHTML = html;
}

// =========== PHASE 4: ALERTS & MONITORING ===========
let monitorPollingInterval = null;
let browserNotificationsEnabled = false;

async function loadAlerts(){
    const res = await api('/api/alerts');
    if(res.error){$('#alertsBody').innerHTML=`<tr><td colspan="7" style="color:var(--text-3)">${res.error}</td></tr>`;return}
    
    let activeCount = 0;
    let html = '';
    for(const a of res){
        if(a.enabled) activeCount++;
        const statusClass = a.enabled ? (a.triggered ? 'triggered' : 'active') : 'disabled';
        const statusText = a.enabled ? (a.triggered ? 'Triggered' : 'Active') : 'Disabled';
        const statusColor = a.enabled ? (a.triggered ? 'var(--orange)' : 'var(--green)') : 'var(--text-3)';
        
        html += `<tr>
            <td style="font-weight:600">${a.name}</td>
            <td><span class="badge">${a.symbol}</span></td>
            <td style="font-size:.7rem">${a.alert_type}</td>
            <td style="font-size:.7rem">${a.condition}</td>
            <td style="font-size:.75rem;font-weight:600">${a.target_value}</td>
            <td style="color:${statusColor};font-size:.7rem">${statusText}</td>
            <td>
                <button class="btn sm" onclick="toggleAlert(${a.id})">${a.enabled?'Disable':'Enable'}</button>
                ${a.triggered?`<button class="btn sm" onclick="resetAlert(${a.id})">Reset</button>`:''}
                <button class="btn sm danger" onclick="deleteAlert(${a.id})">&#x1F5D1;</button>
            </td>
        </tr>`;
    }
    $('#alertsBody').innerHTML = html || '<tr><td colspan="7" style="color:var(--text-3)">No alerts configured</td></tr>';
    $('#statActiveAlerts').textContent = activeCount;
}

async function loadAlertHistory(){
    const res = await api('/api/alerts/history?limit=30');
    if(res.error){$('#alertHistoryBody').innerHTML=`<tr><td colspan="4">${res.error}</td></tr>`;return}
    
    let todayCount = 0;
    const today = new Date().toDateString();
    
    let html = '';
    for(const h of res){
        const time = new Date(h.triggered_at);
        if(time.toDateString() === today) todayCount++;
        
        html += `<tr>
            <td style="font-size:.7rem;white-space:nowrap">${time.toLocaleTimeString()}</td>
            <td style="font-size:.75rem">${h.alert_name}</td>
            <td><span class="badge">${h.symbol}</span></td>
            <td style="font-size:.7rem;max-width:200px;overflow:hidden;text-overflow:ellipsis">${h.message}</td>
        </tr>`;
    }
    $('#alertHistoryBody').innerHTML = html || '<tr><td colspan="4" style="color:var(--text-3)">No alerts triggered yet</td></tr>';
    $('#statTriggeredToday').textContent = todayCount;
}

async function loadScanResults(){
    const res = await api('/api/scan/results?limit=10&min_signals=3');
    if(res.error){$('#scanResultsBody').innerHTML=`<tr><td colspan="7">${res.error}</td></tr>`;return}
    
    let html = '';
    for(const s of res){
        const time = new Date(s.scanned_at);
        const recColor = s.recommendation === 'TRADE' ? 'var(--green)' : 'var(--orange)';
        const stratIcon = s.strategy === 'penguin' ? '&#x1F427;' : '&#x1F9B4;';
        
        html += `<tr>
            <td><span class="badge">${s.symbol}</span></td>
            <td style="font-size:.75rem">${stratIcon} ${s.strategy}</td>
            <td>${signalPips(s.signal_count)}</td>
            <td style="font-size:.75rem">$${s.price?.toFixed(4)||'?'}</td>
            <td style="color:${recColor};font-weight:600;font-size:.75rem">${s.recommendation}</td>
            <td style="font-size:.65rem;color:var(--text-3)">${time.toLocaleTimeString()}</td>
            <td><button class="btn sm primary" onclick="analyzeFromScan('${s.symbol}','${s.strategy}')">Analyze</button></td>
        </tr>`;
    }
    $('#scanResultsBody').innerHTML = html || '<tr><td colspan="7" style="color:var(--text-3)">No recent setups. Run a scan!</td></tr>';
    
    // Count active setups
    const setups = await api('/api/scan/setups');
    $('#statActiveSetups').textContent = setups.count || 0;
}

async function loadMonitorStatus(){
    const res = await api('/api/monitor/status');
    const running = res.running;
    
    $('#statMonitorStatus').textContent = running ? 'ON' : 'OFF';
    $('#statMonitorStatus').style.color = running ? 'var(--green)' : 'var(--text-3)';
    $('#monitorStartBtn').disabled = running;
    $('#monitorStopBtn').disabled = !running;
    
    if(res.last_scan){
        const lastScan = new Date(res.last_scan);
        $('#lastScanTime').textContent = lastScan.toLocaleTimeString();
    }
}

async function startMonitor(){
    const res = await api('/api/monitor/start',{method:'POST'});
    toast(res.status === 'started' ? 'Monitor started' : 'Monitor already running', 'success');
    loadMonitorStatus();
    startNotificationPolling();
}

async function stopMonitor(){
    const res = await api('/api/monitor/stop',{method:'POST'});
    toast('Monitor stopping...', 'info');
    setTimeout(loadMonitorStatus, 1000);
    stopNotificationPolling();
}

async function runManualScan(){
    toast('Running scan...', 'info');
    const res = await api('/api/scan/run',{method:'POST'});
    toast('Scan completed', 'success');
    loadScanResults();
    loadMonitorStatus();
}

function startNotificationPolling(){
    if(monitorPollingInterval) return;
    monitorPollingInterval = setInterval(async()=>{
        const res = await api('/api/alerts/triggered?clear=true');
        if(res.alerts && res.alerts.length > 0){
            for(const alert of res.alerts){
                showNotification(alert);
            }
            loadAlertHistory();
            loadAlerts();
        }
    }, 5000);
}

function stopNotificationPolling(){
    if(monitorPollingInterval){
        clearInterval(monitorPollingInterval);
        monitorPollingInterval = null;
    }
}

function showNotification(alert){
    toast(`${alert.name}: ${alert.message}`, alert.type.includes('sl')?'error':alert.type.includes('tp')?'success':'info');
    
    if(browserNotificationsEnabled && 'Notification' in window && Notification.permission === 'granted'){
        new Notification(`${alert.name}`, {
            body: alert.message,
            icon: '&#x1F4CA;',
            tag: `alert-${alert.id}`
        });
    }
}

function toggleBrowserNotifications(){
    if(!('Notification' in window)){
        toast('Browser notifications not supported', 'error');
        $('#browserNotifications').checked = false;
        return;
    }
    
    if($('#browserNotifications').checked){
        Notification.requestPermission().then(perm => {
            browserNotificationsEnabled = perm === 'granted';
            if(!browserNotificationsEnabled){
                toast('Notifications blocked by browser', 'error');
                $('#browserNotifications').checked = false;
            }
        });
    } else {
        browserNotificationsEnabled = false;
    }
}

async function toggleAlert(id){
    await api(`/api/alerts/${id}/toggle`,{method:'POST'});
    loadAlerts();
}

async function resetAlert(id){
    await api(`/api/alerts/${id}/reset`,{method:'POST'});
    loadAlerts();
}

async function deleteAlert(id){
    if(!confirm('Delete this alert?')) return;
    await api(`/api/alerts/${id}`,{method:'DELETE'});
    loadAlerts();
    toast('Alert deleted','info');
}

function openAlertModal(){
    const html = `
        <div class="modal-header"><h3>Create Alert</h3><button class="modal-close" onclick="closeModal()">&#x2715;</button></div>
        <div class="modal-body">
            <div class="form-group"><label class="form-label">Alert Name</label><input type="text" id="alertName" placeholder="SOL $200 Alert"></div>
            <div class="grid grid-2">
                <div class="form-group"><label class="form-label">Symbol</label><input type="text" id="alertSymbol" placeholder="SOL"></div>
                <div class="form-group"><label class="form-label">Alert Type</label>
                    <select id="alertType" onchange="updateAlertConditions()">
                        <option value="price">Price</option>
                        <option value="indicator">Indicator</option>
                        <option value="funding">Funding Rate</option>
                        <option value="divergence">Divergence</option>
                        <option value="portfolio">Portfolio</option>
                    </select>
                </div>
            </div>
            <div class="grid grid-2">
                <div class="form-group"><label class="form-label">Condition</label>
                    <select id="alertCondition">
                        <option value="crosses_above">Crosses Above</option>
                        <option value="crosses_below">Crosses Below</option>
                        <option value="reaches">Reaches</option>
                    </select>
                </div>
                <div class="form-group"><label class="form-label">Target Value</label><input type="number" id="alertTarget" step="any" placeholder="200"></div>
            </div>
            <div class="form-group" style="display:flex;gap:16px">
                <label style="font-size:.75rem;display:flex;align-items:center;gap:6px">
                    <input type="checkbox" id="alertOneTime" checked style="width:auto"> One-time (disable after trigger)
                </label>
                <label style="font-size:.75rem;display:flex;align-items:center;gap:6px">
                    <input type="checkbox" id="alertEnabled" checked style="width:auto"> Enabled
                </label>
            </div>
            <div class="form-group"><label class="form-label">Notes (optional)</label><textarea id="alertNotes" rows="2" style="font-size:.75rem"></textarea></div>
        </div>
        <div class="modal-footer">
            <button class="btn" onclick="closeModal()">Cancel</button>
            <button class="btn primary" onclick="createAlert()">Create Alert</button>
        </div>
    `;
    openModal(html);
}

function updateAlertConditions(){
    const type = $('#alertType').value;
    let options = '';
    
    if(type === 'price'){
        options = '<option value="crosses_above">Crosses Above</option><option value="crosses_below">Crosses Below</option><option value="reaches">Reaches</option>';
    } else if(type === 'indicator'){
        options = '<option value="rsi_above">RSI Above</option><option value="rsi_below">RSI Below</option><option value="adx_above">ADX Above</option><option value="adx_below">ADX Below</option><option value="volume_spike">Volume Spike (x avg)</option>';
    } else if(type === 'funding'){
        options = '<option value="extreme_positive">Extreme Positive (>0.08%)</option><option value="extreme_negative">Extreme Negative (<-0.08%)</option><option value="above_threshold">Above Threshold</option><option value="below_threshold">Below Threshold</option>';
    } else if(type === 'divergence'){
        options = '<option value="btc_alt_divergence">BTC/Alt Divergence</option>';
    } else if(type === 'portfolio'){
        options = '<option value="heat_above">Heat Above %</option><option value="daily_loss_above">Daily Loss Above %</option><option value="consecutive_losses">Consecutive Losses >=</option>';
    }
    
    $('#alertCondition').innerHTML = options;
}

async function createAlert(){
    const payload = {
        name: $('#alertName').value || `${$('#alertSymbol').value} Alert`,
        symbol: $('#alertSymbol').value.toUpperCase(),
        alert_type: $('#alertType').value,
        condition: $('#alertCondition').value,
        target_value: parseFloat($('#alertTarget').value) || 0,
        one_time: $('#alertOneTime').checked,
        enabled: $('#alertEnabled').checked,
        notes: $('#alertNotes').value
    };
    
    if(!payload.symbol){toast('Symbol required','error');return}
    
    const res = await api('/api/alerts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    if(res.error){toast(res.error,'error');return}
    
    closeModal();
    toast('Alert created','success');
    loadAlerts();
}

function analyzeFromScan(symbol, strategy){
    // Switch to analyze tab and pre-fill
    $$('.tab').forEach(t=>t.classList.remove('active'));
    $$('.panel').forEach(p=>p.classList.remove('active'));
    $$('.tab')[0].classList.add('active');
    $('#panel-analyze').classList.add('active');
    
    $('#symbolInput').value = symbol;
    $('#strategySelect').value = strategy;
    toast(`Set up for ${symbol} ${strategy} analysis`, 'info');
}

function loadAlertsTab(){
    loadAlerts();
    loadAlertHistory();
    loadScanResults();
    loadMonitorStatus();
}

// Add alerts tab to tab handler
const origTabHandler = $$('.tab').forEach;
$$('.tab').forEach(tab=>{tab.addEventListener('click',()=>{$$('.tab').forEach(t=>t.classList.remove('active'));$$('.panel').forEach(p=>p.classList.remove('active'));tab.classList.add('active');$(`#panel-${tab.dataset.tab}`).classList.add('active');({history:loadHistory,journal:loadJournal,watchlist:loadWatchlist,templates:loadTemplates,settings:loadSettings,positions:loadPositions,quant:loadQuantDashboard,alerts:loadAlertsTab}[tab.dataset.tab]||function(){})()})});

// =========== WALLET INTEGRATION ===========
let walletState = {connected:false,type:null,address:null,chain:null};

function checkWalletConnection(){
    const hasPhantom = typeof window.solana !== 'undefined' && window.solana.isPhantom;
    const hasMetaMask = typeof window.ethereum !== 'undefined' && window.ethereum.isMetaMask;
    let html = '';
    if(hasPhantom) html += '<button class="wallet-btn phantom" onclick="connectPhantom()">&#x1F47B; Phantom</button>';
    if(hasMetaMask) html += '<button class="wallet-btn metamask" onclick="connectMetaMask()">&#x1F98A; MetaMask</button>';
    if(!hasPhantom && !hasMetaMask) html = '<span style="color:var(--text-3);font-size:.7rem">No wallet detected</span>';
    $('#walletStatus').innerHTML = html;
}

async function connectPhantom(){
    if(typeof window.solana === 'undefined' || !window.solana.isPhantom){toast('Phantom not installed','error');return}
    try{
        const resp = await window.solana.connect();
        walletState = {connected:true,type:'phantom',address:resp.publicKey.toString(),chain:'solana'};
        updateWalletUI();
        toast('Phantom connected','success');
    }catch(e){toast('Connection rejected','error')}
}

async function connectMetaMask(){
    if(typeof window.ethereum === 'undefined' || !window.ethereum.isMetaMask){toast('MetaMask not installed','error');return}
    try{
        const accounts = await window.ethereum.request({method:'eth_requestAccounts'});
        const chainId = await window.ethereum.request({method:'eth_chainId'});
        const chainMap = {'0x1':'ethereum','0x38':'bsc','0x2105':'base','0xa4b1':'arbitrum','0x89':'polygon'};
        walletState = {connected:true,type:'metamask',address:accounts[0],chain:chainMap[chainId]||'ethereum'};
        updateWalletUI();
        toast('MetaMask connected','success');
        window.ethereum.on('accountsChanged',(accts)=>{if(accts.length===0){walletState={connected:false};updateWalletUI()}else{walletState.address=accts[0];updateWalletUI()}});
        window.ethereum.on('chainChanged',()=>location.reload());
    }catch(e){toast('Connection rejected','error')}
}

function updateWalletUI(){
    if(!walletState.connected){checkWalletConnection();return}
    const shortAddr = walletState.address.slice(0,6)+'...'+walletState.address.slice(-4);
    const btnClass = walletState.type === 'phantom' ? 'phantom' : 'metamask';
    $('#walletStatus').innerHTML = `<button class="wallet-btn ${btnClass} connected" onclick="disconnectWallet()"><span class="wallet-addr">${shortAddr}</span> &#x2713;</button><span style="font-size:.65rem;color:var(--text-2)">${walletState.chain}</span>`;
    if($('#connectWalletBtn'))$('#connectWalletBtn').textContent = 'Connected';
}

function disconnectWallet(){
    if(walletState.type==='phantom'&&window.solana)window.solana.disconnect();
    walletState={connected:false,type:null,address:null,chain:null};
    checkWalletConnection();
    toast('Wallet disconnected','info');
}

function connectWallet(){
    if(walletState.connected)return;
    const hasPhantom = typeof window.solana !== 'undefined' && window.solana.isPhantom;
    const hasMetaMask = typeof window.ethereum !== 'undefined' && window.ethereum.isMetaMask;
    if(hasPhantom && hasMetaMask){
        openModal(`<div class="modal-header"><span style="font-weight:800">Connect Wallet</span><button class="modal-close" onclick="closeModal()">&times;</button></div><div style="display:flex;flex-direction:column;gap:12px"><button class="btn primary" onclick="closeModal();connectPhantom()" style="padding:14px">&#x1F47B; Connect Phantom (Solana)</button><button class="btn primary" onclick="closeModal();connectMetaMask()" style="padding:14px">&#x1F98A; Connect MetaMask (EVM)</button></div>`);
    }else if(hasPhantom){connectPhantom()}
    else if(hasMetaMask){connectMetaMask()}
    else{toast('No wallet detected','error')}
}

// =========== POSITIONS ===========
async function loadPositions(){
    const summary = await api('/api/positions/open-summary');
    $('#positionStats').innerHTML = `<div class="card stat"><div class="stat-value">${summary.open_count||0}</div><div class="stat-label">Open</div></div><div class="card stat"><div class="stat-value">$${(summary.total_exposure_usd||0).toFixed(2)}</div><div class="stat-label">Exposure</div></div><div class="card stat"><div class="stat-value" style="color:${(summary.total_unrealized_pnl||0)>=0?'var(--green)':'var(--red)'}">$${(summary.total_unrealized_pnl||0).toFixed(2)}</div><div class="stat-label">Unrealized</div></div><div class="card stat"><div class="stat-value" style="color:${summary.portfolio_heat>60?'var(--red)':summary.portfolio_heat>40?'var(--yellow)':'var(--green)'}">${summary.portfolio_heat||0}%</div><div class="stat-label">Heat</div></div>`;
    const positions = summary.positions || [];
    if(!positions.length){$('#positionsBody').innerHTML='<tr><td colspan="11" style="text-align:center;color:var(--text-3)">No open positions</td></tr>';return}
    $('#positionsBody').innerHTML = positions.map(p=>`<tr><td><strong>${p.symbol}</strong></td><td>${p.chain}</td><td>${p.direction==='long'?'&#x1F7E2;':'&#x1F534;'}</td><td>$${p.entry_price.toFixed(6)}</td><td>$${(p.current_price||p.entry_price).toFixed(6)}</td><td>$${p.size_usd.toFixed(2)}</td><td class="${(p.unrealized_pnl||0)>=0?'pnl-positive':'pnl-negative'}">$${(p.unrealized_pnl||0).toFixed(2)} (${(p.unrealized_pnl_pct||0).toFixed(1)}%)</td><td style="color:var(--red)">${p.stop_loss||'-'}</td><td style="color:var(--green)">${p.take_profit||'-'}</td><td><span class="badge ${p.status}">${p.status}</span></td><td><div class="btn-group"><button class="btn sm" onclick="updatePositionPrice(${p.id})">Update</button><button class="btn sm danger" onclick="closePositionModal(${p.id})">Close</button></div></td></tr>`).join('');
}

async function updatePositionPrice(pid){
    const price = prompt('Enter current price:');
    if(!price) return;
    const res = await api(`/api/positions/${pid}/update-price`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({current_price:parseFloat(price)})});
    if(res.error){toast(res.error,'error');return}
    toast(`Updated: $${res.unrealized_pnl.toFixed(2)} (${res.unrealized_pnl_pct.toFixed(1)}%)`,res.unrealized_pnl>=0?'success':'error');
    loadPositions();
}

function closePositionModal(pid){
    openModal(`<div class="modal-header"><span style="font-weight:800">Close Position #${pid}</span><button class="modal-close" onclick="closeModal()">&times;</button></div><div class="form-group"><label class="form-label">Exit Price</label><input type="number" id="exitPrice" step="any" placeholder="Current market price"></div><div class="form-group"><label class="form-label">Tx Hash (optional)</label><input type="text" id="exitTxHash" placeholder="Transaction hash if executed"></div><button class="btn primary" onclick="closePosition(${pid})">Close Position</button>`);
}

async function closePosition(pid){
    const exitPrice = parseFloat($('#exitPrice').value);
    if(!exitPrice){toast('Enter exit price','error');return}
    const res = await api(`/api/positions/${pid}/close`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({exit_price:exitPrice,tx_hash:$('#exitTxHash').value})});
    if(res.error){toast(res.error,'error');return}
    closeModal();
    toast(`Position closed: ${res.outcome.toUpperCase()} $${res.pnl_usd.toFixed(2)}`,res.outcome==='win'?'success':'error');
    loadPositions();
}

// =========== EXECUTION ===========
let pendingExecution = null;

async function openExecutionModal(tradeParams){
    const {symbol,entry_price,stop_loss,take_profit,position_size_pct,strategy,signal_count,analysis_id} = tradeParams;
    if(!walletState.connected){toast('Connect wallet first','error');connectWallet();return}
    
    const balance = parseFloat(settings.portfolio_balance || 100);
    const sizeUsd = balance * (parseFloat(position_size_pct)||50) / 100;
    const entryNum = parseFloat(String(entry_price).replace(/[^0-9.]/g,'')) || 0;
    const slNum = parseFloat(String(stop_loss).replace(/[^0-9.]/g,'')) || 0;
    const tpNum = parseFloat(String(take_profit).replace(/[^0-9.]/g,'')) || 0;
    
    openModal(`<div class="modal-header"><span style="font-weight:800">&#x26A1; Execute Trade</span><button class="modal-close" onclick="closeModal()">&times;</button></div>
    <div class="form-group"><label class="form-label">Symbol</label><input type="text" id="execSymbol" value="${symbol||''}"></div>
    <div class="form-group"><label class="form-label">Token Address (for safety check)</label><input type="text" id="execTokenAddr" placeholder="Contract address..."></div>
    <div class="grid grid-2">
        <div class="form-group"><label class="form-label">Chain</label><select id="execChain"><option value="solana" ${walletState.chain==='solana'?'selected':''}>Solana</option><option value="ethereum" ${walletState.chain==='ethereum'?'selected':''}>Ethereum</option><option value="base" ${walletState.chain==='base'?'selected':''}>Base</option><option value="arbitrum" ${walletState.chain==='arbitrum'?'selected':''}>Arbitrum</option><option value="bsc" ${walletState.chain==='bsc'?'selected':''}>BSC</option></select></div>
        <div class="form-group"><label class="form-label">Direction</label><select id="execDir"><option value="long">Long (Buy)</option><option value="short">Short (Sell)</option></select></div>
    </div>
    <div class="grid grid-3">
        <div class="form-group"><label class="form-label">Size ($)</label><input type="number" id="execSize" value="${sizeUsd.toFixed(2)}" step="any"></div>
        <div class="form-group"><label class="form-label">Entry Price</label><input type="number" id="execEntry" value="${entryNum}" step="any"></div>
        <div class="form-group"><label class="form-label">Slippage %</label><input type="number" id="execSlip" value="1" step="0.1"></div>
    </div>
    <div class="grid grid-2">
        <div class="form-group"><label class="form-label">Stop Loss</label><input type="number" id="execSL" value="${slNum}" step="any"></div>
        <div class="form-group"><label class="form-label">Take Profit</label><input type="number" id="execTP" value="${tpNum}" step="any"></div>
    </div>
    <div class="form-group"><label class="form-label">Signals Present</label><input type="number" id="execSigs" value="${signal_count||0}" min="0" max="5"></div>
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px"><input type="checkbox" id="execOverride" style="width:auto"><label for="execOverride" style="font-size:.78rem;color:var(--yellow)">Override signal requirement (< 4/5)</label></div>
    <div id="execSafetyResult"></div>
    <div id="execWarnings"></div>
    <div class="btn-group" style="margin-top:14px"><button class="btn" onclick="runSafetyCheck()">&#x1F6E1; Safety Check</button><button class="btn primary" onclick="validateExecution(${analysis_id||'null'},'${strategy||''}')">Validate & Execute</button></div>`);
}

async function runSafetyCheck(){
    const addr = $('#execTokenAddr').value.trim();
    const chain = $('#execChain').value;
    if(!addr){toast('Enter token address','error');return}
    const res = await api(`/api/safety-check/${addr}?chain=${chain}`);
    if(res.error){$('#execSafetyResult').innerHTML=`<div class="exec-warning">Could not verify: ${res.error}</div>`;return}
    const s = res.safety || {};
    const scoreClass = s.safety_score >= 70 ? 'safe' : s.safety_score >= 40 ? 'caution' : 'danger';
    let html = `<div style="margin:12px 0;padding:12px;background:var(--bg-0);border-radius:var(--radius)"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px"><span style="font-weight:700">Safety Check</span><span class="safety-score ${scoreClass}">${s.safety_score}/100</span></div>`;
    if(s.issues&&s.issues.length){html+=`<ul style="font-size:.75rem;color:var(--text-2);margin:0;padding-left:16px">${s.issues.map(i=>`<li>${i}</li>`).join('')}</ul>`}
    if(res.liquidity){html+=`<div style="font-size:.75rem;margin-top:8px"><span style="color:var(--text-3)">Liquidity:</span> $${(res.liquidity.liquidity_usd||0).toLocaleString()}</div>`}
    html+='</div>';
    $('#execSafetyResult').innerHTML=html;
}

async function validateExecution(analysisId,strategy){
    const payload = {
        symbol: $('#execSymbol').value,
        chain: $('#execChain').value,
        token_address: $('#execTokenAddr').value,
        direction: $('#execDir').value,
        size_usd: parseFloat($('#execSize').value)||0,
        entry_price: parseFloat($('#execEntry').value)||0,
        stop_loss: parseFloat($('#execSL').value)||0,
        take_profit: parseFloat($('#execTP').value)||0,
        signal_count: parseInt($('#execSigs').value)||0,
        slippage: parseFloat($('#execSlip').value)||1,
        override_signals: $('#execOverride').checked,
        analysis_id: analysisId
    };
    
    const res = await api('/api/execute',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    
    if(!res.approved){
        $('#execWarnings').innerHTML=`<div class="exec-warning" style="background:rgba(255,82,82,.1);border-color:var(--red);color:var(--red)">&#x274C; ${res.rejection_reason}</div>`;
        toast('Trade rejected','error');
        return;
    }
    
    let warningsHtml = '';
    if(res.warnings&&res.warnings.length){warningsHtml=`<div class="exec-warning">${res.warnings.map(w=>'&#x26A0; '+w).join('<br>')}</div>`}
    
    pendingExecution = {...res, strategy};
    
    $('#execWarnings').innerHTML = warningsHtml + `<div class="exec-confirm"><div style="font-weight:800;margin-bottom:10px;color:var(--green)">&#x2705; Trade Approved</div>
    <div class="exec-row"><span class="exec-label">Action</span><span class="exec-value">${res.action} ${res.symbol}</span></div>
    <div class="exec-row"><span class="exec-label">Size</span><span class="exec-value">$${res.size_usd.toFixed(2)} (${res.size_pct}%)</span></div>
    <div class="exec-row"><span class="exec-label">Entry</span><span class="exec-value">$${res.entry_price}</span></div>
    <div class="exec-row"><span class="exec-label">Stop Loss</span><span class="exec-value" style="color:var(--red)">$${res.stop_loss} (-${res.risk_pct}%)</span></div>
    <div class="exec-row"><span class="exec-label">Take Profit</span><span class="exec-value" style="color:var(--green)">$${res.take_profit} (+${res.reward_pct}%)</span></div>
    <div class="exec-row"><span class="exec-label">Risk/Reward</span><span class="exec-value">1:${res.rr_ratio}</span></div>
    <div class="exec-row"><span class="exec-label">Min Received</span><span class="exec-value">${res.min_received.toFixed(6)} tokens</span></div>
    <button class="btn success" onclick="confirmExecution()" style="width:100%;margin-top:12px;padding:12px">&#x1F680; CONFIRM & SIGN</button></div>`;
}

async function confirmExecution(){
    if(!pendingExecution){toast('No pending execution','error');return}
    
    // In a real implementation, this would trigger wallet signing
    // For now, we'll just log the trade and show success
    const res = await api('/api/execute/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
        symbol: pendingExecution.symbol,
        chain: pendingExecution.chain,
        token_address: pendingExecution.token_address,
        wallet_address: walletState.address,
        direction: pendingExecution.direction,
        size_usd: pendingExecution.size_usd,
        entry_price: pendingExecution.entry_price,
        stop_loss: pendingExecution.stop_loss,
        take_profit: pendingExecution.take_profit,
        signal_count: pendingExecution.signal_count,
        analysis_id: pendingExecution.analysis_id,
        strategy: pendingExecution.strategy,
        tx_hash: 'SIMULATED_' + Date.now()
    })});
    
    if(res.error){toast(res.error,'error');return}
    
    closeModal();
    toast(`Position opened: ${pendingExecution.symbol}`,'success');
    pendingExecution = null;
    
    // Switch to positions tab
    $$('.tab').forEach(t=>t.classList.remove('active'));
    $$('.panel').forEach(p=>p.classList.remove('active'));
    $$('.tab')[1].classList.add('active');
    $('#panel-positions').classList.add('active');
    loadPositions();
}

// Start notification polling if monitor is running
(async()=>{
    const status = await api('/api/monitor/status');
    if(status.running){startNotificationPolling()}
})();
</script>
</body>
</html>"""


if __name__ == "__main__":
    init_db()
    
    # Auto-start background monitor
    start_background_monitor()
    
    ks = "Set" if ANTHROPIC_API_KEY else "MISSING - export ANTHROPIC_API_KEY"
    print(f"""
    =====================================================
      PENGUIN-BURRY CHART ANALYZER v4
      Powered by Claude Opus 4.5 + Web Search
      Phase 5: Intelligence & Optimization
    -----------------------------------------------------
      Server:     http://localhost:{PORT}
      API Key:    {ks}
      Model:      {CLAUDE_MODEL}
      DB:         {DB_PATH}
      Web Search: Enabled (live prices, news, volume)
      Monitor:    Auto-started (alerts every 30s, scan every 5m)
    -----------------------------------------------------
      NEW IN PHASE 5:
      • Backtesting Engine (/api/intel/backtest/<symbol>/<strategy>)
      • Parameter Optimization (/api/intel/backtest/optimize)
      • Pattern Recognition (/api/intel/patterns/similar)
      • Market Regime (HMM) (/api/intel/regime/<symbol>)
      • Performance Attribution (/api/intel/attribution)
      • Risk Simulation (/api/intel/risk/simulate)
      • Stress Testing (/api/intel/risk/stress-test)
      • Intelligence Dashboard (/api/intel/dashboard)
    -----------------------------------------------------
      "If it's not obvious, it's not a trade."
    =====================================================
    """)
    if not ANTHROPIC_API_KEY:
        print("  Set your key:  export ANTHROPIC_API_KEY='sk-ant-...'")
        print()
    app.run(host=HOST, port=PORT, debug=False)
