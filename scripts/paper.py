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
import json, os, sys, time, urllib.request, collections
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
POOL = ThreadPoolExecutor(int(os.environ.get("RPC_THREADS") or 2))


def get(url, body=None, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"content-type": "application/json", "user-agent": "solchain-paper/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
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
    """The wallet's swap in this transaction as (mint, token_delta, sol_value, pre_balance), or None."""
    meta = tx.get("meta")
    if not meta or meta.get("err"):
        return None
    keys = tx["transaction"]["message"]["accountKeys"]
    pk = [k["pubkey"] if isinstance(k, dict) else k for k in keys]
    signers = [k["pubkey"] for k in keys if isinstance(k, dict) and k.get("signer")] or pk[:1]
    if wallet not in signers:
        return None
    d, pre_bal = collections.defaultdict(float), collections.defaultdict(float)
    opened = closed = 0
    pre = {b["accountIndex"]: b for b in meta.get("preTokenBalances", []) if b.get("owner") == wallet}
    post = {b["accountIndex"]: b for b in meta.get("postTokenBalances", []) if b.get("owner") == wallet}
    for idx in set(pre) | set(post):
        mint = (post.get(idx) or pre.get(idx))["mint"]
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
    return mint, d[mint], abs(dsol), pre_bal[mint]


def load_state():
    try:
        return json.load(open(STATE))
    except FileNotFoundError:
        return {"startedAt": now_iso(), "wallets": {}, "positions": [], "trades": [], "equity": [], "skipped": 0}


def main():
    st = load_state()
    sp = sol_usd()
    traders = json.load(open(f"{DATA}/traders.json"))
    top = [r["wallet"] for r in sorted((r for r in traders if r["botLike"] == "no"), key=lambda r: -r["pnlUsd"])[:FOLLOW]]
    holders = {p["wallet"] for p in st["positions"]}
    watch = list(dict.fromkeys(top + sorted(holders)))  # keep watching wallets we still hold a copy of
    print(f"SOL ${sp:.2f}; following {len(top)} wallets, watching {len(watch)}", file=sys.stderr)

    for w in top:
        st["wallets"].setdefault(w, {"since": now_iso(), "lastSig": None})
        st["wallets"][w]["following"] = True
    for w, info in st["wallets"].items():
        if w not in top:
            info["following"] = False

    events = []  # (blockTime, sig, wallet, swap)
    for w in watch:
        info = st["wallets"][w]
        opts = {"limit": 100, **({"until": info["lastSig"]} if info["lastSig"] else {})}
        sigs = [s for s in (rpc("getSignaturesForAddress", [w, opts]) or [])]
        if not sigs:
            continue
        if info["lastSig"] is None:
            info["lastSig"] = sigs[0]["signature"]  # first sight: start copying from now on, no backfill
            continue
        info["lastSig"] = sigs[0]["signature"]
        new = [s for s in sigs if s.get("err") is None]
        txs = list(POOL.map(lambda s: rpc("getTransaction", [s["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}]), new))
        for s, tx in zip(new, txs):
            if not tx:
                continue
            sw = parse_swap(tx, w, sp)
            if sw:
                events.append((tx.get("blockTime") or s.get("blockTime") or 0, s["signature"], w, sw))
    events.sort()
    print(f"{len(events)} new swaps by watched wallets", file=sys.stderr)

    info = token_info({e[3][0] for e in events} | {p["mint"] for p in st["positions"]}) if (events or st["positions"]) else {}
    open_pos = {(p["wallet"], p["mint"]): p for p in st["positions"]}
    for t, sig, w, (mint, dtok, sol_amt, pre_bal) in events:
        px = sol_amt / abs(dtok)  # leader's fill in SOL per token
        ti = info.get(mint, {})
        sym = ti.get("symbol") or mint[:4] + "…"
        key = (w, mint)
        if dtok > 0:  # leader bought
            if key in open_pos or not st["wallets"][w].get("following"):
                continue
            if ti.get("liquidityUsd", 0) < MIN_LIQ_USD:
                st["skipped"] += 1
                continue
            fill = px * (1 + SLIP)
            qty = (TRADE_SOL - FEE_SOL) / fill
            p = {"wallet": w, "mint": mint, "symbol": sym, "openedAt": now_iso(t), "entrySol": TRADE_SOL,
                 "qty": qty, "qtyLeft": qty, "entryPrice": fill, "realizedSol": 0.0, "leaderSig": sig}
            open_pos[key] = p
            st["trades"].append({"time": now_iso(t), "wallet": w, "symbol": sym, "mint": mint, "side": "buy",
                                 "sol": round(TRADE_SOL, 4), "price": fill, "leaderSig": sig})
        else:  # leader sold
            p = open_pos.get(key)
            if not p:
                continue
            frac = min(1.0, abs(dtok) / pre_bal) if pre_bal > 0 else 1.0
            q = p["qtyLeft"] if frac > 0.98 else p["qtyLeft"] * frac
            fill = px * (1 - SLIP)
            got = q * fill - FEE_SOL
            p["qtyLeft"] -= q
            p["realizedSol"] += got
            st["trades"].append({"time": now_iso(t), "wallet": w, "symbol": p["symbol"], "mint": mint, "side": "sell",
                                 "sol": round(got, 4), "price": fill, "fraction": round(frac, 3), "leaderSig": sig})
            if p["qtyLeft"] <= p["qty"] * 0.001:
                p["closedAt"] = now_iso(t)
                p["pnlSol"] = p["realizedSol"] - p["entrySol"]
                st.setdefault("closed", []).append(p)
                del open_pos[key]

    # Mark open positions to the current price
    unreal = 0.0
    positions = []
    for p in open_pos.values():
        ti = info.get(p["mint"]) or {}
        cur = (ti.get("priceUsd") or 0) / sp
        p["priceNow"] = cur
        p["valueSol"] = p["qtyLeft"] * cur
        p["pnlSol"] = p["realizedSol"] + p["valueSol"] - p["entrySol"]
        unreal += p["pnlSol"]
        positions.append(p)
    st["positions"] = positions
    closed = st.get("closed", [])
    realized = sum(p["pnlSol"] for p in closed)
    st["equity"].append({"t": now_iso(), "realizedSol": round(realized, 4), "openPnlSol": round(unreal, 4),
                         "open": len(positions)})
    st["equity"] = st["equity"][-24 * 120:]
    st["trades"] = st["trades"][-2000:]
    st["closed"] = closed[-1000:]
    st["solUsd"] = sp
    st["updatedAt"] = now_iso()
    json.dump(st, open(STATE, "w"))

    # What the site reads
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
    summary = {"startedAt": st["startedAt"], "updatedAt": st["updatedAt"], "solUsd": sp,
               "tradeSol": TRADE_SOL, "slippagePct": SLIP * 100, "feeSol": FEE_SOL, "following": len(top),
               "realizedSol": round(realized, 4), "openPnlSol": round(unreal, 4), "totalPnlSol": round(realized + unreal, 4),
               "openPositions": len(positions), "closedPositions": len(closed),
               "winRatePct": round(100 * wins / len(closed), 1) if closed else None,
               "capitalInUseSol": round(sum(p["entrySol"] for p in positions), 2),
               "skippedLowLiquidity": st["skipped"]}
    pub = lambda p: {k: p[k] for k in ("wallet", "mint", "symbol", "openedAt", "entrySol", "entryPrice", "realizedSol", "pnlSol") if k in p} | (
        {"closedAt": p["closedAt"]} if "closedAt" in p else {"priceNow": p.get("priceNow"), "valueSol": round(p.get("valueSol", 0), 4)})
    json.dump({"summary": summary, "wallets": wallets, "positions": [pub(p) for p in positions],
               "closed": [pub(p) for p in closed[-200:]][::-1], "trades": st["trades"][-200:][::-1],
               "equity": st["equity"]}, open(f"{DATA}/paper.json", "w"))
    print(f"open {len(positions)}, closed {len(closed)}, total PnL {realized + unreal:+.4f} SOL", file=sys.stderr)


if __name__ == "__main__":
    main()
