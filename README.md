# SolChain top traders

A leaderboard of the most profitable wallets trading the hottest Solana pairs on Dexscreener, published at
https://alicetin1905-ux.github.io/SolChain/

Dexscreener's own Top Traders tab is not in its public API, so the leaderboard is rebuilt from on-chain swaps:

1. `scripts/collect.py` takes trending Solana tokens from Dexscreener (boosts, profiles, community takeovers) and GeckoTerminal's trending pools, then keeps the highest-volume pairs with at least $25k liquidity.
2. For each pair it reads the latest transactions on the pool account from Solana RPC and decodes the swaps per wallet.
3. PnL per wallet = sells received − buys paid + net tokens bought in the window at the current price. Bot-like wallets (same-slot buy and sell, or 40+ trades) are flagged.
4. Output goes to `data/*.json`, which `index.html` renders. Pair prices on the page refresh live from Dexscreener every minute.

The GitHub Action in `.github/workflows/update-data.yml` reruns the collector every 3 hours and commits fresh data. Run it by hand from the Actions tab with "Run workflow".

Run locally:

```sh
MAX_PAIRS=15 SWAPS=1500 python3 scripts/collect.py data
python3 -m http.server   # then open http://localhost:8000
```

Set `SOL_RPC` to use your own Solana RPC endpoint (faster than the free public one).

Not financial advice.
