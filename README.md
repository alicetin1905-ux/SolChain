# SolChain top traders

A leaderboard of the most profitable wallets trading the hottest Solana pairs on Dexscreener, published at
https://alicetin1905-ux.github.io/SolChain/

Dexscreener's own Top Traders tab is not in its public API, so the leaderboard is rebuilt from on-chain swaps:

1. `scripts/collect.py` takes trending Solana tokens from Dexscreener (boosts, profiles, community takeovers) and GeckoTerminal's trending pools, then keeps the highest-volume pairs with at least $25k liquidity.
2. For each pair it reads the latest transactions on the pool account from Solana RPC and decodes the swaps per wallet.
3. PnL per wallet = sells received − buys paid + net tokens bought in the window at the current price. Bot-like wallets (same-slot buy and sell, or 40+ trades) are flagged.
4. Output goes to `data/*.json`, which `index.html` renders. Pair prices on the page refresh live from Dexscreener every minute.

The GitHub Action in `.github/workflows/update-data.yml` reruns the collector once a day (06:47 UTC) and commits fresh data. Run it by hand from the Actions tab with "Run workflow".

Run locally:

```sh
MAX_PAIRS=15 SWAPS=1500 python3 scripts/collect.py data
python3 -m http.server   # then open http://localhost:8000
```

## Own RPC

The free public RPC is slow and rate-limited. To use your own endpoint (Helius, QuickNode, Alchemy, …):

1. Copy your Solana mainnet RPC URL, e.g. `https://mainnet.helius-rpc.com/?api-key=YOUR_KEY`.
2. In the repo go to **Settings → Secrets and variables → Actions → New repository secret**, name it `SOL_RPC` and paste the URL.
3. Optional: under the **Variables** tab add `RPC_THREADS` (default 2, sized for the Helius free plan) to match your plan's requests per second.

One run makes about 22,500 RPC calls (15 pairs × 1,500 transactions). Check that against your plan's monthly credits and adjust the cron in the workflow if needed.

Locally: `SOL_RPC=https://... python3 scripts/collect.py data`.

Not financial advice.

## Paper copy-trading

`copy.html` shows a simulated copy-trading engine (`scripts/paper.py`, run hourly by `.github/workflows/paper-trade.yml`).
It follows the scored wallets and copies their swaps from a paper wallet that started with 5 SOL:
each buy spends 20% of the wallet's value (free SOL plus open copies) and is skipped when too little SOL is free.
A buy opens one position per wallet and token (tokens with under $5k liquidity, or under $5k market cap while still on the pump.fun bonding curve, are skipped), and a sell by that wallet sells the same share of the copy, back into the wallet.
Each fill is 3% worse than the wallet's own price plus 0.001 SOL in fees. No real trades are made.
Tune it with the `FOLLOW`, `WALLET_SOL`, `BUY_PCT`, `SLIPPAGE_PCT` and `FEE_SOL` environment variables.
The first run (14 to 20 October, 1 SOL per buy without a wallet limit, four exit strategies side by side) is kept in `data/archive/`.

For faster reactions, run the live engine (`scripts/live.py`) on an always-on Mac; see [mac/README.md](mac/README.md).
It watches the wallets in real time, prices every copy with a Jupiter quote, and takes over from the hourly check while it runs.

## Choosing wallets and exits

- `scripts/early.py` (daily, `early-buyers.yml`) finds wallets that were among the first buyers of several pumped pump.fun tokens, from each token's bonding-curve history. Results: `early.html`.
- `scripts/score.py` (daily, `score-wallets.yml`) reads up to 250 swaps from the last 30 days of each candidate wallet and backtests copying it. Wallets that look like bots, flip faster than a minute, traded fewer than 5 tokens or lose in the backtest are left out. Results: `wallets.html`.
- The copy-trade engine follows the 10 best scored wallets, drops a wallet after 10 losing copies, skips risky tokens (mint or freeze authority kept, concentrated holders, under $5k liquidity or market cap) and buys that already ran 30% past the wallet's price. It sells when the wallet sells: in the first run that beat a -30% stop, selling half at 2x and selling after 24 hours.

## On the iPhone home screen

The site is a web app: open https://alicetin1905-ux.github.io/SolChain/copy.html in Safari, tap Share, then "Add to Home Screen". It opens full screen with its own icon, always in the dark theme, and shows the last loaded data when offline (`manifest.webmanifest`, `sw.js`, `icons/`).

## Watchlist

Add wallets you found elsewhere (GMGN, Birdeye, Kolscan…) with the "Add or remove wallets" button on `wallets.html`. It opens a GitHub issue form; the `watchlist.yml` workflow adds the addresses to `data/watchlist.json`, scores them straight away, replies in the issue with the verdict and closes it. Only issues opened by the repository owner change the watchlist. Watchlist wallets are rescored daily with the other candidates, and the engine copies those that pass every check.
