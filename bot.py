"""
Solana On-Chain Wallet Tracker Bot
===================================
Tracks swaps, token transfers, and SOL transfers for any Solana wallet.
Sends real-time alerts to your Telegram chat.

Setup: See SETUP.md
"""

import asyncio
import sqlite3
import httpx
import logging
import os
import json
from aiohttp import web
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    filename="bot.log",
    level=logging.INFO,
    format="%(asctime)s %(message)s"
)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# ============================================================
# CONFIG — keys are loaded from .env (never commit .env to git)
# ============================================================
HELIUS_API_KEY     = os.getenv("HELIUS_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
POLL_INTERVAL = 30  # Fallback polling interval (webhook handles real-time)
WEBHOOK_PORT  = 8080  # Port for Helius webhook receiver

# ── Transfer spam filter ──────────────────────────────────────
# Incoming transfers with fewer recipients than this are likely dust/spam airdrops
# (e.g. the odinbot sending tiny SOL to 20 wallets at once)
MAX_TRANSFER_RECIPIENTS = 3   # skip tx if SOL was sent to more than this many accounts at once
MIN_INCOMING_SOL = 0.05       # skip incoming SOL transfers smaller than this (SOL)
# ─────────────────────────────────────────────────────────────
# ============================================================


# ─────────────────────────────────────────
# DATABASE (SQLite — stores wallets + chats)
# ─────────────────────────────────────────

def init_db():
    conn = sqlite3.connect("wallets.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS wallets (
            address TEXT PRIMARY KEY,
            label TEXT,
            last_signature TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS first_buys (
            wallet  TEXT,
            mint    TEXT,
            price   REAL,
            ts      INTEGER,
            PRIMARY KEY (wallet, mint)
        )
    """)
    conn.commit()
    conn.close()

def get_first_buy(wallet: str, mint: str):
    """Returns (price, ts) of first recorded buy, or None."""
    conn = sqlite3.connect("wallets.db")
    c = conn.cursor()
    c.execute("SELECT price, ts FROM first_buys WHERE wallet=? AND mint=?", (wallet, mint))
    row = c.fetchone()
    conn.close()
    return row  # (price, ts) or None

def save_first_buy(wallet: str, mint: str, price: float):
    """Store the first detected buy price for a wallet+mint pair."""
    import time
    conn = sqlite3.connect("wallets.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO first_buys (wallet, mint, price, ts) VALUES (?,?,?,?)",
              (wallet, mint, price, int(time.time())))
    conn.commit()
    conn.close()

def get_wallets():
    conn = sqlite3.connect("wallets.db")
    c = conn.cursor()
    c.execute("SELECT address, label, last_signature FROM wallets")
    rows = c.fetchall()
    conn.close()
    return rows

def add_wallet(address: str, label: str) -> bool:
    conn = sqlite3.connect("wallets.db")
    c = conn.cursor()
    try:
        c.execute(
            "INSERT INTO wallets (address, label, last_signature) VALUES (?, ?, NULL)",
            (address, label)
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def remove_wallet(address: str) -> bool:
    conn = sqlite3.connect("wallets.db")
    c = conn.cursor()
    c.execute("DELETE FROM wallets WHERE address = ?", (address,))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def update_last_signature(address: str, signature: str):
    conn = sqlite3.connect("wallets.db")
    c = conn.cursor()
    c.execute("UPDATE wallets SET last_signature = ? WHERE address = ?", (signature, address))
    conn.commit()
    conn.close()

def add_chat(chat_id: int):
    conn = sqlite3.connect("wallets.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO chats (chat_id) VALUES (?)", (chat_id,))
    conn.commit()
    conn.close()

def get_chats():
    conn = sqlite3.connect("wallets.db")
    c = conn.cursor()
    c.execute("SELECT chat_id FROM chats")
    chats = [row[0] for row in c.fetchall()]
    conn.close()
    return chats


# ─────────────────────────────────────────
# HELIUS API (fetches on-chain transactions)
# ─────────────────────────────────────────

# Well-known token symbols (so we don't need an API call for these)
KNOWN_TOKENS = {
    "So11111111111111111111111111111111111111112":  "WSOL",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB":  "USDT",
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So":  "mSOL",
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs":  "ETH",
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263":  "BONK",
}

# In-memory caches
_processed_sigs: set[str] = set()     # dedup: signatures already sent to Telegram
_symbol_cache: dict[str, str] = {}
_mc_cache: dict[str, float] = {}      # mint → market cap USD
_created_cache: dict[str, int] = {}   # mint → pair created timestamp (ms)

async def get_token_symbol(mint: str) -> str:
    """Resolve a mint address to a symbol. Uses cache + Helius DAS API."""
    if not mint:
        return "?"
    if mint in KNOWN_TOKENS:
        return KNOWN_TOKENS[mint]
    if mint in _symbol_cache:
        return _symbol_cache[mint]

    # Ask Helius DAS for the asset metadata
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(
                f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}",
                json={"jsonrpc": "2.0", "id": "sym", "method": "getAsset",
                      "params": {"id": mint}},
            )
            if resp.status_code == 200:
                data = resp.json().get("result", {})
                symbol = (
                    data.get("content", {})
                        .get("metadata", {})
                        .get("symbol", "")
                    or data.get("token_info", {}).get("symbol", "")
                )
                if symbol:
                    _symbol_cache[mint] = symbol
                    return symbol
    except Exception:
        pass

    # Fallback: short address
    sym = short(mint)
    _symbol_cache[mint] = sym
    return sym

async def resolve_symbols(tx: dict) -> tuple[dict[str, str], dict[str, float]]:
    """Pre-fetch symbols + USD prices for all mints in a transaction."""
    SOL_MINT = "So11111111111111111111111111111111111111112"
    mints = {SOL_MINT}  # always include SOL so we can price it
    for xfer in tx.get("tokenTransfers", []):
        if m := xfer.get("mint"):
            mints.add(m)
    swap = tx.get("events", {}).get("swap", {})
    for t in swap.get("tokenInputs", []) + swap.get("tokenOutputs", []):
        if m := t.get("mint"):
            mints.add(m)

    syms: dict[str, str] = {}
    for mint in mints:
        syms[mint] = await get_token_symbol(mint)

    prices = await get_token_prices(list(mints))
    return syms, prices

async def get_token_prices(mints: list[str]) -> dict[str, float]:
    """
    Fetch USD prices for a list of mints.
    Strategy:
      1. Try Jupiter (good for major tokens, SOL, WSOL, USDC…)
      2. For any mints still missing, try Dexscreener (covers pump.fun & new tokens)
    """
    if not mints:
        return {}

    unique = list(set(mints))
    result: dict[str, float] = {}

    # ── 1. Jupiter Price API ──────────────────────────────────
    try:
        ids = ",".join(unique)
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(f"https://api.jup.ag/price/v2?ids={ids}")
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                for mint, info in data.items():
                    if info and info.get("price"):
                        try:
                            result[mint] = float(info["price"])
                        except Exception:
                            pass
    except Exception as e:
        print(f"[Jupiter] Price fetch failed: {e}")

    # ── 2. Dexscreener: fill missing prices + always grab MC for non-stable tokens ───
    _stable = {
        "So11111111111111111111111111111111111111112",
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    }
    # include any token that's missing price OR missing market cap data
    missing = [m for m in unique if m not in result or (m not in _stable and m not in _mc_cache)]
    if missing:
        try:
            # Dexscreener accepts up to 30 comma-separated addresses
            ids = ",".join(missing[:30])
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(
                    f"https://api.dexscreener.com/latest/dex/tokens/{ids}"
                )
                if resp.status_code == 200:
                    pairs = resp.json().get("pairs") or []
                    # For each mint, pick the pair with highest liquidity
                    best: dict[str, tuple[float, float, float]] = {}  # mint → (liq, price, mc)
                    oldest: dict[str, int] = {}  # mint → oldest pairCreatedAt
                    for pair in pairs:
                        price_usd = pair.get("priceUsd")
                        if not price_usd:
                            continue
                        liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                        mc  = float(pair.get("marketCap") or pair.get("fdv") or 0)
                        created = int(pair.get("pairCreatedAt") or 0)
                        base_addr = pair.get("baseToken", {}).get("address", "")
                        quote_addr = pair.get("quoteToken", {}).get("address", "")
                        # Match the mint we're looking for (could be base or quote)
                        for mint in (base_addr, quote_addr):
                            if mint and mint in unique:
                                if mint not in best or liq > best[mint][0]:
                                    best[mint] = (liq, float(price_usd), mc)
                                if created and (mint not in oldest or created < oldest[mint]):
                                    oldest[mint] = created
                    for mint, (_, price, mc) in best.items():
                        if mint not in result:
                            result[mint] = price
                        if mc:
                            _mc_cache[mint] = mc
                    for mint, created in oldest.items():
                        if created:
                            _created_cache[mint] = created
        except Exception as e:
            print(f"[Dexscreener] Price fetch failed: {e}")

    print(f"[Prices] resolved {len(result)}/{len(unique)}: { {k[-6:]: round(v,6) for k,v in result.items()} }")
    return result

def fmt_usd(val: float) -> str:
    if val >= 1_000_000: return f"${val/1_000_000:.2f}M"
    if val >= 1_000:     return f"${val:,.0f}"
    if val >= 0.01:      return f"${val:.2f}"
    return f"${val:.6f}"

def fmt_age(created_ms: int) -> str:
    """Format token age as '5m', '3h 20m', '2d 5h' etc."""
    import time
    if not created_ms:
        return ""
    elapsed = int(time.time()) - (created_ms // 1000)
    if elapsed < 0:
        return ""
    if elapsed < 60:
        return f"{elapsed}s"
    if elapsed < 3600:
        return f"{elapsed // 60}m"
    if elapsed < 86400:
        h = elapsed // 3600
        m = (elapsed % 3600) // 60
        return f"{h}h {m}m" if m else f"{h}h"
    d = elapsed // 86400
    h = (elapsed % 86400) // 3600
    return f"{d}d {h}h" if h else f"{d}d"

async def get_token_age(mint: str) -> int:
    """Fetch the OLDEST pair creation time from Dexscreener for this token.
    Uses the oldest pairCreatedAt across all pairs where the mint is either
    the base or quote token — that gives the true 'first seen' age."""
    if mint in _created_cache:
        return _created_cache[mint]
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
            if resp.status_code == 200:
                pairs = resp.json().get("pairs") or []
                oldest = 0
                for p in pairs:
                    # Only consider pairs where our mint is base or quote
                    base = p.get("baseToken", {}).get("address", "")
                    quote = p.get("quoteToken", {}).get("address", "")
                    if mint not in (base, quote):
                        continue
                    created = int(p.get("pairCreatedAt") or 0)
                    if created and (oldest == 0 or created < oldest):
                        oldest = created
                if oldest:
                    _created_cache[mint] = oldest
                    return oldest
    except Exception:
        pass
    return 0

async def get_wallet_token_balance(wallet: str, mint: str) -> float:
    """
    Fetch token balance via Solana RPC getTokenAccountsByOwner.
    More reliable and up-to-date than the Helius REST balances endpoint.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}",
                json={
                    "jsonrpc": "2.0", "id": "bal",
                    "method": "getTokenAccountsByOwner",
                    "params": [
                        wallet,
                        {"mint": mint},
                        {"encoding": "jsonParsed", "commitment": "confirmed"}
                    ]
                }
            )
            if resp.status_code == 200:
                accounts = resp.json().get("result", {}).get("value", [])
                total = 0.0
                for acc in accounts:
                    info = (acc.get("account", {})
                               .get("data", {})
                               .get("parsed", {})
                               .get("info", {}))
                    ui_amount = info.get("tokenAmount", {}).get("uiAmount") or 0
                    total += float(ui_amount)
                return total
    except Exception as e:
        print(f"[RPC] Balance error: {e}")
    return 0.0

async def fetch_transactions(address: str, limit: int = 10):
    """Fetch recent parsed transactions for a wallet from Helius."""
    url = f"https://api.helius.xyz/v0/addresses/{address}/transactions"
    params = {"api-key": HELIUS_API_KEY, "limit": limit}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                return resp.json()
            else:
                print(f"[Helius] Error {resp.status_code} for {address[:8]}...")
    except Exception as e:
        print(f"[Helius] Request failed: {e}")
    return []


# ─────────────────────────────────────────
# FORMATTERS (turn raw tx data into messages)
# ─────────────────────────────────────────

def short(addr: str) -> str:
    """Shorten a wallet address: ABC...XYZ"""
    return f"{addr[:6]}...{addr[-4:]}" if addr else "?"

def format_amount(amount) -> str:
    """Full comma-separated token amount (e.g. 5,908,396.81)."""
    try:
        n = float(amount)
        if n >= 1:
            return f"{n:,.2f}"
        else:
            return f"{n:.6f}".rstrip("0").rstrip(".")
    except:
        return str(amount)

def fmt_mc(val: float) -> str:
    """Short K/M format for market cap (e.g. $23.92K, $1.24M)."""
    if val >= 1_000_000_000: return f"${val/1_000_000_000:.2f}B"
    if val >= 1_000_000:     return f"${val/1_000_000:.2f}M"
    if val >= 1_000:         return f"${val/1_000:.2f}K"
    if val >= 0.01:          return f"${val:.2f}"
    return f"${val:.6f}"

def format_swap(tx: dict, label: str, address: str,
                syms: dict = {}, prices: dict = {},
                balance: float = 0.0) -> tuple:
    """
    Returns (text, reply_markup) matching the reference design.
    """
    SOL_MINT = "So11111111111111111111111111111111111111112"
    events    = tx.get("events", {})
    swap      = events.get("swap", {})
    tok_xfers = tx.get("tokenTransfers", [])
    nat_xfers = tx.get("nativeTransfers", [])
    fee_sol   = tx.get("fee", 0) / 1e9

    def sym_for(mint: str, fallback: str = "") -> str:
        return syms.get(mint) or fallback or short(mint)

    def usd_val(raw: float, mint: str) -> float:
        return raw * prices.get(mint, 0)

    def sol_usd(sol: float) -> float:
        return sol * prices.get(SOL_MINT, 0)

    # ── Resolve sent / received sides ──────────────────────────
    inputs     = swap.get("tokenInputs", [])
    outputs    = swap.get("tokenOutputs", [])
    native_in  = swap.get("nativeInput")
    native_out = swap.get("nativeOutput")

    sol_sent = sol_got = 0.0
    tok_sent_raw = tok_got_raw = 0.0
    tok_sent_mint = tok_got_mint = ""
    tok_sent_sym  = tok_got_sym  = ""

    # Jupiter / Raydium path
    if inputs:
        t = inputs[0]
        tok_sent_raw  = float(t.get("tokenAmount", 0))
        tok_sent_mint = t.get("mint", "")
        tok_sent_sym  = sym_for(tok_sent_mint, t.get("symbol", ""))
    elif native_in:
        sol_sent = float(native_in.get("amount", 0)) / 1e9

    if outputs:
        t = outputs[0]
        tok_got_raw  = float(t.get("tokenAmount", 0))
        tok_got_mint = t.get("mint", "")
        tok_got_sym  = sym_for(tok_got_mint, t.get("symbol", ""))
    elif native_out:
        sol_got = float(native_out.get("amount", 0)) / 1e9

    # Pump.fun fallback
    if not any([tok_sent_raw, tok_got_raw, sol_sent, sol_got]):
        t_out = [x for x in tok_xfers if x.get("fromUserAccount") == address]
        t_in  = [x for x in tok_xfers if x.get("toUserAccount") == address]
        if t_out:
            best = max(t_out, key=lambda x: float(x.get("tokenAmount", 0)))
            tok_sent_raw  = float(best.get("tokenAmount", 0))
            tok_sent_mint = best.get("mint", "")
            tok_sent_sym  = sym_for(tok_sent_mint, best.get("symbol", ""))
        if t_in:
            best = max(t_in, key=lambda x: float(x.get("tokenAmount", 0)))
            tok_got_raw  = float(best.get("tokenAmount", 0))
            tok_got_mint = best.get("mint", "")
            tok_got_sym  = sym_for(tok_got_mint, best.get("symbol", ""))
        n_out = [x for x in nat_xfers if x.get("fromUserAccount") == address
                 and float(x.get("amount", 0)) / 1e9 > 0.001]
        n_in  = [x for x in nat_xfers if x.get("toUserAccount") == address
                 and float(x.get("amount", 0)) / 1e9 > 0.001]
        # Always capture SOL movement regardless of token transfers
        if n_out:
            sol_sent = float(max(n_out, key=lambda x: x.get("amount", 0))["amount"]) / 1e9
        if n_in:
            sol_got  = float(max(n_in,  key=lambda x: x.get("amount", 0))["amount"]) / 1e9

    # ── Last-resort SOL capture ────────────────────────────────
    # Pump AMM sets tokenOutputs but leaves nativeInput null, so sol_sent stays 0.
    # Always check nativeTransfers if we received tokens but have no SOL sent/got yet.
    if tok_got_raw and not sol_sent and not tok_sent_raw:
        n_out_all = [x for x in nat_xfers
                     if x.get("fromUserAccount") == address
                     and float(x.get("amount", 0)) / 1e9 > 0.001]
        if n_out_all:
            sol_sent = float(max(n_out_all, key=lambda x: x.get("amount", 0))["amount"]) / 1e9
    if tok_sent_raw and not sol_got and not tok_got_raw:
        n_in_all = [x for x in nat_xfers
                    if x.get("toUserAccount") == address
                    and float(x.get("amount", 0)) / 1e9 > 0.001]
        if n_in_all:
            sol_got = float(max(n_in_all, key=lambda x: x.get("amount", 0))["amount"]) / 1e9

    # ── BUY or SELL? ───────────────────────────────────────────
    # SELL = wallet sent a non-base token (the meme) and received SOL or base
    # BUY  = everything else (wallet received the meme token)
    _BASE_SET = {SOL_MINT, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                 "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
                 "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So"}
    if tok_sent_raw and tok_sent_mint and tok_sent_mint not in _BASE_SET and (sol_got > 0 or tok_got_mint in _BASE_SET):
        is_buy = False
    elif tok_got_raw and tok_got_mint and tok_got_mint not in _BASE_SET:
        is_buy = True
    else:
        is_buy = bool(sol_sent or (tok_sent_mint == SOL_MINT))

    # The "main" token (the non-SOL side)
    if is_buy:
        main_mint = tok_got_mint or tok_sent_mint
        main_sym  = tok_got_sym  or tok_sent_sym
        sol_amt   = sol_sent or (tok_sent_raw if tok_sent_mint == SOL_MINT else 0)
        tok_amt   = tok_got_raw
        tok_mint  = tok_got_mint
    else:
        main_mint = tok_sent_mint or tok_got_mint
        main_sym  = tok_sent_sym  or tok_got_sym
        sol_amt   = sol_got  or (tok_got_raw  if tok_got_mint  == SOL_MINT else 0)
        tok_amt   = tok_sent_raw
        tok_mint  = tok_sent_mint

    # ── Skip base-token swaps (SOL↔USDC, WSOL↔USDT etc.) ────
    _BASE_MINTS = {
        "So11111111111111111111111111111111111111112",   # WSOL
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", # USDC
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
        "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",  # mSOL
    }
    if main_mint in _BASE_MINTS:
        return None  # not a meme/token trade, skip

    action_emoji = "🟢" if is_buy else "🔴"
    action_word  = "BUY"  if is_buy else "SELL"

    # ── USD values ─────────────────────────────────────────────
    sol_usd_val = sol_usd(sol_amt)
    tok_usd_val = usd_val(tok_amt, tok_mint)
    total_usd   = tok_usd_val or sol_usd_val

    # Price per token
    if tok_amt and sol_amt:
        price_per = sol_amt / tok_amt * prices.get(SOL_MINT, 0) if prices.get(SOL_MINT) else 0
    elif tok_amt and prices.get(tok_mint):
        price_per = prices[tok_mint]
    else:
        price_per = 0

    # ── Market cap + token age ────────────────────────────────
    mc = _mc_cache.get(main_mint, 0)
    mc_str = f"MC: <b>{fmt_mc(mc)}</b> | " if mc else ""
    created_ts = _created_cache.get(main_mint, 0)
    age_str = f"Seen: <b>{fmt_age(created_ts)}</b> | " if created_ts and fmt_age(created_ts) else ""

    # ── Format the swap line ───────────────────────────────────
    # Show USD values rather than raw SOL amounts
    in_usd    = sol_usd_val or usd_val(tok_sent_raw, tok_sent_mint)
    out_usd   = tok_usd_val
    # Helper: make any token name a clickable Solscan link
    def token_link(sym: str, mint: str) -> str:
        if mint:
            return f'<a href="https://solscan.io/token/{mint}"><b>{sym}</b></a>'
        return f"<b>{sym}</b>"

    SOL_LINK = token_link("SOL", SOL_MINT)
    main_link = token_link(main_sym, main_mint)

    USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    USDC_LINK = token_link("USDC", USDC_MINT)
    # SOL swap: "0.0707 SOL ($5.78)"  |  USDC swap: "11.59 USDC ($11.59)"
    if sol_amt and in_usd >= 0.01:
        in_usd_str = f"<b>{sol_amt:.4f}</b> {SOL_LINK} (<b>{fmt_usd(in_usd)}</b>)"
    elif in_usd >= 0.01:
        in_usd_str = f"<b>{in_usd:.2f}</b> {USDC_LINK} (<b>{fmt_usd(in_usd)}</b>)"
    else:
        in_usd_str = ""
    out_usd_str = f"(<b>{fmt_usd(out_usd)}</b>)"  if out_usd >= 0.01 else ""
    fee_str     = f" [fee {fee_sol:.4f} {SOL_LINK}]"   if fee_sol > 0.0001 else ""
    tok_str     = f"<b>{format_amount(tok_amt)}</b>" if tok_amt else "<b>?</b>"
    price_str   = f"@ <b>{fmt_usd(price_per)}</b>" if price_per else ""

    if is_buy:
        swap_line = (f"💎 <b>{label}</b> swapped {in_usd_str} for "
                     f"{tok_str} {main_link} {out_usd_str} {price_str}{fee_str}".strip())
    else:
        swap_line = (f"💎 <b>{label}</b> swapped {tok_str} {main_link} {out_usd_str} for "
                     f"{in_usd_str} {price_str}{fee_str}".strip())

    # ── Holdings line ──────────────────────────────────────────
    if balance > 0:
        holds_str = f"🤚 Holds: <b>{format_amount(balance)} {main_link}</b> total"
    elif balance == 0 and not is_buy and main_mint:
        holds_str = f"🤚 Holds: <b>0 {main_link}</b> (fully sold)"
    else:
        holds_str = ""

    # ── PnL vs first detected buy ──────────────────────────────
    pnl_str = ""
    if is_buy and price_per and main_mint:
        first = get_first_buy(address, main_mint)
        if first is None:
            save_first_buy(address, main_mint, price_per)
        else:
            entry_price = first[0]
            if entry_price and entry_price > 0:
                pct = (price_per - entry_price) / entry_price * 100
                sign = "+" if pct >= 0 else ""
                emoji = "📈" if pct >= 0 else "📉"
                pnl_str = f"{emoji} PnL vs entry: <b>{sign}{pct:.1f}%</b>"

    # ── Source / DEX ───────────────────────────────────────────
    source = tx.get("source", "DEX").replace("_", " ").title()
    sig    = tx.get("signature", "")

    # ── Assemble message ───────────────────────────────────────
    lines = [
        f"{action_emoji} <b>{action_word}</b> {main_link} on {source}",
        f"💎 <b>{label}</b>",
        f"<code>{address}</code>",
        "",
        swap_line,
    ]
    if holds_str:
        lines.append(holds_str)
    if pnl_str:
        lines.append(pnl_str)
    lines += [
        "",
        f"🟡 <b>#{main_sym}</b> | {mc_str}{age_str}"
        + (f'<a href="https://dexscreener.com/solana/{main_mint}">DexS</a> · <a href="https://gmgn.ai/sol/token/{main_mint}">GMGN</a>' if main_mint else ""),
        f"<code>{main_mint}</code>",
        "",
        f'🔗 <a href="https://solscan.io/tx/{sig}">Solscan</a>',
    ]

    text = "\n".join(lines)

    # ── Inline buttons ─────────────────────────────────────────
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"🦎 GMGN",   url=f"https://gmgn.ai/sol/token/{main_mint}"),
        InlineKeyboardButton(f"⚡ Trojan",  url=f"https://t.me/solana_trojanbot?start=r-ref_{main_mint}"),
        InlineKeyboardButton(f"🌸 Bloom",   url=f"https://t.me/BloomSolana_bot?start={main_mint}"),
    ]])

    return text, keyboard

def format_transfer(tx: dict, label: str, address: str) -> str:
    token_xfers = tx.get("tokenTransfers", [])
    native_xfers = tx.get("nativeTransfers", [])
    sig = tx.get("signature", "")

    # ── Skip transfers that are really swaps / DEX activity ─────
    # Any tx where the wallet has token transfers is almost certainly a swap
    # or DEX interaction — the SWAP formatter handles these properly.
    # We only want to show pure SOL-only transfers here.
    wallet_tok_xfers = [x for x in token_xfers
                        if x.get("fromUserAccount") == address or x.get("toUserAccount") == address]
    if wallet_tok_xfers:
        return None  # has token activity → not a simple transfer
    # ────────────────────────────────────────────────────────────

    # ── Spam / dust filter ──────────────────────────────────────
    # If many different accounts received SOL in one tx, it's a mass airdrop/spam
    native_recipients = {x.get("toUserAccount") for x in native_xfers}
    if len(native_recipients) > MAX_TRANSFER_RECIPIENTS:
        return None  # skip — looks like a bot spray

    # Skip incoming SOL transfers below the minimum threshold
    incoming_sol = sum(
        float(x.get("amount", 0)) / 1e9
        for x in native_xfers
        if x.get("toUserAccount") == address
    )
    outgoing_sol = sum(
        float(x.get("amount", 0)) / 1e9
        for x in native_xfers
        if x.get("fromUserAccount") == address
    )
    # Skip any dust/spam transfer where total SOL is tiny and no meaningful tokens
    total_sol = incoming_sol + outgoing_sol
    wallet_tokens = [x for x in token_xfers
                     if x.get("fromUserAccount") == address or x.get("toUserAccount") == address]
    if total_sol < MIN_INCOMING_SOL and not wallet_tokens:
        return None
    # ────────────────────────────────────────────────────────────

    lines = []

    # Only show transfers the tracked wallet is directly part of
    wallet_token_xfers = [
        x for x in token_xfers
        if x.get("fromUserAccount") == address or x.get("toUserAccount") == address
    ]
    for xfer in wallet_token_xfers[:4]:
        amount = format_amount(xfer.get("tokenAmount", 0))
        symbol = xfer.get("symbol") or short(xfer.get("mint", ""))
        frm = short(xfer.get("fromUserAccount", ""))
        to = short(xfer.get("toUserAccount", ""))
        direction = "📥 Received" if xfer.get("toUserAccount", "") == address else "📤 Sent"
        lines.append(f"{direction} <b>{amount} {symbol}</b>  {frm} → {to}")

    wallet_native_xfers = [
        x for x in native_xfers
        if x.get("fromUserAccount") == address or x.get("toUserAccount") == address
    ]
    for xfer in wallet_native_xfers[:2]:
        amount = float(xfer.get("amount", 0)) / 1e9
        if amount < 0.0001:
            continue
        frm = short(xfer.get("fromUserAccount", ""))
        to = short(xfer.get("toUserAccount", ""))
        direction = "📥 Received" if xfer.get("toUserAccount", "") == address else "📤 Sent"
        lines.append(f"{direction} <b>{amount:.4f} SOL</b>  {frm} → {to}")

    if not lines:
        return None  # Nothing worth alerting

    body = "\n".join(lines)
    return (
        f"💸 <b>TRANSFER</b>\n"
        f"👤 <b>{label}</b>  <code>{short(address)}</code>\n\n"
        f"{body}\n\n"
        f'🔗 <a href="https://solscan.io/tx/{sig}">View on Solscan</a>'
    )

def format_generic(tx: dict, label: str, address: str) -> str:
    tx_type = tx.get("type", "UNKNOWN").replace("_", " ").title()
    desc = tx.get("description", "")
    sig = tx.get("signature", "")

    # Skip tiny SOL transfers described in the description field
    if desc:
        import re
        sol_match = re.search(r"transferred.*?([\d.]+)\s*SOL", desc, re.IGNORECASE)
        if sol_match:
            try:
                sol_amt = float(sol_match.group(1))
                if sol_amt < MIN_INCOMING_SOL:
                    return None
            except ValueError:
                pass

    msg = (
        f"⚡ <b>{tx_type}</b>\n"
        f"👤 <b>{label}</b>  <code>{short(address)}</code>\n"
    )
    if desc:
        msg += f"\n{desc[:200]}\n"
    msg += f'\n🔗 <a href="https://solscan.io/tx/{sig}">View on Solscan</a>'
    return msg

# Transaction types to alert on (add/remove as you like)
ALERT_TYPES = {
    "SWAP",
    "TRANSFER",
    "TOKEN_MINT",
    "BURN",
    "COMPRESSED_NFT_TRANSFER",
    "NFT_SALE",
    "NFT_MINT",
    "STAKE_SOL",
    "UNSTAKE_SOL",
}

async def format_transaction(tx: dict, label: str, address: str,
                             syms: dict = {}, prices: dict = {}):
    """
    Route a transaction to the right formatter.
    Returns (text, reply_markup) or None if not alertable.
    """
    tx_type = tx.get("type", "UNKNOWN")

    if tx_type not in ALERT_TYPES:
        return None

    # ── Detect if this tx has swap-like token activity ──────────
    # Helius sometimes types swaps as "TRANSFER" — detect and route to swap formatter
    tok_xfers = tx.get("tokenTransfers", [])
    SOL_MINT = "So11111111111111111111111111111111111111112"
    _BASE = {SOL_MINT, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
             "Es9vMFrzaCERmKfreVB8xSJux2KQ9pCUhZzQqau6t1Hn",
             "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So"}
    wallet_tok_xfers = [x for x in tok_xfers
                        if (x.get("fromUserAccount") == address or x.get("toUserAccount") == address)
                        and x.get("mint") not in _BASE]
    is_swap_like = tx_type == "SWAP" or (tx_type == "TRANSFER" and len(wallet_tok_xfers) > 0)

    if is_swap_like:
        main_mint = ""
        # First try: token received by wallet (buy side)
        for xfer in tok_xfers:
            if xfer.get("toUserAccount") == address and xfer.get("mint") and xfer["mint"] not in _BASE:
                main_mint = xfer["mint"]
                break
        # Fallback: token sent by wallet (sell side)
        if not main_mint:
            for xfer in tok_xfers:
                if xfer.get("fromUserAccount") == address and xfer.get("mint") and xfer["mint"] not in _BASE:
                    main_mint = xfer["mint"]
                    break
        if not main_mint:
            # No non-base token found — skip entirely (USDC/SOL shuffle)
            return None
        balance = 0.0
        if main_mint:
            if main_mint not in _created_cache:
                await get_token_age(main_mint)
            balance = await get_wallet_token_balance(address, main_mint)
        return format_swap(tx, label, address, syms, prices, balance)

    if tx_type == "TRANSFER":
        text = format_transfer(tx, label, address)
        if text is None:
            return None
        return text, None
    text = format_generic(tx, label, address)
    if text is None:
        return None
    return text, None   # no keyboard for non-swap alerts


# ─────────────────────────────────────────
# HELIUS WEBHOOK (real-time)
# ─────────────────────────────────────────

_webhook_app_ref = None  # will hold the Telegram Application

async def webhook_handler(request):
    """Handle incoming Helius webhook POST with parsed transactions."""
    try:
        body = await request.json()
        txs = body if isinstance(body, list) else [body]
        app = _webhook_app_ref
        if not app:
            return web.Response(status=200)

        wallets = {addr: label for addr, label, _ in get_wallets()}
        chats = get_chats()

        for tx in txs:
            # Determine which tracked wallet this tx belongs to
            address = ""
            label = ""
            for acc in tx.get("accountData", []):
                a = acc.get("account", "")
                if a in wallets:
                    address = a
                    label = wallets[a]
                    break
            if not address:
                # Check token/native transfers for tracked wallets
                for xfer in tx.get("tokenTransfers", []) + tx.get("nativeTransfers", []):
                    for key in ("fromUserAccount", "toUserAccount"):
                        a = xfer.get(key, "")
                        if a in wallets:
                            address = a
                            label = wallets[a]
                            break
                    if address:
                        break
            if not address:
                continue

            # Update cursor so polling doesn't re-send
            sig = tx.get("signature", "")
            if sig:
                update_last_signature(address, sig)

            # ── Dedup: skip if we already sent this signature ──
            if sig and sig in _processed_sigs:
                continue
            # ───────────────────────────────────────────────────

            syms, prices = await resolve_symbols(tx)
            result = await format_transaction(tx, label, address, syms, prices)
            if result is None:
                continue

            msg, keyboard = result

            # ── Mark as processed BEFORE sending ──
            if sig:
                _processed_sigs.add(sig)
                # Keep set bounded to last 2000 entries
                if len(_processed_sigs) > 2000:
                    _processed_sigs.clear()
            # ──────────────────────────────────────

            for chat_id in chats:
                try:
                    await app.bot.send_message(
                        chat_id=chat_id, text=msg, parse_mode="HTML",
                        disable_web_page_preview=True, reply_markup=keyboard,
                    )
                except Exception as e:
                    print(f"[Webhook→TG] Failed: {e}")

    except Exception as e:
        print(f"[Webhook] Error: {e}")
    return web.Response(status=200)

async def register_helius_webhook(wallets: list[str]):
    """Create or update a Helius webhook for the tracked wallets."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # List existing webhooks
            resp = await client.get(
                f"https://api.helius.xyz/v0/webhooks?api-key={HELIUS_API_KEY}"
            )
            existing = resp.json() if resp.status_code == 200 else []

            # Find our webhook
            our_hook = None
            for wh in existing:
                if wh.get("webhookURL", "").endswith(f":{WEBHOOK_PORT}/helius"):
                    our_hook = wh
                    break

            # Get server public IP for webhook URL
            ip_resp = await client.get("https://api.ipify.org")
            public_ip = ip_resp.text.strip()
            webhook_url = f"http://{public_ip}:{WEBHOOK_PORT}/helius"

            payload = {
                "webhookURL": webhook_url,
                "transactionTypes": ["Any"],
                "accountAddresses": wallets,
                "webhookType": "enhanced",
            }

            if our_hook:
                # Update existing
                hook_id = our_hook["webhookID"]
                resp = await client.put(
                    f"https://api.helius.xyz/v0/webhooks/{hook_id}?api-key={HELIUS_API_KEY}",
                    json=payload,
                )
                print(f"[Webhook] Updated: {webhook_url} → {len(wallets)} wallets")
            else:
                # Create new
                resp = await client.post(
                    f"https://api.helius.xyz/v0/webhooks?api-key={HELIUS_API_KEY}",
                    json=payload,
                )
                print(f"[Webhook] Created: {webhook_url} → {len(wallets)} wallets")

    except Exception as e:
        print(f"[Webhook] Registration failed: {e}")

async def start_webhook_server():
    """Start aiohttp server to receive Helius webhooks."""
    app = web.Application()
    app.router.add_post("/helius", webhook_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    print(f"🔔 Webhook server listening on port {WEBHOOK_PORT}")

    # Register webhook with Helius
    wallets_list = [addr for addr, _, _ in get_wallets()]
    if wallets_list:
        await register_helius_webhook(wallets_list)


# ─────────────────────────────────────────
# TRACKER LOOP (fallback — catches anything webhook missed)
# ─────────────────────────────────────────

async def track_wallets(app: Application):
    """Background loop: polls wallets and sends Telegram alerts on new activity."""
    print("👁  Tracker started — polling every", POLL_INTERVAL, "seconds (fallback)")
    await asyncio.sleep(3)  # Let the bot fully start first

    while True:
        wallets = get_wallets()

        for address, label, last_sig in wallets:
            try:
                txs = await fetch_transactions(address, limit=10)
                if not txs:
                    await asyncio.sleep(1)
                    continue

                # First time seeing this wallet — just save the cursor
                if last_sig is None:
                    update_last_signature(address, txs[0]["signature"])
                    print(f"[Init] {label} — cursor set to {txs[0]['signature'][:12]}...")
                    await asyncio.sleep(1)
                    continue

                # Find transactions newer than the last known one
                new_txs = []
                for tx in txs:
                    if tx["signature"] == last_sig:
                        break
                    new_txs.append(tx)

                if not new_txs:
                    await asyncio.sleep(1)
                    continue

                # Update cursor to the newest tx
                update_last_signature(address, new_txs[0]["signature"])
                chats = get_chats()

                # Alert for each new transaction (oldest first)
                for tx in reversed(new_txs):
                    tx_sig = tx.get("signature", "")

                    # ── Dedup: skip if webhook already sent this ──
                    if tx_sig and tx_sig in _processed_sigs:
                        continue
                    # ──────────────────────────────────────────────

                    # Resolve token symbols + USD prices before formatting
                    syms, prices = await resolve_symbols(tx)
                    result = await format_transaction(tx, label, address, syms, prices)
                    if result is None:
                        continue

                    msg, keyboard = result

                    # ── Mark as processed BEFORE sending ──
                    if tx_sig:
                        _processed_sigs.add(tx_sig)
                        if len(_processed_sigs) > 2000:
                            _processed_sigs.clear()
                    # ──────────────────────────────────────

                    for chat_id in chats:
                        try:
                            await app.bot.send_message(
                                chat_id=chat_id,
                                text=msg,
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                                reply_markup=keyboard,
                            )
                        except Exception as e:
                            print(f"[Telegram] Failed to send to {chat_id}: {e}")

                    await asyncio.sleep(0.3)  # slight delay between messages

            except Exception as e:
                print(f"[Tracker] Error on {label}: {e}")

        await asyncio.sleep(POLL_INTERVAL)


# ─────────────────────────────────────────
# BOT COMMANDS
# ─────────────────────────────────────────

HELP_TEXT = (
    "👁 <b>Solana Wallet Tracker</b>\n\n"
    "<b>Commands:</b>\n"
    "/add <code>ADDRESS</code> <code>Label</code> — Start tracking a wallet\n"
    "/remove <code>ADDRESS</code> — Stop tracking a wallet\n"
    "/list — Show all tracked wallets\n"
    "/help — Show this message\n\n"
    "<i>Alerts for: swaps, transfers, mints, burns, NFT activity</i>"
)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_chat(update.effective_chat.id)
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML")

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_chat(update.effective_chat.id)
    args = context.args

    if not args:
        await update.message.reply_text(
            "Usage: /add <code>WALLET_ADDRESS</code> <code>Label</code>\n"
            "Example: /add So11...abc MyWhale",
            parse_mode="HTML",
        )
        return

    address = args[0].strip()
    label = " ".join(args[1:]) if len(args) > 1 else short(address)

    if not (32 <= len(address) <= 44):
        await update.message.reply_text("❌ That doesn't look like a valid Solana address.")
        return

    if add_wallet(address, label):
        await update.message.reply_text(
            f"✅ Now tracking <b>{label}</b>\n<code>{address}</code>",
            parse_mode="HTML",
        )
        # Update Helius webhook with new wallet
        all_addrs = [a for a, _, _ in get_wallets()]
        await register_helius_webhook(all_addrs)
    else:
        await update.message.reply_text("⚠️ That wallet is already being tracked.")

async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /remove <code>WALLET_ADDRESS</code>", parse_mode="HTML")
        return

    address = context.args[0].strip()
    if remove_wallet(address):
        await update.message.reply_text(
            f"🗑 Stopped tracking <code>{address}</code>", parse_mode="HTML"
        )
    else:
        await update.message.reply_text("❌ Wallet not found. Use /list to see tracked wallets.")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallets = get_wallets()
    if not wallets:
        await update.message.reply_text(
            "No wallets tracked yet.\nUse /add <code>ADDRESS</code> <code>Label</code> to add one.",
            parse_mode="HTML",
        )
        return

    lines = "\n".join(
        f"• <b>{label}</b>\n  <code>{addr}</code>" for addr, label, _ in wallets
    )
    await update.message.reply_text(
        f"👁 <b>Tracked Wallets ({len(wallets)})</b>\n\n{lines}",
        parse_mode="HTML",
    )


# ─────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────

async def main():
    if HELIUS_API_KEY == "YOUR_HELIUS_API_KEY":
        print("❌ Please set your HELIUS_API_KEY in bot.py before running.")
        return
    if TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        print("❌ Please set your TELEGRAM_BOT_TOKEN in bot.py before running.")
        return

    init_db()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))

    print("🤖 Bot is running. Press Ctrl+C to stop.")

    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

        # Start Helius webhook server for real-time alerts
        global _webhook_app_ref
        _webhook_app_ref = app
        await start_webhook_server()

        # Start the wallet tracker as fallback
        tracker = asyncio.create_task(track_wallets(app))

        # Keep running until Ctrl+C
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            tracker.cancel()
            await app.updater.stop()
            await app.stop()

if __name__ == "__main__":
    asyncio.run(main())