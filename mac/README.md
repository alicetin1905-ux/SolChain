# Live engine on a Mac

`scripts/live.py` follows the copied wallets in real time and prices every copy with a Jupiter quote at the moment it reacts.
It commits its results every 10 minutes, so the site's Copy-trade page stays current. While it runs, the hourly GitHub check skips itself; when the Mac is off for 30 minutes or more, the hourly check takes over again.

## Install (once)

In Terminal:

```sh
xcode-select --install          # Python 3 and git, if you don't have them yet
brew install gh                 # GitHub CLI (needs Homebrew: https://brew.sh)
gh auth login                   # log in to GitHub so the engine can push its results
gh repo clone alicetin1905-ux/SolChain ~/SolChain
cd ~/SolChain && ./mac/install.sh
```

`install.sh` asks for your Helius RPC URL once and stores it in `~/.solchain.env`, readable only by you. It then installs the engine as a background service that starts at login and restarts itself if it stops.

## Day to day

- Follow it: `tail -f ~/Library/Logs/solchain-live.log`
- Update to the latest code: `cd ~/SolChain && git pull && ./mac/install.sh`
- Stop it: `./mac/uninstall.sh`

## Keep the Mac awake

The engine keeps the Mac from sleeping while it runs, but a MacBook still sleeps when its lid is closed on battery. Keep it on power, or leave the lid open. Trades missed while the Mac sleeps are caught up when it wakes, priced with the 3% slippage estimate instead of a live quote.

## How it reacts

It subscribes to each wallet over the RPC's websocket and also polls every 5 minutes as a safety net. If the RPC refuses websocket subscriptions, it polls every 30 seconds instead (set `POLL_SEC` in `~/.solchain.env` to change that).

## Phone notifications

The engine can push a notification for every copied buy, every closed copy with its result, every take-profit, and a daily summary at 21:00, through [ntfy](https://ntfy.sh). Install the ntfy app, subscribe to a topic with a long random name (anyone who knows the name can read the messages), and give that name to `install.sh` when it asks for the ntfy topic. To add it later: `echo 'NTFY_TOPIC=your-topic' >> ~/.solchain.env && ./mac/install.sh`.
