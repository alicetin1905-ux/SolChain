"""Paper copy-trading: follow the top wallets from the leaderboard and simulate copying their swaps.

No real trades are made. Each run reads the followed wallets' new transactions from Solana RPC,
turns their swaps into simulated copy trades, marks open positions to the current Dexscreener price,
and writes the result to data/paper.json for the site.

Settings (environment):
  SOL_RPC        Solana RPC URL (falls back to the free public endpoint)
  FOLLOW         number of leaderboard wallets to follow (default 10)
  TRADE_SOL      SOL per copied buy (default 1)
  SLIPPAGE_PCT   price penalty per fill for arriving after the leader (default 3)
  FEE_SOL        network + priority fee per fill (default 0.001)
"""
import calendar, json, os, ssl, sys, time, urllib.request, collections
from concurrent.futures import ThreadPoolExecutor

RPC = os.environ.get("SOL_RPC") or "https://solana-rpc.publicnode.com"
DS = "https://api.dexscreener.com"
WSOL = "So11111111111111111111111111111111111111112"
STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY5NNmFfeQe36Ci"}
FOLLOW = int(os.environ.get("FOLLOW") or 10)
TRADE_SOL = float(os.environ.get("TRADE_SOL") or 1)
SLIP = float(os.environ.get("SLIPPAGE_PCT") or 3) / 100
FEE_SOL = float(os.environ.get("FEE_SOL") or 0.001)
MIN_LIQ_USD = 25_000
DATA = sys.argv[1] if len(sys.argv) > 1 else "data"
STATE = f"{DATA}/paper_state.json"
try:  # macOS Pythons often can't find the system certificates; certifi ships its own
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()
POOL = ThreadPoolExecutor(int(os.environ.get("RPC_THREADS") or 2))


def get(url, body=None, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"content-type": "application/json", "user-agent": "solchain-paper/1.0"})
            with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
                return json.load(r)
        except Exception as e:
            print(f"  retry {i+1} after {e}", file=sys.stderr)
            time.sleep(2 ** i)
    raise RuntimeError(f"failed: {url.split('?')[0]}")  # never log an API key in the query


def rpc(method, params):
    for attempt in range(6):
        r = get(RPC, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if "error" not in r:
            return r.get("result")
        if r["error"].get("code") not in (-32005, -32429, 429, -32603):
            print(f"  rpc error: {str(r['error'])[:160]}", file=sys.stderr)
            return None
        time.sleep(1 + 2 ** attempt)
    return None


def epoch(iso):
    return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))


def now_iso(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t or time.time()))


def sol_usd():
    pairs = [p for p in get(f"{DS}/tokens/v1/solana/{WSOL}") if p["quoteToken"]["address"] in STABLES and p["baseToken"]["address"] == WSOL]
    return float(max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)["priceUsd"])


def token_info(mints):
    """Current price (USD), liquidity and symbol of each mint's deepest Dexscreener pair."""
    out, mints = {}, list(mints)
    for i in range(0, len(mints), 30):
        for p in get(f"{DS}/tokens/v1/solana/{','.join(mints[i:i+30])}"):
            m = p["baseToken"]["address"]
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if m not in out or liq > out[m]["liquidityUsd"]:
                out[m] = {"priceUsd": float(p.get("priceUsd") or 0), "liquidityUsd": liq,
                          "symbol": p["baseToken"]["symbol"], "url": p.get("url")}
    return out


def parse_swap(tx, wallet, sol_price):
    """The wallet's swap in this transaction: mint, token delta, SOL value, token balance before and decimals."""
    meta = tx.get("meta")
    if not meta or meta.get("err"):
        return None
    keys = tx["transaction"]["message"]["accountKeys"]
    pk = [k["pubkey"] if isinstance(k, dict) else k for k in keys]
    signers = [k["pubkey"] for k in keys if isinstance(k, dict) and k.get("signer")] or pk[:1]
    if wallet not in signers:
        return None
    d, pre_bal, decimals = collections.defaultdict(float), collections.defaultdict(float), {}
    opened = closed = 0
    pre = {b["accountIndex"]: b for b in meta.get("preTokenBalances", []) if b.get("owner") == wallet}
    post = {b["accountIndex"]: b for b in meta.get("postTokenBalances", []) if b.get("owner") == wallet}
    for idx in set(pre) | set(post):
        mint = (post.get(idx) or pre.get(idx))["mint"]
        decimals[mint] = (post.get(idx) or pre.get(idx))["uiTokenAmount"].get("decimals")
        a = float((post.get(idx) or {}).get("uiTokenAmount", {}).get("uiAmount") or 0)
        b = float((pre.get(idx) or {}).get("uiTokenAmount", {}).get("uiAmount") or 0)
        d[mint] += a - b
        pre_bal[mint] += b
        opened += idx not in pre; closed += idx not in post
    tokens = [m for m, v in d.items() if m not in (WSOL, *STABLES) and abs(v) > 0]
    if len(tokens) != 1:
        return None  # nothing traded, or several tokens at once
    mint = tokens[0]
    i0 = pk.index(wallet)
    dsol = (meta["postBalances"][i0] - meta["preBalances"][i0] + (meta.get("fee", 0) if i0 == 0 else 0)) / 1e9
    dsol += (opened - closed) * 0.00203928 + d[WSOL]
    dsol += sum(d[m] for m in STABLES) / sol_price
    if d[mint] * dsol >= 0 or abs(dsol) < 0.01:
        return None  # not a buy or sell against SOL/stables, or too small to matter
    return {"mint": mint, "dtok": d[mint], "sol": abs(dsol), "pre": pre_bal[mint], "decimals": decimals.get(mint)}


def load_state():
    try:
        return json.load(open(STATE))
    except FileNotFoundError:
        return {"startedAt": now_iso(), "wallets": {}, "positions": [], "trades": [], "equity": [], "skipped": 0}


def follow_list(st):
    """Top wallets from the leaderboard; marks who is followed and returns (top, wallets to watch)."""
    traders = json.load(open(f"{DATA}/traders.json"))
    top = [r["wallet"] for r in sorted((r for r in traders if r["botLike"] == "no"), key=lambda r: -r["pnlUsd"])[:FOLLOW]]
    for w in top:
        st["wallets"].setdefault(w, {"since": now_iso(), "lastSig": None})
    for w, info in st["wallets"].items():
        info["following"] = w in top
    holders = {p["wallet"] for p in st["positions"]}
    return top, list(dict.fromkeys(top + sorted(holders)))  # keep watching wallets we still hold a copy of


def fetch_tx(sig, commitment="finalized"):
    return rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1, "commitment": commitment}])


def poll_events(st, watch, sp):
    """Swaps the watched wallets made since the last check, oldest first."""
    events = []
    for w in watch:
        info = st["wallets"][w]
        opts = {"limit": 100, **({"until": info["lastSig"]} if info["lastSig"] else {})}
        sigs = rpc("getSignaturesForAddress", [w, opts]) or []
        if not sigs:
            continue
        first = info["lastSig"] is None
        info["lastSig"] = sigs[0]["signature"]
        if first:
            continue  # first sight: start copying from now on, no backfill
        new = [s for s in sigs if s.get("err") is None]
        for s, tx in zip(new, POOL.map(lambda s: fetch_tx(s["signature"]), new)):
            sw = tx and parse_swap(tx, w, sp)
            if sw:
                events.append({**sw, "time": tx.get("blockTime") or s.get("blockTime") or 0, "sig": s["signature"], "wallet": w})
    return sorted(events, key=lambda e: e["time"])


def model_fill(side, ev, qty):
    """Fill price in SOL per token: the leader's own price, made worse by SLIPPAGE_PCT."""
    px = ev["sol"] / abs(ev["dtok"])
    return px * (1 + SLIP) if side == "buy" else px * (1 - SLIP), "model"


def apply_events(st, events, info, fill=model_fill):
    """Turn the leaders' swaps into simulated copy trades. `fill(side, event, qty)` returns (price, source)."""
    open_pos = {(p["wallet"], p["mint"]): p for p in st["positions"]}
    for ev in events:
        w, mint = ev["wallet"], ev["mint"]
        ti = info.get(mint, {})
        key = (w, mint)
        lag = round(time.time() - ev["time"], 1) if ev.get("live") else None
        if ev["dtok"] > 0:  # leader bought
            if key in open_pos or not st["wallets"][w].get("following"):
                continue
            if ti.get("liquidityUsd", 0) < MIN_LIQ_USD:
                st["skipped"] += 1
                continue
            px, src = fill("buy", ev, None)
            if not px:
                continue
            qty = (TRADE_SOL - FEE_SOL) / px
            sym = ti.get("symbol") or mint[:4] + "…"
            open_pos[key] = {"wallet": w, "mint": mint, "symbol": sym, "openedAt": now_iso(ev["time"]), "entrySol": TRADE_SOL,
                             "qty": qty, "qtyLeft": qty, "entryPrice": px, "realizedSol": 0.0, "leaderSig": ev["sig"],
                             "decimals": ev.get("decimals")}
            st["trades"].append({"time": now_iso(ev["time"]), "wallet": w, "symbol": sym, "mint": mint, "side": "buy",
                                 "sol": round(TRADE_SOL, 4), "price": px, "fill": src, "lagSec": lag, "leaderSig": ev["sig"]})
        else:  # leader sold
            p = open_pos.get(key)
            if not p:
                continue
            frac = min(1.0, abs(ev["dtok"]) / ev["pre"]) if ev["pre"] > 0 else 1.0
            q = p["qtyLeft"] if frac > 0.98 else p["qtyLeft"] * frac
            if frac <= 0.98 and q * ev["sol"] / abs(ev["dtok"]) < 0.02:
                continue  # a tiny partial sell: fees would eat it, so the copy waits for a bigger one
            px, src = fill("sell", ev, q)
            if px is None:
                continue
            got = q * px - FEE_SOL
            p["qtyLeft"] -= q
            p["realizedSol"] += got
            st["trades"].append({"time": now_iso(ev["time"]), "wallet": w, "symbol": p["symbol"], "mint": mint, "side": "sell",
                                 "sol": round(got, 4), "price": px, "fraction": round(frac, 3), "fill": src, "lagSec": lag,
                                 "leaderSig": ev["sig"]})
            if p["qtyLeft"] <= p["qty"] * 0.001:
                p["closedAt"] = now_iso(ev["time"])
                p["pnlSol"] = p["realizedSol"] - p["entrySol"]
                st.setdefault("closed", []).append(p)
                del open_pos[key]
    st["positions"] = list(open_pos.values())


def publish(st, sp, top, engine):
    """Mark open positions to the current price and write the state and the site's data/paper.json."""
    info = token_info({p["mint"] for p in st["positions"]}) if st["positions"] else {}
    unreal = 0.0
    for p in st["positions"]:
        cur = ((info.get(p["mint"]) or {}).get("priceUsd") or 0) / sp
        p["priceNow"] = cur
        p["valueSol"] = p["qtyLeft"] * cur
        p["pnlSol"] = p["realizedSol"] + p["valueSol"] - p["entrySol"]
        unreal += p["pnlSol"]
    positions = st["positions"]
    closed = st.get("closed", [])
    realized = sum(p["pnlSol"] for p in closed)
    point = {"t": now_iso(), "realizedSol": round(realized, 4), "openPnlSol": round(unreal, 4), "open": len(positions)}
    # live runs publish often; keep at most one equity point per 10 minutes
    if st["equity"] and time.time() - epoch(st["equity"][-1]["t"]) < 600:
        st["equity"][-1] = point
    else:
        st["equity"].append(point)
    st["equity"] = st["equity"][-6 * 24 * 60:]
    st["trades"] = st["trades"][-2000:]
    st["closed"] = closed[-1000:]
    st["solUsd"] = sp
    st["engine"] = engine
    st["updatedAt"] = now_iso()
    json.dump(st, open(STATE, "w"))

    by_wallet = collections.defaultdict(lambda: {"pnlSol": 0.0, "positions": 0, "wins": 0, "closed": 0})
    for p in closed + positions:
        b = by_wallet[p["wallet"]]
        b["pnlSol"] += p["pnlSol"]; b["positions"] += 1
        if "closedAt" in p:
            b["closed"] += 1; b["wins"] += p["pnlSol"] > 0
    wallets = [{"wallet": w, "following": i.get("following", False), "since": i["since"],
                "pnlSol": round(by_wallet[w]["pnlSol"], 4), "positions": by_wallet[w]["positions"],
                "closed": by_wallet[w]["closed"], "wins": by_wallet[w]["wins"]}
               for w, i in st["wallets"].items() if i.get("following") or w in by_wallet]
    wins = sum(p["pnlSol"] > 0 for p in closed)
    lags = [t["lagSec"] for t in st["trades"] if t.get("lagSec") is not None]
    summary = {"startedAt": st["startedAt"], "updatedAt": st["updatedAt"], "engine": engine, "solUsd": sp,
               "tradeSol": TRADE_SOL, "slippagePct": SLIP * 100, "feeSol": FEE_SOL, "following": len(top),
               "realizedSol": round(realized, 4), "openPnlSol": round(unreal, 4), "totalPnlSol": round(realized + unreal, 4),
               "openPositions": len(positions), "closedPositions": len(closed),
               "winRatePct": round(100 * wins / len(closed), 1) if closed else None,
               "capitalInUseSol": round(sum(p["entrySol"] for p in positions), 2),
               "skippedLowLiquidity": st["skipped"],
               "medianLagSec": sorted(lags)[len(lags) // 2] if lags else None}
    keep = ("wallet", "mint", "symbol", "openedAt", "entrySol", "entryPrice", "realizedSol", "pnlSol", "qtyLeft")
    pub = lambda p: {k: p[k] for k in keep if k in p} | (
        {"closedAt": p["closedAt"]} if "closedAt" in p else {"priceNow": p.get("priceNow"), "valueSol": round(p.get("valueSol", 0), 4)})
    json.dump({"summary": summary, "wallets": wallets, "positions": [pub(p) for p in positions],
               "closed": [pub(p) for p in closed[-200:]][::-1], "trades": st["trades"][-200:][::-1],
               "equity": st["equity"]}, open(f"{DATA}/paper.json", "w"))
    print(f"open {len(positions)}, closed {len(closed)}, total PnL {realized + unreal:+.4f} SOL", file=sys.stderr)


def live_engine_active(st):
    """True while the Mac engine (scripts/live.py) has published in the last 30 minutes."""
    if st.get("engine") != "live" or not st.get("updatedAt"):
        return False
    return time.time() - epoch(st["updatedAt"]) < 1800


def main():
    st = load_state()
    if live_engine_active(st):
        print("live engine is running; hourly check skipped", file=sys.stderr)
        return
    sp = sol_usd()
    top, watch = follow_list(st)
    print(f"SOL ${sp:.2f}; following {len(top)} wallets, watching {len(watch)}", file=sys.stderr)
    events = poll_events(st, watch, sp)
    print(f"{len(events)} new swaps by watched wallets", file=sys.stderr)
    info = token_info({e["mint"] for e in events}) if events else {}
    apply_events(st, events, info)
    publish(st, sp, top, "hourly")


if __name__ == "__main__":
    main()
