"""Wallet scores: judge candidate wallets on their recent history instead of one snapshot.

Candidates are the watchlist, the best Kolscan traders, the leaderboard's best non-bot wallets, the repeat early buyers and the wallets the
copy-trade engine already follows. For each, this reads up to WALLET_TXS of its swaps from the last
DAYS days, works out its own results per token, and backtests copying it: buying 1 SOL of each token
at its first buy price plus slippage, selling the same share whenever it sold at its price minus
slippage, and valuing what is left at today's price. The best wallets by backtest are written to
data/scores.json as "selected"; the copy-trade engine follows those.

Settings (environment):
  SOL_RPC        archival Solana RPC (Helius works)
  CANDIDATES     wallets to score per run (default 30)
  WALLET_TXS     transactions to read per wallet (default 250)
  DAYS           history window in days (default 30)
  SCORE_ONLY     space-separated wallets to score now; their rows are merged into the existing scores
  WALLET_TXS_DEEP transactions read again for wallets that pass the first read (default 1000)
  KOL_MAX        quiet traders from the kolscan.io leaderboards to add as candidates (default 50)
"""
import collections, json, os, re, sys, time, urllib.request
import paper as P

CANDIDATES = int(os.environ.get("CANDIDATES") or 30)
WALLET_TXS = int(os.environ.get("WALLET_TXS") or 250)
DAYS = int(os.environ.get("DAYS") or 30)
KOL_MAX = int(os.environ.get("KOL_MAX") or 50)  # Kolscan traders to score per run
KOL_MIN_TRADES = 10     # Kolscan wins + losses needed before a trader is worth scoring
KOL_MIN_WIN = 0.4       # and the share of them that won
KOL_MAX_TRADES_PER_DAY = 25  # busier traders are crowded by copy bots
MIN_TOKENS = 5          # fewer tokens than this is too small a sample to judge
MIN_WIN_RATE = 30       # % of copied tokens that must have made money
BOT_TOKENS_PER_DAY = 50  # trading more different tokens a day than this looks like a bot (splitting one trade into many swaps is fine)
MIN_HOLD_SEC = 60       # a median hold shorter than this can't be copied in time
BUSY_TXS_PER_DAY = 2000 # successful transactions naming a wallet a day beyond which it is not read at all
SIG_PAGES = 5           # signature pages (1000 each) to look through per wallet
WALLET_TXS_DEEP = int(os.environ.get("WALLET_TXS_DEEP") or 1000)  # second, longer read of the wallets that pass
DEEP_SIG_PAGES = 12
LIVE_MIN_COPIES = 5     # live copies needed before their result can fail a wallet
GAP_MIN = 3             # measured fills needed before a wallet's own price gap replaces the average
GAP_FETCH_MAX = 400     # wallet transactions fetched per run to learn the price of older copies


def watchlist():
    """Wallets added by hand (data/watchlist.json, via the "Add wallets" form), always scored."""
    try:
        return json.load(open(f"{P.DATA}/watchlist.json"))
    except FileNotFoundError:
        return []


def kolscan():
    """Quiet, profitable traders from the kolscan.io day, week and month leaderboards, most profit per trade first.
    Busy famous traders are left out: copy bots crowd them, so a copy always buys after the crowd.
    The page embeds its leaderboard data; one request a day."""
    try:
        req = urllib.request.Request("https://kolscan.io/leaderboard", headers={"user-agent": "Mozilla/5.0 (solchain wallet scoring)"})
        with urllib.request.urlopen(req, timeout=20, context=P.SSL_CTX) as r:
            page = r.read().decode("utf-8", "replace").replace('\\"', '"')
    except Exception as e:
        print(f"  kolscan unavailable: {e}", file=sys.stderr)
        return {}
    rows = re.findall(r'"wallet_address":"([1-9A-HJ-NP-Za-km-z]{32,44})","name":"([^"]*)",[^{}]*?"profit":(-?[\d.e+-]+),"wins":(\d+),"losses":(\d+),"timeframe":(\d+)', page)
    best = {}
    for wallet, name, profit, wins, losses, tf in rows:
        wins, losses, tf, profit = int(wins), int(losses), int(tf), float(profit)
        trades = wins + losses
        if trades < KOL_MIN_TRADES or wins / trades < KOL_MIN_WIN or profit <= 0 or trades / tf > KOL_MAX_TRADES_PER_DAY:
            continue
        note = f"Kolscan {name}: {profit:+.0f} SOL in {tf}d, {wins}W/{losses}L"
        if wallet not in best or tf > best[wallet]["tf"]:
            best[wallet] = {"tf": tf, "perTrade": profit / trades, "note": note}
    return dict(sorted(best.items(), key=lambda kv: -kv[1]["perTrade"])[:KOL_MAX])


def candidates():
    out = [w["wallet"] for w in watchlist()] + list(KOLS)
    try:
        traders = json.load(open(f"{P.DATA}/traders.json"))
        out += [r["wallet"] for r in sorted((r for r in traders if r["botLike"] == "no"), key=lambda r: -r["pnlUsd"])[:CANDIDATES]]
    except FileNotFoundError:
        pass
    out += P.early_buyers()[:CANDIDATES // 2]
    try:
        st = json.load(open(P.STATE))
        out += [w for w, i in st["wallets"].items() if i.get("following")]
    except FileNotFoundError:
        pass
    try:  # keep scoring last run's picks, so a good wallet doesn't drop out just because it left the leaderboard
        out += json.load(open(f"{P.DATA}/scores.json")).get("selected", [])
    except FileNotFoundError:
        pass
    return list(dict.fromkeys(out))


def history(wallet, sp, txs=WALLET_TXS, pages=SIG_PAGES):
    """The wallet's swaps in the window (oldest first), the transactions it signs a day, and the days the swaps cover.
    Other wallets' transactions can name it too (famous traders are swarmed by copy bots), so activity is measured
    on the transactions it signed. Swaps are None for a wallet too busy to read: its transactions are then not fetched."""
    since = time.time() - DAYS * 86400
    sigs, before, oldest = [], None, time.time()
    for _ in range(pages):
        page = P.rpc("getSignaturesForAddress", [wallet, {"limit": 1000, **({"before": before} if before else {})}]) or []
        if not page:
            break
        oldest = min(oldest, page[-1].get("blockTime") or oldest)
        sigs += [s for s in page if (s.get("blockTime") or 0) >= since and s.get("err") is None]
        if len(sigs) >= txs * 4 or len(page) < 1000 or oldest < since:
            break
        before = page[-1]["signature"]
    span = lambda xs: max(1 / 24, (time.time() - min((x.get("blockTime") or time.time()) for x in xs)) / 86400) if xs else 0
    busy = len(sigs) / max(1 / 24, (time.time() - max(oldest, since)) / 86400)
    if busy > BUSY_TXS_PER_DAY:
        return None, busy, span(sigs)  # a bot or a crowd of copy bots: too costly to read, and too crowded to copy
    ok = sigs[:txs]
    swaps, signed = [], 0
    for s, tx in zip(ok, P.POOL.map(lambda s: P.fetch_tx(s["signature"]), ok)):
        if not tx:
            continue
        signed += any(isinstance(k, dict) and k.get("signer") and k.get("pubkey") == wallet for k in tx["transaction"]["message"]["accountKeys"])
        sw = P.parse_swap(tx, wallet, sp)
        if sw:
            swaps.append({**sw, "time": tx.get("blockTime") or s.get("blockTime") or 0})
    return sorted(swaps, key=lambda x: x["time"]), signed / span(ok) if ok else 0, span(ok)


def fill_gaps(sp):
    """How much worse than the wallet itself the live engine's Jupiter quotes were, per wallet: the median of
    (our price / the wallet's price - 1) on buys and on sells. Copies logged before the engine kept the wallet's
    price get it from the wallet's own transaction. Results are kept in data/fill_gaps.json; "*" is everyone."""
    path = f"{P.DATA}/fill_gaps.json"
    try:
        cache = json.load(open(path))
    except FileNotFoundError:
        cache = {}
    try:
        trades = json.load(open(f"{P.DATA}/paper.json")).get("trades", [])
    except FileNotFoundError:
        trades = []
    fetched = 0
    for t in trades:
        side = {"buy": "buy", "skip": "buy", "sell": "sell"}.get(t.get("side"))
        if not side or t.get("fill") != "jupiter" or not t.get("price") or not t.get("leaderSig"):
            continue
        if t["side"] != "skip" and t.get("strategy") != "follow" or t["side"] == "sell" and t.get("reason") != "wallet sold":
            continue  # one record per copy, and only sells that followed the wallet
        key = f"{t['leaderSig']}:{side}"
        if key in cache:
            continue
        lp = t.get("leaderPrice")
        if not lp:
            if fetched >= GAP_FETCH_MAX:
                continue
            fetched += 1
            tx = P.fetch_tx(t["leaderSig"])
            sw = tx and P.parse_swap(tx, t["wallet"], sp)
            lp = sw["sol"] / abs(sw["dtok"]) if sw and sw["mint"] == t.get("mint") else None
        cache[key] = {"wallet": t["wallet"], "side": side, "gap": t["price"] / lp - 1 if lp else None, "time": t["time"]}
    json.dump(cache, open(path, "w"))
    med = lambda xs: sorted(xs)[len(xs) // 2] if xs else None
    per = collections.defaultdict(lambda: {"buy": [], "sell": []})
    for v in cache.values():
        if v.get("gap") is not None:
            per[v["wallet"]][v["side"]].append(v["gap"])
            per["*"][v["side"]].append(v["gap"])
    print(f"  measured fills: {len(per['*']['buy'])} buys, {len(per['*']['sell'])} sells ({fetched} wallet transactions read)", file=sys.stderr)
    return {w: {"entry": med(g["buy"]), "exit": med(g["sell"]), "n": len(g["buy"]), "nExit": len(g["sell"])} for w, g in per.items()}


def fill_cost(gaps, wallet):
    """(buy multiplier, sell multiplier, where they came from) for a copy of this wallet."""
    own, avg = gaps.get(wallet) or {}, gaps.get("*") or {}
    pick = lambda key, n_key: (own[key], "measured") if own.get(n_key, 0) >= GAP_MIN else (avg[key], "average") if avg.get(n_key, 0) >= 10 else (None, "estimate")
    entry, e_src = pick("entry", "n")
    exit_, x_src = pick("exit", "nExit")
    buy = 1 + min(2.0, max(0.0, entry)) if entry is not None else 1 + P.SLIP
    sell = 1 + min(0.1, max(-0.9, exit_)) if exit_ is not None else 1 - P.SLIP
    return buy, sell, e_src if e_src == x_src else f"{e_src} buys, {x_src} sells"


def live_results():
    """Per wallet: (number of closed live copies, their total PnL in SOL), "sell when the wallet sells" strategy."""
    res = collections.defaultdict(lambda: [0, 0.0])
    try:
        for p in json.load(open(f"{P.DATA}/paper.json")).get("closed", []):
            if p.get("strategy") == "follow" and p.get("pnlSol") is not None:
                res[p["wallet"]][0] += 1
                res[p["wallet"]][1] += p["pnlSol"]
    except FileNotFoundError:
        pass
    return res


def evaluate(wallet, swaps, tx_per_day, span_days, prices, supply=None, min_mcap_sol=0, cost=(1 + P.SLIP, 1 - P.SLIP, "estimate")):
    per = collections.OrderedDict()
    for s in swaps:
        t = per.setdefault(s["mint"], {"buySol": 0.0, "buyTok": 0.0, "sellSol": 0.0, "sellTok": 0.0, "first": None, "firstSell": None})
        if s["dtok"] > 0:
            t["buySol"] += s["sol"]; t["buyTok"] += s["dtok"]
            t["first"] = t["first"] or s["time"]
        elif t["buyTok"] > 0:  # sells of tokens bought before the window are left out
            t["sellSol"] += s["sol"]; t["sellTok"] += -s["dtok"]
            t["firstSell"] = t["firstSell"] or s["time"]
    own_pnl = copy_pnl = 0.0
    tokens = wins = copy_wins = too_small = 0
    holds = []
    for mint, t in per.items():
        if t["buyTok"] <= 0:
            continue
        cur = prices.get(mint, 0.0)
        left = max(0.0, t["buyTok"] - t["sellTok"])
        pnl = t["sellSol"] + left * cur - t["buySol"]
        own_pnl += pnl
        b = t["buySol"] / t["buyTok"]
        if (supply or {}).get(mint) and b * supply[mint] < min_mcap_sol:
            too_small += 1  # bought below the engine's market cap floor: the engine wouldn't have copied it
            continue
        tokens += 1
        wins += pnl > 0
        f = min(1.0, t["sellTok"] / t["buyTok"])
        s_px = t["sellSol"] / t["sellTok"] if t["sellTok"] else 0
        ret = (f * s_px + (1 - f) * cur) * cost[1] / (b * cost[0])
        cp = P.TRADE_SOL * (ret - 1) - 2 * P.FEE_SOL
        copy_pnl += cp
        copy_wins += cp > 0
        if t["firstSell"]:
            holds.append(t["firstSell"] - t["first"])
    holds.sort()
    median_hold = holds[len(holds) // 2] if holds else None
    flags = []
    if span_days and (tokens + too_small) / span_days > BOT_TOKENS_PER_DAY:
        flags.append("bot-like activity")
    if median_hold is not None and median_hold < MIN_HOLD_SEC:
        flags.append("flips too fast to copy")
    if tokens < MIN_TOKENS:
        flags.append("too few tokens")
    copy_win_rate = round(100 * copy_wins / tokens, 1) if tokens else None
    if copy_win_rate is not None and copy_win_rate < MIN_WIN_RATE:
        flags.append("low win rate")
    if copy_pnl <= 0:
        flags.append("copying lost money")
    return {"wallet": wallet, "tokens": tokens, "swaps": len(swaps), "txPerDay": round(tx_per_day, 1),
            "daysCovered": round(span_days, 1), "ownPnlSol": round(own_pnl, 3), "ownWinRatePct": round(100 * wins / tokens, 1) if tokens else None,
            "copyPnlSol": round(copy_pnl, 3), "copyPerTokenSol": round(copy_pnl / tokens, 4) if tokens else None,
            "copyWinRatePct": copy_win_rate, "medianHoldSec": median_hold, "tooSmallTokens": too_small,
            "buyCostPct": round(100 * (cost[0] - 1), 1), "sellCostPct": round(100 * (1 - cost[1]), 1), "costSource": cost[2], "flags": flags}


KOLS = {}


def main():
    global KOLS
    sp = P.sol_usd()
    only = [w for w in (os.environ.get("SCORE_ONLY") or "").split() if re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", w)]
    KOLS = kolscan()  # also labels Kolscan traders scored with SCORE_ONLY
    print(f"{len(KOLS)} quiet Kolscan traders with {KOL_MIN_TRADES}+ trades and {KOL_MIN_WIN:.0%}+ wins", file=sys.stderr)
    cands = only or candidates()
    print(f"scoring {len(cands)} wallets over {DAYS} days, up to {WALLET_TXS} transactions each", file=sys.stderr)
    hist = {}
    for i, w in enumerate(cands, 1):
        swaps, rate, span = history(w, sp)
        hist[w] = (swaps, rate, span)
        print(f"  {i}/{len(cands)} {w[:6]}: " + (f"{len(swaps)} swaps over {span:.1f} days" if swaps is not None else "skipped as a bot")
              + f", {rate:.0f} transactions a day", file=sys.stderr)
    mints = {s["mint"] for swaps, _, _ in hist.values() for s in swaps or []}
    info = P.token_info(mints) if mints else {}
    # tokens with no pair, or too little liquidity to sell into, are worth 0: an illiquid pool's price can't be cashed out
    prices = {m: (i.get("priceUsd") or 0) / sp for m, i in info.items() if P.tradable(i)}
    # total supply from market cap and price, to tell each buy's market cap; a token without a pair now keeps counting
    supply = {m: i["marketCapUsd"] / i["priceUsd"] for m, i in info.items() if i.get("priceUsd") and i.get("marketCapUsd")}
    try:
        traders = {r["wallet"] for r in json.load(open(f"{P.DATA}/traders.json"))}
    except FileNotFoundError:
        traders = set()
    early = set(P.early_buyers())
    watched = {w["wallet"]: w for w in watchlist()}
    gaps, live = fill_gaps(sp), live_results()

    def judge(w, swaps, rate, span):
        r = evaluate(w, swaps or [], rate, span, prices, supply, P.MIN_MCAP_USD / sp, fill_cost(gaps, w))
        if swaps is None or (not r["tokens"] and span < 1):
            r["flags"] = ["too busy to follow"]  # skipped, or so crowded by other wallets' transactions that its own trades weren't reached
        n, pnl = live.get(w, (0, 0.0))
        r["liveCopies"], r["livePnlSol"] = n, round(pnl, 3)
        if n >= LIVE_MIN_COPIES and pnl < 0:
            r["flags"].append("losing live")
        elif n >= LIVE_MIN_COPIES and pnl > 0 and "too busy to follow" not in r["flags"]:
            r["flags"] = []  # its real copies made money: that outweighs what a short history suggests
            r["provenLive"] = True
        return r

    rows = {w: judge(w, *h) for w, h in hist.items()}
    # a short read can catch one lucky day: read the wallets that pass again, further back, before trusting them
    deep = [w for w, r in rows.items() if not r["flags"]]
    print(f"reading {len(deep)} passing wallets again, up to {WALLET_TXS_DEEP} transactions each", file=sys.stderr)
    for w in deep:
        swaps, rate, span = history(w, sp, WALLET_TXS_DEEP, DEEP_SIG_PAGES)
        new = {s["mint"] for s in swaps or []} - set(info)
        if new:
            more = P.token_info(new)
            info.update(more)
            prices.update({m: (i.get("priceUsd") or 0) / sp for m, i in more.items() if P.tradable(i)})
            supply.update({m: i["marketCapUsd"] / i["priceUsd"] for m, i in more.items() if i.get("priceUsd") and i.get("marketCapUsd")})
        rows[w] = judge(w, swaps, rate, span)
        rows[w]["deep"] = True
        print(f"  {w[:6]}: {rows[w]['tokens']} tokens over {span:.1f} days, copy backtest {rows[w]['copyPnlSol']:+.2f} SOL"
              + (f" ({', '.join(rows[w]['flags'])})" if rows[w]["flags"] else ", still passes"), file=sys.stderr)
    out = []
    for w, r in rows.items():
        r["source"] = ("watchlist" if w in watched else "kolscan" if w in KOLS else "early" if w in early
                       else "leaderboard" if w in traders else "followed")
        if w in watched and watched[w].get("note"):
            r["note"] = watched[w]["note"]
        elif w in KOLS:
            r["note"] = KOLS[w]["note"]
        r["scoredAt"] = P.now_iso()
        out.append(r)
    rows = out
    if only:  # a quick run for a few wallets keeps everyone else's last score
        prev = json.load(open(f"{P.DATA}/scores.json")).get("wallets", []) if os.path.exists(f"{P.DATA}/scores.json") else []
        rows += [r for r in prev if r["wallet"] not in hist]
    rows.sort(key=lambda r: (bool(r["flags"]), -(r.get("livePnlSol") if r.get("provenLive") else r["copyPnlSol"])))
    selected = [r["wallet"] for r in rows if not r["flags"]][:P.FOLLOW]
    json.dump({"updatedAt": P.now_iso(), "days": DAYS, "walletTxs": WALLET_TXS, "walletTxsDeep": WALLET_TXS_DEEP,
               "fillGap": gaps.get("*"), "slippagePct": P.SLIP * 100, "feeSol": P.FEE_SOL,
               "tradeSol": P.TRADE_SOL, "rules": {"minTokens": MIN_TOKENS, "minWinRatePct": MIN_WIN_RATE,
               "maxTokensPerDay": BOT_TOKENS_PER_DAY, "minHoldSec": MIN_HOLD_SEC, "liveMinCopies": LIVE_MIN_COPIES}, "selected": selected, "wallets": rows},
              open(f"{P.DATA}/scores.json", "w"))
    print(f"{len(rows)} wallets scored, {len(selected)} pass every rule", file=sys.stderr)


if __name__ == "__main__":
    main()
