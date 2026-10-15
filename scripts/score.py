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
  KOL_MAX        quiet traders from the kolscan.io leaderboards to add as candidates (default 25)
"""
import collections, json, os, re, sys, time, urllib.request
import paper as P

CANDIDATES = int(os.environ.get("CANDIDATES") or 30)
WALLET_TXS = int(os.environ.get("WALLET_TXS") or 250)
DAYS = int(os.environ.get("DAYS") or 30)
KOL_MAX = int(os.environ.get("KOL_MAX") or 25)  # Kolscan traders to score per run
KOL_MIN_TRADES = 10     # Kolscan wins + losses needed before a trader is worth scoring
KOL_MIN_WIN = 0.4       # and the share of them that won
KOL_MAX_TRADES_PER_DAY = 25  # busier traders are crowded by copy bots
MIN_TOKENS = 5          # fewer tokens than this is too small a sample to judge
MIN_WIN_RATE = 30       # % of copied tokens that must have made money
BOT_TXS_PER_DAY = 150   # signing more transactions a day than this looks like a bot
MIN_HOLD_SEC = 60       # a median hold shorter than this can't be copied in time
BUSY_TXS_PER_DAY = 2000 # successful transactions naming a wallet a day beyond which it is not read at all
SIG_PAGES = 5           # signature pages (1000 each) to look through per wallet


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


def history(wallet, sp):
    """The wallet's swaps in the window (oldest first), the transactions it signs a day, and the days the swaps cover.
    Other wallets' transactions can name it too (famous traders are swarmed by copy bots), so activity is measured
    on the transactions it signed. Swaps are None for a wallet too busy to read: its transactions are then not fetched."""
    since = time.time() - DAYS * 86400
    sigs, before, oldest = [], None, time.time()
    for _ in range(SIG_PAGES):
        page = P.rpc("getSignaturesForAddress", [wallet, {"limit": 1000, **({"before": before} if before else {})}]) or []
        if not page:
            break
        oldest = min(oldest, page[-1].get("blockTime") or oldest)
        sigs += [s for s in page if (s.get("blockTime") or 0) >= since and s.get("err") is None]
        if len(sigs) >= WALLET_TXS * 4 or len(page) < 1000 or oldest < since:
            break
        before = page[-1]["signature"]
    span = lambda xs: max(1 / 24, (time.time() - min((x.get("blockTime") or time.time()) for x in xs)) / 86400) if xs else 0
    busy = len(sigs) / max(1 / 24, (time.time() - max(oldest, since)) / 86400)
    if busy > BUSY_TXS_PER_DAY:
        return None, busy, span(sigs)  # a bot or a crowd of copy bots: too costly to read, and too crowded to copy
    ok = sigs[:WALLET_TXS]
    swaps, signed = [], 0
    for s, tx in zip(ok, P.POOL.map(lambda s: P.fetch_tx(s["signature"]), ok)):
        if not tx:
            continue
        signed += any(isinstance(k, dict) and k.get("signer") and k.get("pubkey") == wallet for k in tx["transaction"]["message"]["accountKeys"])
        sw = P.parse_swap(tx, wallet, sp)
        if sw:
            swaps.append({**sw, "time": tx.get("blockTime") or s.get("blockTime") or 0})
    return sorted(swaps, key=lambda x: x["time"]), signed / span(ok) if ok else 0, span(ok)


def evaluate(wallet, swaps, tx_per_day, span_days, prices, supply=None, min_mcap_sol=0):
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
        ret = (f * s_px * (1 - P.SLIP) + (1 - f) * cur * (1 - P.SLIP)) / (b * (1 + P.SLIP))
        cp = P.TRADE_SOL * (ret - 1) - 2 * P.FEE_SOL
        copy_pnl += cp
        copy_wins += cp > 0
        if t["firstSell"]:
            holds.append(t["firstSell"] - t["first"])
    holds.sort()
    median_hold = holds[len(holds) // 2] if holds else None
    flags = []
    if tx_per_day > BOT_TXS_PER_DAY:
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
            "copyWinRatePct": copy_win_rate, "medianHoldSec": median_hold, "tooSmallTokens": too_small, "flags": flags}


KOLS = {}


def main():
    global KOLS
    sp = P.sol_usd()
    only = (os.environ.get("SCORE_ONLY") or "").split()
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
    rows = []
    for w, (swaps, rate, span) in hist.items():
        r = evaluate(w, swaps or [], rate, span, prices, supply, P.MIN_MCAP_USD / sp)
        if swaps is None or (not r["tokens"] and span < 1):
            r["flags"] = ["too busy to follow"]  # skipped, or so crowded by other wallets' transactions that its own trades weren't reached
        r["source"] = ("watchlist" if w in watched else "kolscan" if w in KOLS else "early" if w in early
                       else "leaderboard" if w in traders else "followed")
        if w in watched and watched[w].get("note"):
            r["note"] = watched[w]["note"]
        elif w in KOLS:
            r["note"] = KOLS[w]["note"]
        r["scoredAt"] = P.now_iso()
        rows.append(r)
    if only:  # a quick run for a few wallets keeps everyone else's last score
        prev = json.load(open(f"{P.DATA}/scores.json")).get("wallets", []) if os.path.exists(f"{P.DATA}/scores.json") else []
        rows += [r for r in prev if r["wallet"] not in hist]
    rows.sort(key=lambda r: (bool(r["flags"]), -r["copyPnlSol"]))
    selected = [r["wallet"] for r in rows if not r["flags"]][:P.FOLLOW]
    json.dump({"updatedAt": P.now_iso(), "days": DAYS, "walletTxs": WALLET_TXS, "slippagePct": P.SLIP * 100, "feeSol": P.FEE_SOL,
               "tradeSol": P.TRADE_SOL, "rules": {"minTokens": MIN_TOKENS, "minWinRatePct": MIN_WIN_RATE,
               "maxTxPerDay": BOT_TXS_PER_DAY, "minHoldSec": MIN_HOLD_SEC}, "selected": selected, "wallets": rows},
              open(f"{P.DATA}/scores.json", "w"))
    print(f"{len(rows)} wallets scored, {len(selected)} pass every rule", file=sys.stderr)


if __name__ == "__main__":
    main()
