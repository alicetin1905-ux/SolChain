"""Paper copy-trading: follow the top wallets from the leaderboard and simulate copying their swaps.

No real trades are made. Each run reads the followed wallets' new transactions from Solana RPC,
turns their swaps into simulated copy trades, marks open positions to the current Dexscreener price,
and writes the result to data/paper.json for the site.

Settings (environment):
  SOL_RPC        Solana RPC URL (falls back to the free public endpoint)
  FOLLOW         number of wallets to follow: the best scored ones from data/scores.json,
                 or the leaderboard's top when there are no scores yet (default 10)
  EARLY_FOLLOW   number of repeat early buyers from data/early.json to follow too (default 0)
  DROP_AFTER     stop following a wallet once this many copies closed at a net loss (default 10)
  CHASE_PCT      skip a copy when the live price is this much above the wallet's own (default 30)
  TOP10_MAX_PCT  skip tokens whose 10 largest holders, pool left out, own more than this (default 40)
  NTFY_TOPIC     ntfy.sh topic for phone notifications (keep it secret: anyone with the name can read them)
  TRADE_SOL      SOL per copied buy (default 1)
  SLIPPAGE_PCT   price penalty per fill for arriving after the leader (default 3)
  FEE_SOL        network + priority fee per fill (default 0.001)
"""
import calendar, json, os, ssl, sys, time, urllib.error, urllib.request, collections
from concurrent.futures import ThreadPoolExecutor

RPC = os.environ.get("SOL_RPC") or "https://solana-rpc.publicnode.com"
DS = "https://api.dexscreener.com"
WSOL = "So11111111111111111111111111111111111111112"
STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY5NNmFfeQe36Ci"}
FOLLOW = int(os.environ.get("FOLLOW") or 10)
EARLY_FOLLOW = int(os.environ.get("EARLY_FOLLOW") or 0)  # early buyers snipe tokens of a few $1000s: too early to copy
TRADE_SOL = float(os.environ.get("TRADE_SOL") or 1)
SLIP = float(os.environ.get("SLIPPAGE_PCT") or 3) / 100
FEE_SOL = float(os.environ.get("FEE_SOL") or 0.001)
MIN_LIQ_USD = 5_000    # a DEX pool with less than this can't take a 1 SOL trade without moving the price a lot
MIN_MCAP_USD = 5_000   # the same floor for tokens still on their pump.fun bonding curve, which has no pool to measure
DROP_AFTER = int(os.environ.get("DROP_AFTER") or 10)
CHASE = float(os.environ.get("CHASE_PCT") or 30) / 100
TOP10_MAX = float(os.environ.get("TOP10_MAX_PCT") or 40) / 100

NTFY_TOPIC = os.environ.get("NTFY_TOPIC") or ""
NTFY_SERVER = os.environ.get("NTFY_SERVER") or "https://ntfy.sh"
SITE = os.environ.get("SITE_URL") or "https://alicetin1905-ux.github.io/SolChain/copy.html"
RECENT_SEC = 1800  # trades older than this (caught up after downtime) are summed up, not sent one by one
OUTBOX = []

# Every copied buy is followed in each of these exit strategies side by side, 1 position each.
STRATEGIES = {
    "follow": "Sell when the wallet sells",
    "stop30": "Also sell everything at -30%",
    "tp2x": "Sell half at 2x, then trail the rest 30% below its peak",
    "max24h": "Also sell everything after 24 hours",
}
DATA = sys.argv[1] if len(sys.argv) > 1 else "data"
STATE = f"{DATA}/paper_state.json"
try:  # macOS Pythons often can't find the system certificates; certifi ships its own
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()
POOL = ThreadPoolExecutor(int(os.environ.get("RPC_THREADS") or 2))


class Refused(RuntimeError):
    """The server refused the request itself (not a rate limit), so retrying won't help."""


def get(url, body=None, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"content-type": "application/json", "user-agent": "solchain-paper/1.0"})
            with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 404, 405, 413):
                raise Refused(f"HTTP {e.code} from {url.split('?')[0]}")
            print(f"  retry {i+1} after {e}", file=sys.stderr)
            time.sleep(2 ** i)
        except Exception as e:
            print(f"  retry {i+1} after {e}", file=sys.stderr)
            time.sleep(2 ** i)
    raise RuntimeError(f"failed: {url.split('?')[0]}")  # never log an API key in the query


def rpc(method, params):
    for attempt in range(6):
        try:
            r = get(RPC, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        except Refused as e:
            print(f"  {method} refused: {e}", file=sys.stderr)
            return None
        except RuntimeError:  # rate-limited or unreachable for a while: back off and try again
            time.sleep(5 + 2 ** attempt)
            continue
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
    """Current price (USD), liquidity, market cap and symbol of each mint's deepest Dexscreener pair.
    "curve" marks a token still on its pump.fun bonding curve (Dexscreener gives such a pair no liquidity)."""
    out, mints = {}, list(mints)
    for i in range(0, len(mints), 30):
        for p in get(f"{DS}/tokens/v1/solana/{','.join(mints[i:i+30])}"):
            m = p["baseToken"]["address"]
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if m not in out or liq > out[m]["liquidityUsd"]:
                out[m] = {"priceUsd": float(p.get("priceUsd") or 0), "liquidityUsd": liq,
                          "marketCapUsd": float(p.get("marketCap") or p.get("fdv") or 0),
                          "curve": p.get("dexId") == "pumpfun" and not liq,
                          "symbol": p["baseToken"]["symbol"], "url": p.get("url")}
    return out


def tradable(ti):
    """Whether a 1 SOL copy can be bought and sold in this token without moving its price much."""
    if ti.get("curve"):
        return ti.get("marketCapUsd", 0) >= MIN_MCAP_USD
    return ti.get("liquidityUsd", 0) >= MIN_LIQ_USD


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


def short(w):
    return f"{w[:4]}…{w[-4:]}"


def notify(title, message, tags=(), priority=3):
    if NTFY_TOPIC:
        OUTBOX.append({"title": title, "message": message, "tags": list(tags), "priority": priority})


def flush_notifications():
    """Send queued phone notifications through ntfy; a burst is cut to the first few and a count."""
    msgs = OUTBOX[:]
    OUTBOX.clear()
    if not msgs or not NTFY_TOPIC:
        return
    if len(msgs) > 6:
        msgs = msgs[:5] + [{"title": f"{len(msgs) - 5} more SolChain updates", "message": "Open the app to see them all.", "tags": ["bell"], "priority": 2}]
    for m in msgs:
        body = json.dumps({"topic": NTFY_TOPIC, "click": SITE, **m}).encode()
        try:
            req = urllib.request.Request(NTFY_SERVER, data=body, headers={"content-type": "application/json", "user-agent": "solchain/1.0"})
            urllib.request.urlopen(req, timeout=10, context=SSL_CTX).close()
        except Exception as e:
            print(f"  notification not sent: {e}", file=sys.stderr)
            return


def load_state():
    try:
        st = json.load(open(STATE))
    except FileNotFoundError:
        st = {"startedAt": now_iso(), "wallets": {}, "positions": [], "trades": [], "equity": []}
    skips = st.setdefault("skips", {})
    if "skipped" in st:  # older state kept one counter for low liquidity
        skips["lowLiquidity"] = skips.get("lowLiquidity", 0) + st.pop("skipped")
    for p in st["positions"] + st.get("closed", []):
        p.setdefault("strategy", "follow")
    return st


def early_buyers():
    """The best repeat early buyers from data/early.json (scripts/early.py), snipers left out."""
    try:
        early = json.load(open(f"{DATA}/early.json"))
    except FileNotFoundError:
        return []
    return [w["wallet"] for w in early.get("wallets", []) if not w["sniper"] and w["tokens"] >= 2]


def load_scores():
    try:
        return json.load(open(f"{DATA}/scores.json"))
    except FileNotFoundError:
        return {}


def scored_wallets():
    """Wallets picked by scripts/score.py from their 30-day history and backtest, best first."""
    return load_scores().get("selected", [])


def failed_scoring():
    """Wallets that scripts/score.py checked and found unfit to copy (bots, too fast, losing backtest)."""
    return {r["wallet"] for r in load_scores().get("wallets", []) if r["flags"]}


def losing_wallets(st):
    """Wallets whose copies (follow strategy) closed at a net loss after at least DROP_AFTER of them."""
    res = collections.defaultdict(list)
    for p in st.get("closed", []):
        if p["strategy"] == "follow":
            res[p["wallet"]].append(p["pnlSol"])
    return {w for w, pnl in res.items() if len(pnl) >= DROP_AFTER and sum(pnl) < 0}


def follow_list(st):
    """Scored wallets (or the leaderboard's top) plus the best early buyers, minus wallets whose copies lose.
    Marks who is followed and returns (followed, wallets to watch)."""
    dropped = losing_wallets(st)
    main = [w for w in scored_wallets() if w not in dropped][:FOLLOW]
    source = "scored"
    if not main:
        traders = json.load(open(f"{DATA}/traders.json"))
        board = [r["wallet"] for r in sorted((r for r in traders if r["botLike"] == "no"), key=lambda r: -r["pnlUsd"])]
        main, source = [w for w in board if w not in dropped][:FOLLOW], "leaderboard"
    unfit = failed_scoring()
    early = [w for w in early_buyers() if w not in main and w not in dropped and w not in unfit][:EARLY_FOLLOW]
    top = main + early
    for w in top:
        st["wallets"].setdefault(w, {"since": now_iso(), "lastSig": None})
    for w, info in st["wallets"].items():
        info["following"] = w in top
        info["dropped"] = w in dropped
        if w in top:
            info["source"] = "early" if w in early else source
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


def token_risk(st, mint):
    """Why a token is too risky to buy (mint or freeze authority kept, or held by a few wallets), or None.
    Results are cached per token; a check the RPC can't answer lets the buy through."""
    cache = st.setdefault("tokenChecks", {})
    if mint in cache:
        return cache[mint]["reason"]
    reason = None
    acc = rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}]) or {}
    info = (((acc.get("value") or {}).get("data") or {}).get("parsed") or {}).get("info") if isinstance(acc.get("value"), dict) else None
    if info:
        if info.get("mintAuthority"):
            reason = "mintAuthority"
        elif info.get("freezeAuthority"):
            reason = "freezeAuthority"
        else:
            largest = (rpc("getTokenLargestAccounts", [mint]) or {}).get("value") or []
            supply = float(info.get("supply") or 0) / 10 ** int(info.get("decimals") or 0)
            amounts = sorted((float(a.get("uiAmount") or 0) for a in largest), reverse=True)
            # the largest account is nearly always the pool or bonding curve, so it is left out
            if supply and sum(amounts[1:11]) / supply > TOP10_MAX:
                reason = "concentrated"
    cache[mint] = {"reason": reason, "at": now_iso()}
    if len(cache) > 3000:
        for k in sorted(cache, key=lambda k: cache[k]["at"])[:500]:
            del cache[k]
    return reason


def sell(st, p, q, px, when, src, reason, frac=None, lag=None, sig=None, leader_px=None):
    """Sell q tokens of a copy at px SOL each. Returns True when the position is now closed."""
    got = q * px - FEE_SOL
    p["qtyLeft"] -= q
    p["realizedSol"] += got
    st["trades"].append({"time": now_iso(when), "wallet": p["wallet"], "symbol": p["symbol"], "mint": p["mint"],
                         "strategy": p["strategy"], "side": "sell", "reason": reason, "sol": round(got, 4), "price": px,
                         "fraction": round(frac, 3) if frac is not None else None, "fill": src, "lagSec": lag, "leaderSig": sig,
                         "leaderPrice": leader_px})
    recent = time.time() - when < RECENT_SEC
    if p["qtyLeft"] <= p["qty"] * 0.001:
        p["closedAt"] = now_iso(when)
        p["closeReason"] = reason
        p["pnlSol"] = p["realizedSol"] - p["entrySol"]
        st.setdefault("closed", []).append(p)
        if recent and p["strategy"] == "follow":
            pnl = p["pnlSol"]
            notify(f"Closed {p['symbol']} {pnl:+.3f} SOL ({100 * pnl / p['entrySol']:+.0f}%)",
                   f"The wallet {short(p['wallet'])} sold, so the copy did too.",
                   ["chart_with_upwards_trend" if pnl > 0 else "chart_with_downwards_trend"])
        return True
    if recent and reason == "take-profit":
        notify(f"{p['symbol']} doubled", f"The \"2x then trail\" strategy sold half for {got:.3f} SOL.", ["moneybag"])
    return False


def apply_events(st, events, info, fill=model_fill):
    """Turn the leaders' swaps into simulated copy trades, one position per strategy.
    `fill(side, event, qty)` returns (price, source)."""
    skips = st.setdefault("skips", {})
    open_pos = {(p["wallet"], p["mint"], p["strategy"]): p for p in st["positions"]}
    for ev in events:
        w, mint = ev["wallet"], ev["mint"]
        ti = info.get(mint, {})
        lag = round(time.time() - ev["time"], 1) if ev.get("live") else None
        mine = [p for (pw, pm, _), p in open_pos.items() if pw == w and pm == mint]
        if ev["dtok"] > 0:  # leader bought
            if mine or not st["wallets"][w].get("following"):
                continue
            skip = None
            if not tradable(ti):
                skip = "lowLiquidity"
            else:
                skip = token_risk(st, mint)
            px = src = None
            leader_px = ev["sol"] / ev["dtok"]
            if not skip:
                px, src = fill("buy", ev, None)
                if not px:
                    continue
                if src == "jupiter" and px > leader_px * (1 + CHASE):
                    skip = "chase"  # the price already ran away from the wallet's own entry
            if skip:
                seen = st.setdefault("lastSkip", {})
                k = f"{w}:{mint}"
                if ev["time"] - seen.get(k, 0) < 3600:
                    continue  # the same wallet buying the same token again: logged and counted once an hour
                seen[k] = ev["time"]
                if len(seen) > 5000:
                    for old in sorted(seen, key=seen.get)[:1000]:
                        del seen[old]
                skips[skip] = skips.get(skip, 0) + 1
                st["trades"].append({"time": now_iso(ev["time"]), "wallet": w, "symbol": ti.get("symbol") or mint[:4] + "…",
                                     "mint": mint, "side": "skip", "reason": skip, "sol": 0, "lagSec": lag, "leaderSig": ev["sig"],
                                     "price": px, "fill": src, "leaderPrice": leader_px})
                continue
            qty = (TRADE_SOL - FEE_SOL) / px
            sym = ti.get("symbol") or mint[:4] + "…"
            if time.time() - ev["time"] < RECENT_SEC:
                src_name = {"scored": "scored wallet", "early": "early buyer"}.get(st["wallets"][w].get("source"), "wallet")
                notify(f"Copied a buy: {sym}", f"{TRADE_SOL:g} SOL per strategy, copying {src_name} {short(w)}"
                       + (f", {lag:.0f} s after it." if lag is not None else "."), ["green_circle"])
            else:
                st["caughtUp"] = st.get("caughtUp", 0) + 1
            for strat in STRATEGIES:
                open_pos[(w, mint, strat)] = {"wallet": w, "mint": mint, "symbol": sym, "strategy": strat,
                                              "openedAt": now_iso(ev["time"]), "entrySol": TRADE_SOL, "qty": qty, "qtyLeft": qty,
                                              "entryPrice": px, "peak": px, "realizedSol": 0.0, "leaderSig": ev["sig"],
                                              "decimals": ev.get("decimals")}
                st["trades"].append({"time": now_iso(ev["time"]), "wallet": w, "symbol": sym, "mint": mint, "strategy": strat,
                                     "side": "buy", "sol": round(TRADE_SOL, 4), "price": px, "fill": src, "lagSec": lag,
                                     "leaderSig": ev["sig"], "leaderPrice": leader_px})
        elif mine:  # leader sold: every strategy still holding this copy sells the same share
            frac = min(1.0, abs(ev["dtok"]) / ev["pre"]) if ev["pre"] > 0 else 1.0
            qs = {p["strategy"]: (p["qtyLeft"] if frac > 0.98 else p["qtyLeft"] * frac) for p in mine}
            if frac <= 0.98 and max(qs.values()) * ev["sol"] / abs(ev["dtok"]) < 0.02:
                continue  # a tiny partial sell: fees would eat it, so the copies wait for a bigger one
            px, src = fill("sell", ev, max(qs.values()))
            if px is None:
                continue
            for p in mine:
                if sell(st, p, qs[p["strategy"]], px, ev["time"], src, "wallet sold", frac, lag, ev["sig"], ev["sol"] / abs(ev["dtok"])):
                    del open_pos[(w, mint, p["strategy"])]
    st["positions"] = list(open_pos.values())
    if st.get("caughtUp"):
        notify(f"Caught up {st['caughtUp']} copied buy{'s' if st['caughtUp'] != 1 else ''}",
               "These happened while the engine was offline and were priced with the slippage estimate.", ["hourglass"], 2)
        st["caughtUp"] = 0
    flush_notifications()


def apply_exit_rules(st, cur_price):
    """Price- and time-based exits of the strategies other than 'follow', at the current price."""
    now = time.time()
    still = []
    for p in st["positions"]:
        cur = cur_price.get(p["mint"]) or 0
        closed = False
        if cur > 0:
            p["peak"] = max(p.get("peak") or p["entryPrice"], cur)
            px = cur * (1 - SLIP)
            if p["strategy"] == "stop30" and cur <= p["entryPrice"] * 0.7:
                closed = sell(st, p, p["qtyLeft"], px, now, "model", "stop-loss")
            elif p["strategy"] == "tp2x":
                if not p.get("tpDone") and cur >= p["entryPrice"] * 2:
                    p["tpDone"] = True
                    closed = sell(st, p, p["qtyLeft"] * 0.5, px, now, "model", "take-profit")
                elif p.get("tpDone") and cur <= p["peak"] * 0.7:
                    closed = sell(st, p, p["qtyLeft"], px, now, "model", "trailing stop")
            elif p["strategy"] == "max24h" and now - epoch(p["openedAt"]) >= 86400:
                closed = sell(st, p, p["qtyLeft"], px, now, "model", "time limit")
        if not closed:
            still.append(p)
    st["positions"] = still


def strategy_totals(positions, closed):
    out = {}
    for name, label in STRATEGIES.items():
        op = [p for p in positions if p["strategy"] == name]
        cl = [p for p in closed if p["strategy"] == name]
        realized, unreal = sum(p["pnlSol"] for p in cl), sum(p["pnlSol"] for p in op)
        out[name] = {"label": label, "realizedSol": round(realized, 4), "openPnlSol": round(unreal, 4),
                     "totalPnlSol": round(realized + unreal, 4), "openPositions": len(op), "closedPositions": len(cl),
                     "winRatePct": round(100 * sum(p["pnlSol"] > 0 for p in cl) / len(cl), 1) if cl else None,
                     "capitalInUseSol": round(sum(p["entrySol"] for p in op), 2)}
    return out


def publish(st, sp, top, engine):
    """Apply the exit rules, mark open positions to the current price, and write the state and data/paper.json."""
    info = token_info({p["mint"] for p in st["positions"]}) if st["positions"] else {}
    cur_price = {m: (i.get("priceUsd") or 0) / sp for m, i in info.items()}
    apply_exit_rules(st, cur_price)
    for p in st["positions"]:
        cur = cur_price.get(p["mint"]) or 0
        p["priceNow"] = cur
        p["valueSol"] = p["qtyLeft"] * cur
        p["pnlSol"] = p["realizedSol"] + p["valueSol"] - p["entrySol"]
    positions = st["positions"]
    closed = st.get("closed", [])
    strats = strategy_totals(positions, closed)
    f = strats["follow"]
    point = {"t": now_iso(), "realizedSol": f["realizedSol"], "openPnlSol": f["openPnlSol"], "open": f["openPositions"],
             "byStrategy": {k: v["totalPnlSol"] for k, v in strats.items()}}
    # live runs publish often; keep at most one equity point per 10 minutes
    if st["equity"] and time.time() - epoch(st["equity"][-1]["t"]) < 600:
        st["equity"][-1] = point
    else:
        st["equity"].append(point)
    st["equity"] = st["equity"][-6 * 24 * 60:]
    st["trades"] = st["trades"][-4000:]
    st["closed"] = closed[-4000:]
    st["solUsd"] = sp
    st["engine"] = engine
    st["updatedAt"] = now_iso()
    json.dump(st, open(STATE, "w"))

    by_wallet = collections.defaultdict(lambda: {"pnlSol": 0.0, "positions": 0, "wins": 0, "closed": 0})
    for p in closed + positions:
        if p["strategy"] != "follow":
            continue
        b = by_wallet[p["wallet"]]
        b["pnlSol"] += p["pnlSol"]; b["positions"] += 1
        if "closedAt" in p:
            b["closed"] += 1; b["wins"] += p["pnlSol"] > 0
    wallets = [{"wallet": w, "following": i.get("following", False), "dropped": i.get("dropped", False), "since": i["since"],
                "source": i.get("source", "leaderboard"), "pnlSol": round(by_wallet[w]["pnlSol"], 4),
                "positions": by_wallet[w]["positions"], "closed": by_wallet[w]["closed"], "wins": by_wallet[w]["wins"]}
               for w, i in st["wallets"].items() if i.get("following") or w in by_wallet]
    lags = [t["lagSec"] for t in st["trades"] if t.get("lagSec") is not None and t["side"] == "buy"]
    sources = collections.Counter(st["wallets"][w].get("source") for w in top)
    summary = {"startedAt": st["startedAt"], "updatedAt": st["updatedAt"], "engine": engine, "solUsd": sp,
               "tradeSol": TRADE_SOL, "slippagePct": SLIP * 100, "feeSol": FEE_SOL, "chasePct": CHASE * 100,
               "top10MaxPct": TOP10_MAX * 100, "dropAfter": DROP_AFTER, "following": len(top),
               "followingEarly": sources.get("early", 0), "followingScored": sources.get("scored", 0),
               "realizedSol": f["realizedSol"], "openPnlSol": f["openPnlSol"], "totalPnlSol": f["totalPnlSol"],
               "openPositions": f["openPositions"], "closedPositions": f["closedPositions"], "winRatePct": f["winRatePct"],
               "capitalInUseSol": f["capitalInUseSol"], "skips": st.get("skips", {}),
               "skippedLowLiquidity": st.get("skips", {}).get("lowLiquidity", 0),
               "medianLagSec": sorted(lags)[len(lags) // 2] if lags else None, "strategies": strats}
    keep = ("wallet", "mint", "symbol", "strategy", "openedAt", "entrySol", "entryPrice", "realizedSol", "pnlSol", "qtyLeft")
    pub = lambda p: {k: p[k] for k in keep if k in p} | (
        {"closedAt": p["closedAt"], "closeReason": p.get("closeReason")} if "closedAt" in p
        else {"priceNow": p.get("priceNow"), "valueSol": round(p.get("valueSol", 0), 4)})
    json.dump({"summary": summary, "strategies": STRATEGIES, "wallets": wallets, "positions": [pub(p) for p in positions],
               "closed": [pub(p) for p in closed[-800:]][::-1], "trades": st["trades"][-800:][::-1],
               "equity": st["equity"]}, open(f"{DATA}/paper.json", "w"))
    print(" · ".join(f"{k} {v['totalPnlSol']:+.3f}" for k, v in strats.items()) + f" SOL; {len(positions)} open positions", file=sys.stderr)
    today = time.strftime("%Y-%m-%d")
    if NTFY_TOPIC and time.localtime().tm_hour >= 21 and st.get("lastSummary") != today:
        st["lastSummary"] = today
        json.dump(st, open(STATE, "w"))
        names = {"follow": "Follow", "stop30": "-30% stop", "tp2x": "2x then trail", "max24h": "24 hours"}
        lines = [f"{names.get(k, k)}: {v['totalPnlSol']:+.3f} SOL ({v['closedPositions']} closed, {v['openPositions']} open)" for k, v in strats.items()]
        best = max(strats, key=lambda k: strats[k]["totalPnlSol"])
        notify(f"SolChain today: best is {names.get(best, best)}", "\n".join(lines), ["bar_chart"], 2)
    flush_notifications()


def live_engine_active(st):
    """True while the Mac engine (scripts/live.py) has published in the last 30 minutes."""
    if st.get("engine") != "live" or not st.get("updatedAt"):
        return False
    return time.time() - epoch(st["updatedAt"]) < 1800


def main():
    global RECENT_SEC
    RECENT_SEC = 5400  # the hourly check sees trades up to an hour old; those still count as new
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
