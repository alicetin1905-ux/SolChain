"""Wallet scores: judge candidate wallets on their recent history instead of one snapshot.

Candidates are the leaderboard's best non-bot wallets, the repeat early buyers and the wallets the
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
"""
import collections, json, os, sys, time
import paper as P

CANDIDATES = int(os.environ.get("CANDIDATES") or 30)
WALLET_TXS = int(os.environ.get("WALLET_TXS") or 250)
DAYS = int(os.environ.get("DAYS") or 30)
MIN_TOKENS = 5          # fewer tokens than this is too small a sample to judge
MIN_WIN_RATE = 30       # % of copied tokens that must have made money
BOT_TXS_PER_DAY = 150   # more transactions a day than this looks like a bot
MIN_HOLD_SEC = 60       # a median hold shorter than this can't be copied in time


def candidates():
    out = []
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
    """The wallet's swaps in the window (oldest first), its transactions per day, and the days the swaps cover."""
    since = time.time() - DAYS * 86400
    sigs, before = [], None
    while len(sigs) < WALLET_TXS * 4:
        page = P.rpc("getSignaturesForAddress", [wallet, {"limit": 1000, **({"before": before} if before else {})}]) or []
        sigs += [s for s in page if (s.get("blockTime") or 0) >= since]
        if len(page) < 1000 or (page[-1].get("blockTime") or 0) < since:
            break
        before = page[-1]["signature"]
    total = len(sigs)
    ok = [s for s in sigs if s.get("err") is None][:WALLET_TXS]
    swaps = []
    for s, tx in zip(ok, P.POOL.map(lambda s: P.fetch_tx(s["signature"]), ok)):
        sw = tx and P.parse_swap(tx, wallet, sp)
        if sw:
            swaps.append({**sw, "time": tx.get("blockTime") or s.get("blockTime") or 0})
    span = lambda xs: max(1 / 24, (time.time() - min((x.get("blockTime") or time.time()) for x in xs)) / 86400) if xs else 0
    # activity rate over every transaction found; coverage over the ones actually read
    return sorted(swaps, key=lambda x: x["time"]), total / span(sigs) if sigs else 0, span(ok)


def evaluate(wallet, swaps, tx_per_day, span_days, prices):
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
    tokens = wins = copy_wins = 0
    holds = []
    for mint, t in per.items():
        if t["buyTok"] <= 0:
            continue
        tokens += 1
        cur = prices.get(mint, 0.0)
        left = max(0.0, t["buyTok"] - t["sellTok"])
        pnl = t["sellSol"] + left * cur - t["buySol"]
        own_pnl += pnl
        wins += pnl > 0
        b = t["buySol"] / t["buyTok"]
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
            "copyWinRatePct": copy_win_rate, "medianHoldSec": median_hold, "flags": flags}


def main():
    sp = P.sol_usd()
    cands = candidates()
    print(f"scoring {len(cands)} wallets over {DAYS} days, up to {WALLET_TXS} transactions each", file=sys.stderr)
    hist = {}
    for i, w in enumerate(cands, 1):
        swaps, rate, span = history(w, sp)
        hist[w] = (swaps, rate, span)
        print(f"  {i}/{len(cands)} {w[:6]}: {len(swaps)} swaps over {span:.1f} days, {rate:.0f} transactions a day", file=sys.stderr)
    mints = {s["mint"] for swaps, _, _ in hist.values() for s in swaps}
    info = P.token_info(mints) if mints else {}
    # tokens with no pair, or too little liquidity to sell into, are worth 0: an illiquid pool's price can't be cashed out
    prices = {m: (i.get("priceUsd") or 0) / sp for m, i in info.items() if (i.get("liquidityUsd") or 0) >= 10_000}
    try:
        traders = {r["wallet"] for r in json.load(open(f"{P.DATA}/traders.json"))}
    except FileNotFoundError:
        traders = set()
    early = set(P.early_buyers())
    rows = []
    for w, (swaps, rate, span) in hist.items():
        r = evaluate(w, swaps, rate, span, prices)
        r["source"] = "early" if w in early else "leaderboard" if w in traders else "followed"
        rows.append(r)
    rows.sort(key=lambda r: (bool(r["flags"]), -r["copyPnlSol"]))
    selected = [r["wallet"] for r in rows if not r["flags"]][:P.FOLLOW]
    json.dump({"updatedAt": P.now_iso(), "days": DAYS, "walletTxs": WALLET_TXS, "slippagePct": P.SLIP * 100, "feeSol": P.FEE_SOL,
               "tradeSol": P.TRADE_SOL, "rules": {"minTokens": MIN_TOKENS, "minWinRatePct": MIN_WIN_RATE,
               "maxTxPerDay": BOT_TXS_PER_DAY, "minHoldSec": MIN_HOLD_SEC}, "selected": selected, "wallets": rows},
              open(f"{P.DATA}/scores.json", "w"))
    print(f"{len(rows)} wallets scored, {len(selected)} pass every rule", file=sys.stderr)


if __name__ == "__main__":
    main()
