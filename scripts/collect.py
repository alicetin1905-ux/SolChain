"""Collect top Solana traders across trending Dexscreener pairs.

Pairs come from Dexscreener's public API. Wallet-level trades are rebuilt from
on-chain swaps on each pair's pool account via Solana RPC, because Dexscreener's
own Top Traders endpoint is not public.
"""
import json, sys, time, urllib.request, collections, os
from concurrent.futures import ThreadPoolExecutor

# An empty SOL_RPC (an unset GitHub secret) falls back to the free public endpoint
RPC = os.environ.get("SOL_RPC") or "https://solana-rpc.publicnode.com"
POOL = ThreadPoolExecutor(int(os.environ.get("RPC_THREADS") or 12))
DS = "https://api.dexscreener.com"
WSOL = "So11111111111111111111111111111111111111112"
STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
           "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY5NNmFfeQe36Ci": "USDT"}
MAX_PAIRS = int(os.environ.get("MAX_PAIRS", 12))
SWAPS_PER_PAIR = int(os.environ.get("SWAPS", 500))
OUT = sys.argv[1] if len(sys.argv) > 1 else "."


def get(url, body=None, tries=6):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"content-type": "application/json", "user-agent": "top-traders/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except Exception as e:
            wait = 2 ** i
            print(f"  retry {i+1} after {e} (sleep {wait}s)", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"failed: {url.split('?')[0]}")  # never log an API key in the query


def rpc_one(call):
    m, p = call
    for attempt in range(6):
        try:
            r = get(RPC, {"jsonrpc": "2.0", "id": 1, "method": m, "params": p}, tries=3)
            if "error" not in r:
                return r.get("result")
            if r["error"].get("code") not in (-32005, -32429, 429, -32603):
                print(f"  rpc error: {str(r['error'])[:160]}", file=sys.stderr)
                return None
        except RuntimeError:
            pass
        time.sleep(1 + 2 ** attempt)
    return None


def rpc_batch(calls):
    return list(POOL.map(rpc_one, calls))


sol_pairs = [p for p in get(f"{DS}/tokens/v1/solana/{WSOL}") if p["quoteToken"]["address"] in STABLES and p["baseToken"]["address"] == WSOL]
SOL_USD = float(max(sol_pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)["priceUsd"])
print(f"SOL ${SOL_USD:.2f}", file=sys.stderr)

# 1. Candidate tokens from Dexscreener's trending feeds
addrs = []
for path in ["/token-boosts/top/v1", "/token-boosts/latest/v1", "/token-profiles/latest/v1"]:
    for t in get(DS + path):
        if t.get("chainId") == "solana" and t["tokenAddress"] not in addrs:
            addrs.append(t["tokenAddress"])
for t in get(DS + "/community-takeovers/latest/v1"):
    if t.get("chainId") == "solana" and t["tokenAddress"] not in addrs:
        addrs.append(t["tokenAddress"])
# GeckoTerminal's trending Solana pools widen the candidate list; prices still come from Dexscreener
for page in (1, 2):
    try:
        for p in get(f"https://api.geckoterminal.com/api/v2/networks/solana/trending_pools?page={page}")["data"]:
            a = p["relationships"]["base_token"]["data"]["id"].split("_", 1)[1]
            if a not in addrs: addrs.append(a)
    except Exception as e:
        print(f"geckoterminal skipped: {e}", file=sys.stderr)
print(f"{len(addrs)} candidate tokens", file=sys.stderr)

pairs = {}
for i in range(0, len(addrs), 30):
    for p in get(f"{DS}/tokens/v1/solana/{','.join(addrs[i:i+30])}"):
        if p.get("chainId") != "solana": continue
        q = p["quoteToken"]["address"]
        if q != WSOL and q not in STABLES: continue
        key = p["baseToken"]["address"]
        if key in (WSOL, *STABLES): continue
        liq = (p.get("liquidity") or {}).get("usd") or 0
        if key not in pairs or liq > ((pairs[key].get("liquidity") or {}).get("usd") or 0):
            pairs[key] = p

cands = [p for p in pairs.values()
         if ((p.get("liquidity") or {}).get("usd") or 0) >= 25_000 and (p.get("volume") or {}).get("h24", 0) >= 100_000]
cands.sort(key=lambda p: p["volume"]["h24"], reverse=True)
cands = cands[:MAX_PAIRS]
print(f"tracking {len(cands)} pairs", file=sys.stderr)

trades = []      # one row per parsed swap
pair_rows = []
for p in cands:
    base = p["baseToken"]["address"]; quote = p["quoteToken"]["address"]
    price_usd = float(p["priceUsd"]); price_native = float(p["priceNative"])
    sym = p["baseToken"]["symbol"]
    print(f"{sym}: {p['pairAddress']} vol24 {p['volume']['h24']:.0f}", file=sys.stderr)

    sigs, before = [], None
    while len(sigs) < SWAPS_PER_PAIR:
        opts = {"limit": 1000, **({"before": before} if before else {})}
        page = rpc_one(("getSignaturesForAddress", [p["pairAddress"], opts])) or []
        if not page: break
        before = page[-1]["signature"]
        sigs += [s for s in page if s.get("err") is None]
        if len(page) < 1000: break
    sigs = sigs[:SWAPS_PER_PAIR]
    print(f"  {len(sigs)} signatures", file=sys.stderr)
    parsed = 0; times = []
    for i in range(0, len(sigs), 40):
        chunk = sigs[i:i+40]
        if i % 200 == 0: print(f"  {i}/{len(sigs)}", file=sys.stderr)
        txs = rpc_batch([("getTransaction", [s["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
                         for s in chunk])
        for s, tx in zip(chunk, txs):
            if not tx or not tx.get("meta") or tx["meta"].get("err"): continue
            meta = tx["meta"]; keys = tx["transaction"]["message"]["accountKeys"]
            pk = [k["pubkey"] if isinstance(k, dict) else k for k in keys]
            signers = [k["pubkey"] for k in keys if isinstance(k, dict) and k.get("signer")] or pk[:1]
            pre_tb, post_tb = meta.get("preTokenBalances", []), meta.get("postTokenBalances", [])

            def deltas(owner):
                d = collections.defaultdict(float); opened = closed = 0
                pre = {b["accountIndex"]: b for b in pre_tb if b.get("owner") == owner}
                post = {b["accountIndex"]: b for b in post_tb if b.get("owner") == owner}
                for idx in set(pre) | set(post):
                    mint = (post.get(idx) or pre.get(idx))["mint"]
                    a = float((post.get(idx) or {}).get("uiTokenAmount", {}).get("uiAmount") or 0)
                    b = float((pre.get(idx) or {}).get("uiTokenAmount", {}).get("uiAmount") or 0)
                    d[mint] += a - b
                    opened += idx not in pre; closed += idx not in post
                return d, opened, closed

            # the trader is the signer whose balance of this token moved the most
            best = None
            for sg in signers:
                d, op, cl = deltas(sg)
                if abs(d[base]) > 0 and (best is None or abs(d[base]) > abs(best[1][base])):
                    best = (sg, d, op, cl)
            if not best: continue
            signer, d, op, cl = best
            dbase = d[base]
            i0 = pk.index(signer)
            dsol = (meta["postBalances"][i0] - meta["preBalances"][i0] + (meta.get("fee", 0) if i0 == 0 else 0)) / 1e9
            dsol += (op - cl) * 0.00203928  # token-account rent is refundable, not trading cost
            dsol += d[WSOL]
            dq_usd = dsol * SOL_USD + sum(d[m] for m in STABLES)
            other = [m for m, v in d.items() if m not in (base, WSOL, *STABLES) and abs(v) > 0]
            if other: continue  # routed through a third token: can't value it cleanly
            if dbase > 0 and dq_usd < 0:
                side = "buy"
            elif dbase < 0 and dq_usd > 0:
                side = "sell"
            else:
                continue
            usd = abs(dq_usd)
            if usd < 1: continue
            parsed += 1
            bt = tx.get("blockTime") or s.get("blockTime")
            times.append(bt)
            trades.append({"wallet": signer, "token": base, "symbol": sym, "side": side,
                           "tokens": abs(dbase), "usd": usd, "slot": tx["slot"], "time": bt})
    window_min = (max(times) - min(times)) / 60 if len(times) > 1 else 0
    pair_rows.append({
        "token": base, "symbol": sym, "name": p["baseToken"]["name"], "dex": p["dexId"],
        "pair": p["pairAddress"], "url": p["url"], "quote": STABLES.get(quote, "SOL"),
        "priceUsd": price_usd, "liquidityUsd": p["liquidity"]["usd"], "volume24hUsd": p["volume"]["h24"],
        "change24hPct": (p.get("priceChange") or {}).get("h24", 0), "marketCapUsd": p.get("marketCap") or p.get("fdv") or 0,
        "swapsSampled": parsed, "windowMinutes": round(window_min, 1),
        "windowStart": min(times) if times else None, "windowEnd": max(times) if times else None,
    })
    print(f"  parsed {parsed} swaps over {window_min:.0f} min", file=sys.stderr)

price = {r["token"]: r["priceUsd"] for r in pair_rows}

# 2. Per wallet x token positions, marked to current price
pos = collections.defaultdict(lambda: {"buyUsd": 0.0, "sellUsd": 0.0, "buyTok": 0.0, "sellTok": 0.0,
                                       "buys": 0, "sells": 0, "slots": collections.defaultdict(set)})
for t in trades:
    k = (t["wallet"], t["token"]); d = pos[k]
    if t["side"] == "buy":
        d["buyUsd"] += t["usd"]; d["buyTok"] += t["tokens"]; d["buys"] += 1
    else:
        d["sellUsd"] += t["usd"]; d["sellTok"] += t["tokens"]; d["sells"] += 1
    d["slots"][t["slot"]].add(t["side"])

sym = {r["token"]: r["symbol"] for r in pair_rows}
positions = []
wallets = collections.defaultdict(lambda: {"pnl": 0.0, "vol": 0.0, "buys": 0, "sells": 0, "tokens": 0, "wins": 0,
                                           "best": None, "bestPnl": None, "sandwich": False})
for (w, tok), d in pos.items():
    pnl = d["sellUsd"] - d["buyUsd"] + (d["buyTok"] - d["sellTok"]) * price[tok]
    sandwich = any(len(v) == 2 for v in d["slots"].values())
    positions.append({"wallet": w, "token": sym[tok], "buyUsd": round(d["buyUsd"], 2), "sellUsd": round(d["sellUsd"], 2),
                      "buys": d["buys"], "sells": d["sells"], "pnlUsd": round(pnl, 2)})
    a = wallets[w]
    a["pnl"] += pnl; a["vol"] += d["buyUsd"] + d["sellUsd"]; a["buys"] += d["buys"]; a["sells"] += d["sells"]
    a["tokens"] += 1; a["wins"] += pnl > 0; a["sandwich"] |= sandwich
    if a["bestPnl"] is None or pnl > a["bestPnl"]:
        a["best"], a["bestPnl"] = sym[tok], pnl

traders = []
for w, a in wallets.items():
    trades_n = a["buys"] + a["sells"]
    bot = a["sandwich"] or trades_n >= 40
    traders.append({"wallet": w, "pnlUsd": round(a["pnl"], 2), "volumeUsd": round(a["vol"], 2),
                    "roiPct": round(100 * a["pnl"] / a["vol"], 2) if a["vol"] else 0,
                    "trades": trades_n, "buys": a["buys"], "sells": a["sells"], "tokens": a["tokens"],
                    "winRatePct": round(100 * a["wins"] / a["tokens"], 1), "bestToken": a["best"],
                    "bestTokenPnlUsd": round(a["bestPnl"], 2), "botLike": "yes" if bot else "no"})
traders.sort(key=lambda r: r["pnlUsd"], reverse=True)
n = 0
for r in traders:
    if r["botLike"] == "no":
        n += 1; r["rank"] = n
    else:
        r["rank"] = None
top = traders[:300]
keep = {r["wallet"] for r in top}
positions = sorted([p for p in positions if p["wallet"] in keep], key=lambda p: p["pnlUsd"], reverse=True)

times = [t["time"] for t in trades if t["time"]]
json.dump([{"snapshotAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "windowStart": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(min(times))),
            "windowEnd": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(max(times))),
            "pairsTracked": len(pair_rows), "swapsParsed": len(trades), "walletsSeen": len(traders),
            "botLikeWallets": sum(r["botLike"] == "yes" for r in traders)}], open(f"{OUT}/run.json", "w"))
json.dump(pair_rows, open(f"{OUT}/pairs.json", "w"))
json.dump(top, open(f"{OUT}/traders.json", "w"))
json.dump(positions, open(f"{OUT}/positions.json", "w"))
print(f"{len(trades)} swaps, {len(traders)} wallets, wrote top {len(top)}", file=sys.stderr)
