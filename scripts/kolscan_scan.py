"""Scan every trader kolscan.io lists (about 540, not only the 150 leaderboard places) for quiet traders worth copying.

A cheap first look per wallet: wallets too busy to read are skipped after a few signature pages, the others get a
short read of SCAN_TXS transactions and a copy backtest at the measured copy cost. The most promising are written
to data/kol_scan.json; score.py then scores them properly (full read, second read, live results).

Settings (environment): SOL_RPC, SCAN_TXS (default 80), SCAN_KEEP (default 25), RESCAN_DAYS (default 7)
"""
import json, os, re, sys, time, urllib.request
import paper as P
import score as S

SCAN_TXS = int(os.environ.get("SCAN_TXS") or 80)
SCAN_KEEP = int(os.environ.get("SCAN_KEEP") or 25)
RESCAN_DAYS = float(os.environ.get("RESCAN_DAYS") or 7)  # wallets scored more recently than this are not scanned again


def kol_list():
    """Every (wallet, name) on the kolscan.io leaderboard page, including its full list of known traders."""
    req = urllib.request.Request("https://kolscan.io/leaderboard", headers={"user-agent": "Mozilla/5.0 (solchain wallet scoring)"})
    with urllib.request.urlopen(req, timeout=20, context=P.SSL_CTX) as r:
        page = r.read().decode("utf-8", "replace").replace('\\"', '"')
    return dict(re.findall(r'"wallet_address":"([1-9A-HJ-NP-Za-km-z]{32,44})","name":"([^"]*)"', page))


def main():
    sp = P.sol_usd()
    kols = kol_list()
    try:
        scored = {r["wallet"]: r for r in json.load(open(f"{P.DATA}/scores.json"))["wallets"]}
    except FileNotFoundError:
        scored = {}
    recent = {w for w, r in scored.items() if r.get("scoredAt") and time.time() - P.epoch(r["scoredAt"]) < RESCAN_DAYS * 86400}
    todo = [w for w in kols if w not in recent]
    print(f"{len(kols)} Kolscan traders, {len(todo)} not scored in the last {RESCAN_DAYS:g} days", file=sys.stderr)
    gaps = S.fill_gaps(sp)
    hist = {}
    for i, w in enumerate(todo, 1):
        hist[w] = S.history(w, sp, SCAN_TXS, 2)
        swaps = hist[w][0]
        print(f"  {i}/{len(todo)} {w[:6]} {kols[w][:20]}: " + ("too busy" if swaps is None else f"{len(swaps)} swaps"), file=sys.stderr)
    mints = {s["mint"] for swaps, _, _ in hist.values() for s in swaps or []}
    info = P.token_info(mints) if mints else {}
    prices = {m: (i.get("priceUsd") or 0) / sp for m, i in info.items() if P.tradable(i)}
    supply = {m: i["marketCapUsd"] / i["priceUsd"] for m, i in info.items() if i.get("priceUsd") and i.get("marketCapUsd")}
    rows = []
    for w, (swaps, rate, span) in hist.items():
        if not swaps:
            continue
        r = S.evaluate(w, swaps, rate, span, prices, supply, P.MIN_MCAP_USD / sp, S.fill_cost(gaps, w))
        # a short read holds few tokens, so "too few tokens" is expected here; the full score decides later
        hard = [f for f in r["flags"] if f != "too few tokens"]
        if r["tokens"] >= 3 and not hard:
            rows.append({"wallet": w, "name": kols[w], "tokens": r["tokens"], "copyPnlSol": r["copyPnlSol"],
                         "copyWinRatePct": r["copyWinRatePct"], "medianHoldSec": r["medianHoldSec"], "daysCovered": r["daysCovered"]})
    rows.sort(key=lambda r: -r["copyPnlSol"] / max(r["tokens"], 1))
    json.dump({"updatedAt": P.now_iso(), "scanned": len(hist), "scanTxs": SCAN_TXS, "promising": rows[:SCAN_KEEP]},
              open(f"{P.DATA}/kol_scan.json", "w"))
    print(f"{len(rows)} promising, keeping {min(len(rows), SCAN_KEEP)} for full scoring", file=sys.stderr)
    for r in rows[:SCAN_KEEP]:
        print(f"  {r['wallet'][:6]} {r['name'][:20]}: {r['tokens']} tokens, {r['copyPnlSol']:+.2f} SOL, {r['copyWinRatePct']}% won", file=sys.stderr)


if __name__ == "__main__":
    main()
