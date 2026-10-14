"""Update data/watchlist.json from a "Watchlist" issue, and report how the wallets scored.

  python scripts/watchlist.py apply data   reads ISSUE_BODY, adds or removes wallets, prints the added ones
  python scripts/watchlist.py report data  reads WALLETS and writes the issue reply to watchlist_reply.md
"""
import json, os, re, sys, time

DATA = sys.argv[2] if len(sys.argv) > 2 else "data"
PATH = f"{DATA}/watchlist.json"
MAX = 100
ADDR = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


def section(body, heading):
    m = re.search(rf"###\s*{re.escape(heading)}\s*\n(.*?)(?=\n###\s|\Z)", body, re.S)
    v = (m.group(1).strip() if m else "")
    return "" if v == "_No response_" else v


def apply():
    body = os.environ.get("ISSUE_BODY", "")
    action = section(body, "Action").lower() or "add"
    wallets = list(dict.fromkeys(ADDR.findall(section(body, "Wallet addresses") or body)))
    note = section(body, "Where you found them (optional)")[:80]
    try:
        current = json.load(open(PATH))
    except FileNotFoundError:
        current = []
    have = {w["wallet"] for w in current}
    if action.startswith("remove"):
        current = [w for w in current if w["wallet"] not in wallets]
        added = []
    else:
        added = [w for w in wallets if w not in have]
        current += [{"wallet": w, "note": note, "added": time.strftime("%Y-%m-%d")} for w in added]
    json.dump(current[-MAX:], open(PATH, "w"), indent=1)
    print(" ".join(added))
    with open("watchlist_action.txt", "w") as f:
        f.write(f"{'remove' if action.startswith('remove') else 'add'} {len(wallets)}")


def report():
    wallets = os.environ.get("WALLETS", "").split()
    action, n = open("watchlist_action.txt").read().split()
    lines = []
    if action == "remove":
        lines.append(f"Removed {n} wallet{'s' if n != '1' else ''} from the watchlist.")
    elif not wallets:
        lines.append("No new wallets: every address was already on the watchlist, or none was found in the form.")
    else:
        scores = {r["wallet"]: r for r in json.load(open(f"{DATA}/scores.json"))["wallets"]}
        lines += [f"Added {len(wallets)} wallet{'s' if len(wallets) != 1 else ''} and scored {'it' if len(wallets) == 1 else 'them'} on the last 30 days:", "",
                  "| Wallet | Copy backtest | Copies won | Tokens | Verdict |", "|---|---|---|---|---|"]
        for w in wallets:
            r = scores.get(w)
            if not r:
                lines.append(f"| `{w[:4]}…{w[-4:]}` | — | — | — | not scored (no history found) |")
                continue
            verdict = "passes, the engine will copy it" if not r["flags"] else ", ".join(r["flags"])
            won = f"{r['copyWinRatePct']:.0f}%" if r["copyWinRatePct"] is not None else "—"
            lines.append(f"| `{w[:4]}…{w[-4:]}` | {r['copyPnlSol']:+.2f} SOL | {won} | {r['tokens']} | {verdict} |")
        lines += ["", "Watchlist wallets are scored again every day. Details: https://alicetin1905-ux.github.io/SolChain/wallets.html"]
    open("watchlist_reply.md", "w").write("\n".join(lines) + "\n")


if __name__ == "__main__":
    {"apply": apply, "report": report}[sys.argv[1]]()
