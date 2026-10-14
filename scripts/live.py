"""Live paper copy-trading, meant to run on an always-on computer (see mac/README.md).

Subscribes to the followed wallets over a Solana RPC websocket, so a copy is simulated within
seconds of the leader's swap. Each fill is priced with a real Jupiter quote at that moment
(price impact included) instead of the fixed slippage the hourly GitHub run assumes.
Results are committed and pushed every 10 minutes, so the site stays current.
While this runs, the hourly GitHub workflow skips its own check.

Run from the repo root:  python3 scripts/live.py data
"""
import asyncio, functools, http.server, json, os, subprocess, sys, threading, time, urllib.parse, urllib.request
import websockets
import paper as P

JUP = "https://lite-api.jup.ag/swap/v1/quote"
PUBLISH_EVERY = int(os.environ.get("PUBLISH_EVERY") or 600)  # seconds between commits to the site
NO_GIT = os.environ.get("NO_GIT") == "1"  # for local tests: never commit or push
LOCAL_EVERY = 60  # seconds between local writes, for the dashboard on this computer
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT") or 8787)  # http://localhost:8787/copy.html; 0 turns it off
REFRESH_FOLLOW_EVERY = 3600  # seconds between re-reading the leaderboard
POLL_SEC = int(os.environ.get("POLL_SEC") or 30)  # fallback when the RPC has no working websocket
RETRY_WS_AFTER = 1800  # seconds in polling mode before trying the websocket again
RECONCILE_SEC = 300  # safety-net poll while the websocket runs, for anything it missed


def ws_url(rpc):
    return rpc.replace("https://", "wss://", 1).replace("http://", "ws://", 1)


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, file=sys.stderr, flush=True)


def jup_quote(input_mint, output_mint, amount):
    q = urllib.parse.urlencode({"inputMint": input_mint, "outputMint": output_mint, "amount": int(amount), "slippageBps": 500})
    req = urllib.request.Request(f"{JUP}?{q}", headers={"user-agent": "solchain-live/1.0"})
    with urllib.request.urlopen(req, timeout=10, context=P.SSL_CTX) as r:
        d = json.load(r)
    if "outAmount" not in d:
        raise RuntimeError(d.get("error") or "no route")
    return int(d["outAmount"])


def jup_fill(side, ev, qty):
    """Fill price in SOL per token from a Jupiter quote now; falls back to the slippage model."""
    dec = ev.get("decimals")
    try:
        if dec is None:
            raise RuntimeError("unknown decimals")
        if side == "buy":
            spend = P.TRADE_SOL - P.FEE_SOL
            out = jup_quote(P.WSOL, ev["mint"], spend * 1e9) / 10 ** dec
            return spend / out, "jupiter"
        out_sol = jup_quote(ev["mint"], P.WSOL, qty * 10 ** dec) / 1e9
        return out_sol / qty, "jupiter"
    except Exception as e:
        log(f"  jupiter quote failed ({e}); using the slippage model")
        return P.model_fill(side, ev, qty)


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True)


def git_sync(commit):
    """Pull the latest leaderboard and, when asked, push the paper results."""
    if NO_GIT:
        return
    if commit:
        git("add", f"{P.DATA}/paper.json", P.STATE)
        if git("diff", "--cached", "--quiet").returncode != 0:
            git("commit", "-q", "-m", "Update paper copy-trade (live)")
    code_before = git("rev-parse", "HEAD:scripts").stdout
    r = git("pull", "-q", "--rebase", "--autostash", "origin", "main")
    if r.returncode != 0:
        git("rebase", "--abort")
        log(f"git pull failed: {r.stderr.strip()[:200]}")
        return
    if commit:
        r = git("push", "-q", "origin", "HEAD:main")
        if r.returncode != 0:
            log(f"git push failed: {r.stderr.strip()[:200]}")
    if git("rev-parse", "HEAD:scripts").stdout != code_before and os.environ.get("SOLCHAIN_SERVICE") == "1":
        log("new engine code pulled from GitHub; restarting to use it")
        os._exit(0)  # launchd (mac/install.sh) starts the engine again with the new code


def log_trades(trades):
    for t in trades:
        if t["side"] == "skip":
            log(f"  skipped {t['symbol']} bought by {t['wallet'][:6]}: {t['reason']}")
        elif t.get("strategy", "follow") == "follow":  # the other strategies make the same entry
            log(f"  {t['side']} {t['symbol']} copying {t['wallet'][:6]}: {t['sol']:+.3f} SOL, {t.get('lagSec')}s after the leader ({t.get('fill')})")


class SubscriptionFailed(Exception):
    pass


class Engine:
    def __init__(self):
        self.st = P.load_state()
        self.lock = asyncio.Lock()
        self.queue = asyncio.Queue()
        self.sp = P.sol_usd()
        self.top, self.watch = P.follow_list(self.st)
        self.polling = os.environ.get("FORCE_POLL") == "1"  # for RPCs without a working websocket

    async def catch_up(self):
        """Copy what the wallets did while the engine was offline (priced with the slippage model)."""
        async with self.lock:
            events = await asyncio.to_thread(P.poll_events, self.st, self.watch, self.sp)
            if events:
                info = await asyncio.to_thread(P.token_info, {e["mint"] for e in events})
                P.apply_events(self.st, events, info)
            log(f"caught up: {len(events)} swaps since the last check")

    async def handle(self, wallet, sig):
        tx = None
        for _ in range(8):  # a just-confirmed transaction can take a moment to be readable
            tx = await asyncio.to_thread(P.fetch_tx, sig, "confirmed")
            if tx:
                break
            await asyncio.sleep(1)
        sw = tx and P.parse_swap(tx, wallet, self.sp)
        async with self.lock:
            self.st["wallets"][wallet]["lastSig"] = sig
            if not sw:
                return
            ev = {**sw, "time": tx.get("blockTime") or time.time(), "sig": sig, "wallet": wallet, "live": True}
            info = await asyncio.to_thread(P.token_info, {ev["mint"]})
            before = len(self.st["trades"])
            await asyncio.to_thread(P.apply_events, self.st, [ev], info, jup_fill)
            log_trades(self.st["trades"][before:])

    async def worker(self):
        while True:
            wallet, sig = await self.queue.get()
            try:
                await self.handle(wallet, sig)
            except Exception as e:
                log(f"error on {sig[:10]}: {e}")

    async def publisher(self):
        """Write the results locally every minute; commit them for the site every PUBLISH_EVERY seconds."""
        last_follow = last_push = time.time()
        while True:
            await asyncio.sleep(LOCAL_EVERY)
            async with self.lock:
                try:
                    self.sp = await asyncio.to_thread(P.sol_usd)
                    await asyncio.to_thread(P.publish, self.st, self.sp, self.top, "live")
                    if time.time() - last_push >= PUBLISH_EVERY:
                        last_push = time.time()
                        await asyncio.to_thread(git_sync, True)
                except Exception as e:
                    log(f"publish failed: {e}")
                if time.time() - last_follow > REFRESH_FOLLOW_EVERY:
                    last_follow = time.time()
                    top, watch = P.follow_list(self.st)
                    if set(watch) != set(self.watch):
                        log("leaderboard changed; resubscribing")
                        self.top, self.watch = top, watch
                        return  # ends this round; run() resubscribes

    async def subscribe(self):
        async with websockets.connect(ws_url(P.RPC), ping_interval=30, max_size=None, ssl=P.SSL_CTX) as ws:
            pending = {}
            for i, w in enumerate(self.watch):
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": i, "method": "logsSubscribe",
                                          "params": [{"mentions": [w]}, {"commitment": "confirmed"}]}))
                pending[i] = w
            subs = {}
            log(f"watching {len(self.watch)} wallets live")
            async for raw in ws:
                msg = json.loads(raw)
                if "id" in msg and msg["id"] in pending:
                    if "error" in msg:
                        raise SubscriptionFailed(msg["error"].get("message"))
                    subs[msg.get("result")] = pending.pop(msg["id"])
                    continue
                if (msg.get("params") or {}).get("error"):
                    raise SubscriptionFailed(msg["params"]["error"].get("message"))
                if msg.get("method") != "logsNotification":
                    continue
                val = msg["params"]["result"]["value"]
                wallet = subs.get(msg["params"]["subscription"])
                if wallet and val.get("err") is None:
                    await self.queue.put((wallet, val["signature"]))

    async def poll(self, every, for_sec=float("inf")):
        """Check every wallet on a timer: the fallback feed, or the safety net beside the websocket."""
        log(f"polling {len(self.watch)} wallets every {every}s")
        started = time.time()
        while time.time() - started < for_sec:
            await asyncio.sleep(every)
            async with self.lock:
                events = await asyncio.to_thread(P.poll_events, self.st, self.watch, self.sp)
                if not events:
                    continue
                for ev in events:
                    ev["live"] = True
                info = await asyncio.to_thread(P.token_info, {e["mint"] for e in events})
                before = len(self.st["trades"])
                await asyncio.to_thread(P.apply_events, self.st, events, info, jup_fill)
                log_trades(self.st["trades"][before:])

    async def run(self):
        await asyncio.to_thread(git_sync, False)
        self.top, self.watch = P.follow_list(self.st)
        asyncio.create_task(self.worker())
        while True:
            await self.catch_up()
            async with self.lock:
                await asyncio.to_thread(P.publish, self.st, self.sp, self.top, "live")
                await asyncio.to_thread(git_sync, True)
            pub = asyncio.create_task(self.publisher())
            if self.polling:
                tasks = {pub, asyncio.create_task(self.poll(POLL_SEC, RETRY_WS_AFTER))}
            else:
                tasks = {pub, asyncio.create_task(self.subscribe()), asyncio.create_task(self.poll(RECONCILE_SEC))}
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in tasks:
                t.cancel()
            self.polling = os.environ.get("FORCE_POLL") == "1"
            for t in done:
                err = None if t.cancelled() else t.exception()
                if isinstance(err, SubscriptionFailed):
                    log(f"websocket subscription refused ({err}); switching to polling")
                    self.polling = True
                elif err:
                    log(f"connection lost: {err}; reconnecting in 5s")
            await asyncio.sleep(5)


def serve_dashboard():
    """Serve the repo folder on this computer only, so copy.html shows the engine's latest local data."""
    if not DASHBOARD_PORT:
        return

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", DASHBOARD_PORT), functools.partial(Quiet, directory=os.getcwd()))
    except OSError as e:
        log(f"dashboard not started: {e}")
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"dashboard: http://localhost:{DASHBOARD_PORT}/copy.html")


if __name__ == "__main__":
    serve_dashboard()
    log(f"live paper engine starting; {P.FOLLOW} wallets, {P.TRADE_SOL} SOL per buy")
    asyncio.run(Engine().run())
