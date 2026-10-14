"""Early buyers: wallets that bought several pumped tokens in their first minutes.

For each recently launched pump.fun token that has since grown a lot, this reads the first trades on
its bonding curve (the pre-migration market) from an archival Solana RPC, and records who bought
first, how soon after launch, and how much the price has multiplied since. Wallets that turn up
early in two or more of these tokens are written to data/early.json; the copy-trade engine follows
the best of them (EARLY_FOLLOW in paper.py).

Settings (environment):
  SOL_RPC       archival Solana RPC (Helius works; the free publicnode endpoint lacks old history)
  MAX_TOKENS    pumped tokens to analyse (default 25)
  BUYERS        first buyers to record per token (default 60)
"""
import hashlib, json, os, sys, time
import paper as P

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
MAX_TOKENS = int(os.environ.get("MAX_TOKENS") or 25)
BUYERS = int(os.environ.get("BUYERS") or 60)
MAX_PAGES = 20             # bonding curves with more than 20,000 trades are skipped
MIN_MCAP_USD = 300_000     # pump.fun tokens launch near $5k, so this is roughly 60x or more
MAX_AGE_DAYS = 10
MIN_AGE_HOURS = 6
SNIPER_SLOTS = 1           # buys within this many slots of the launch count as snipers or bundles

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
ED_P = 2 ** 255 - 19
ED_D = (-121665 * pow(121666, ED_P - 2, ED_P)) % ED_P


def b58decode(s):
    n = 0
    for c in s:
        n = n * 58 + B58.index(c)
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\0" * (len(s) - len(s.lstrip("1"))) + b


def b58encode(b):
    n, s = int.from_bytes(b, "big"), ""
    while n:
        n, r = divmod(n, 58)
        s = B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + s


def on_curve(b):
    """Whether 32 bytes are a valid ed25519 point; program addresses must not be."""
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    if y >= ED_P:
        return False
    u, v = (y * y - 1) % ED_P, (ED_D * y * y + 1) % ED_P
    x2 = u * pow(v, ED_P - 2, ED_P) % ED_P
    return x2 == 0 or pow(x2, (ED_P - 1) // 2, ED_P) == 1


def program_address(seeds, program):
    prog = b58decode(program)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + prog + b"ProgramDerivedAddress").digest()
        if not on_curve(h):
            return b58encode(h)


def bonding_curve(mint):
    return program_address([b"bonding-curve", b58decode(mint)], PUMP)


def candidates():
    """Recently launched pump.fun tokens that have grown a lot, from Dexscreener and GeckoTerminal feeds."""
    mints = []
    for path in ["/token-boosts/top/v1", "/token-boosts/latest/v1", "/token-profiles/latest/v1", "/community-takeovers/latest/v1"]:
        try:
            mints += [t["tokenAddress"] for t in P.get(P.DS + path) if t.get("chainId") == "solana"]
        except RuntimeError:
            pass
    for page in (1, 2, 3):
        try:
            for p in P.get(f"https://api.geckoterminal.com/api/v2/networks/solana/trending_pools?page={page}")["data"]:
                mints.append(p["relationships"]["base_token"]["data"]["id"].split("_", 1)[1])
        except Exception:
            pass
    try:
        mints += [p["token"] for p in json.load(open(f"{P.DATA}/pairs.json"))]
    except FileNotFoundError:
        pass
    mints = list(dict.fromkeys(mints))
    best = {}
    for i in range(0, len(mints), 30):
        for p in P.get(f"{P.DS}/tokens/v1/solana/{','.join(mints[i:i+30])}"):
            m = p["baseToken"]["address"]
            if m not in best or ((p.get("liquidity") or {}).get("usd") or 0) > ((best[m].get("liquidity") or {}).get("usd") or 0):
                best[m] = p
    now = time.time()
    out = []
    for m, p in best.items():
        created = (p.get("pairCreatedAt") or 0) / 1000
        age_h = (now - created) / 3600 if created else None
        mcap = p.get("marketCap") or p.get("fdv") or 0
        if age_h is None or not (MIN_AGE_HOURS <= age_h <= MAX_AGE_DAYS * 24) or mcap < MIN_MCAP_USD:
            continue
        out.append({"mint": m, "symbol": p["baseToken"]["symbol"], "url": p.get("url"), "mcapUsd": mcap,
                    "priceUsd": float(p.get("priceUsd") or 0), "pairAgeHours": round(age_h, 1)})
    return sorted(out, key=lambda t: -t["mcapUsd"])


def oldest_signatures(addr):
    """All signatures of an address, oldest first, or None when there are more than MAX_PAGES pages."""
    sigs, before = [], None
    for _ in range(MAX_PAGES):
        page = P.rpc("getSignaturesForAddress", [addr, {"limit": 1000, **({"before": before} if before else {})}]) or []
        sigs += page
        if len(page) < 1000:
            return sigs[::-1]
        before = page[-1]["signature"]
    return None


def first_buyers(tok, sp):
    bc = bonding_curve(tok["mint"])
    info = P.rpc("getAccountInfo", [bc, {"encoding": "base64"}])
    if not info or not info.get("value") or info["value"]["owner"] != PUMP:
        return None, "not a pump.fun token"
    sigs = oldest_signatures(bc)
    if not sigs:
        return None, "too many bonding-curve trades to reach the launch"
    launch = sigs[0]
    launch_tx = P.fetch_tx(launch["signature"])
    if not launch_tx:
        return None, "launch transaction not readable (RPC without archive?)"
    keys = launch_tx["transaction"]["message"]["accountKeys"]
    creator = keys[0]["pubkey"] if isinstance(keys[0], dict) else keys[0]
    launch_slot, launch_time = launch_tx["slot"], launch_tx.get("blockTime") or launch.get("blockTime")
    cur_sol = tok["priceUsd"] / sp
    buyers, seen = [], {creator}
    ok = [s for s in sigs if s.get("err") is None]
    for i in range(0, min(len(ok), 600), 40):
        chunk = ok[i:i + 40]
        for s, tx in zip(chunk, P.POOL.map(lambda s: P.fetch_tx(s["signature"]), chunk)):
            if not tx:
                continue
            k = tx["transaction"]["message"]["accountKeys"]
            for signer in [x["pubkey"] for x in k if isinstance(x, dict) and x.get("signer")]:
                if signer in seen:
                    continue
                sw = P.parse_swap(tx, signer, sp)
                if not sw or sw["mint"] != tok["mint"] or sw["dtok"] <= 0:
                    continue
                seen.add(signer)
                entry = sw["sol"] / sw["dtok"]
                buyers.append({"wallet": signer, "rank": len(buyers) + 1,
                               "secAfterLaunch": (tx.get("blockTime") or launch_time) - launch_time,
                               "sniper": tx["slot"] - launch_slot <= SNIPER_SLOTS,
                               "entrySol": round(sw["sol"], 4), "multiple": round(cur_sol / entry, 2) if entry else None})
        if len(buyers) >= BUYERS:
            break
    tok.update({"launchAt": P.now_iso(launch_time), "creator": creator, "bondingTrades": len(sigs),
                "buyersAnalyzed": len(buyers[:BUYERS]),
                "launchMultiple": buyers[0]["multiple"] if buyers else None})
    return buyers[:BUYERS], None


def main():
    sp = P.sol_usd()
    toks = candidates()
    print(f"{len(toks)} pumped tokens launched in the last {MAX_AGE_DAYS} days; analysing up to {MAX_TOKENS}", file=sys.stderr)
    done, hits = [], {}
    for tok in toks:
        if len(done) >= MAX_TOKENS:
            break
        buyers, why = first_buyers(tok, sp)
        if buyers is None:
            print(f"  {tok['symbol']}: skipped, {why}", file=sys.stderr)
            continue
        done.append(tok)
        print(f"  {tok['symbol']}: {len(buyers)} first buyers, launch x{tok['launchMultiple']}", file=sys.stderr)
        for b in buyers:
            hits.setdefault(b["wallet"], []).append({"symbol": tok["symbol"], "mint": tok["mint"], **{k: b[k] for k in ("rank", "secAfterLaunch", "sniper", "entrySol", "multiple")}})
    creators = {t["creator"] for t in done}
    wallets = []
    for w, hs in hits.items():
        if len(hs) < 2 or w in creators:
            continue
        mult = sorted(h["multiple"] for h in hs if h["multiple"])
        wallets.append({"wallet": w, "tokens": len(hs), "symbols": [h["symbol"] for h in hs],
                        "medianMultiple": mult[len(mult) // 2] if mult else None,
                        "bestMultiple": mult[-1] if mult else None,
                        "avgRank": round(sum(h["rank"] for h in hs) / len(hs), 1),
                        "sniper": sum(h["sniper"] for h in hs) > len(hs) / 2, "hits": hs})
    wallets.sort(key=lambda r: (r["sniper"], -r["tokens"], -(r["medianMultiple"] or 0)))
    json.dump({"updatedAt": P.now_iso(), "solUsd": sp, "buyersPerToken": BUYERS, "minMcapUsd": MIN_MCAP_USD,
               "maxAgeDays": MAX_AGE_DAYS, "walletsSeen": len(hits), "tokens": done, "wallets": wallets[:300]},
              open(f"{P.DATA}/early.json", "w"))
    print(f"{len(done)} tokens, {len(hits)} early buyers, {len(wallets)} early in 2+ tokens", file=sys.stderr)


if __name__ == "__main__":
    main()
