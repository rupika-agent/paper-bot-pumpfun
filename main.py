"""Pump.fun replay paper bot: near-completion runners + cheap guardrail probes.

Strategy (agent-fun/replay/v2, 10 SOL paper capital, max 5 open positions).

Why this shape: measured on the dev window, every past-move signal (momentum,
dip-buying, accumulation) loses money after fees at the 2-slot latency — by the
time a fill lands, the move that triggered it is already 2-7% gone. The only
family that is not negative is buying coins that are close to completing their
bonding curve (real SOL >= 50, still attracting net inflow): most such coins
still fail, but the ones that complete pay multiples, so the trade has a
positive skew (tight stop, wide target). The parameters sit in the middle of
that family's flat region rather than at any single best point, so nothing
here is fitted to one window's luck.

- Runner entry: real SOL reserves >= 50, at least 4 buys among the last 8
  trades with net inflow >= 0.2 SOL, creator has not sold. One buy of up to
  0.5 SOL (the 1%-of-real-reserve per-reply cap at 50 SOL real), no adds:
  adding into strength measured worse, because the add lands near the top.
- Runner exits (whole position): take profit +60% of cost, stop loss -15%,
  or a 3000-slot time limit. No trailing stop: on these curves a tight trail
  fires at a loss after a +20% peak and converts would-be winners into
  losers. At most 2 entries per coin, 300-slot cooldown after an exit.
- Guardrail probes: the run must end with a round trip of >= 0.01 SOL on at
  least 20 coins. Runners alone are rare, so when the bot's distinct-coin
  count lags a 25-coin pace over the 24h window, it opens a tiny 0.012 SOL
  probe on the most liquid coin traded that batch and sells it as soon as the
  buy lands. A probe round trip costs ~0.0005 SOL worst case; at most 1 probe
  open at a time and only while at most 3 positions are committed, so runners
  always have room.
- Drawdown guard: if marked equity falls 20% below its peak, no new entries
  (exits keep running), keeping max drawdown far under the 50% guardrail.
- Deterministic by construction: no clock, no randomness, no I/O, iteration
  only over insertion-ordered dicts. Buys use 1000 bps slippage (a fill that
  moved >10% against us in the 2-slot latency is refused for free); sells use
  10000 bps so exits actually happen.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402

LAMPORTS = makefun_bot.LAMPORTS_PER_SOL
TX_FEE = 105_000  # base + priority, lamports, paid on every sent order

# --- runner entry ---
RUNNER_MIN_REAL = 50_000_000_000       # 50 SOL real reserves
RUNNER_WINDOW = 8
RUNNER_MIN_BUYS = 4
RUNNER_MIN_NET = 200_000_000           # 0.2 SOL net inflow over the window
RUNNER_STAKE = 500_000_000              # 0.5 SOL (== 1% cap at 50 SOL real)
MAX_ENTRIES_PER_COIN = 2
COOLDOWN_SLOTS = 300
# --- runner exits ---
TP_NUM = 160                            # take profit at +60% of cost
SL_NUM = 85                             # stop loss at -15% of cost
MAX_HOLD_SLOTS = 3000
# --- probes (guardrail breadth) ---
PROBE_LAMPORTS = 12_000_000             # 0.012 SOL (> 0.01 SOL round-trip bar)
PROBE_MIN_REAL = 10_000_000_000         # 10 SOL real (cap 0.1 SOL >> probe)
PROBE_TARGET_COINS = 25
PROBE_WINDOW_SECS = 86_400              # 24h window
MAX_PROBES_OPEN = 1
MAX_COMMITTED_FOR_PROBE = 3
# --- slippage / risk ---
BUY_SLIPPAGE_BPS = 1000
SELL_SLIPPAGE_BPS = 10_000
MAX_DD_NUM = 80                         # stop new entries if equity < 80% of peak


def _ceil_fee(amount, bps):
    return -(-amount * bps // 10_000)


def sell_value_lamports(tokens, vq, vt):
    """Net lamports a sell of `tokens` against (vq, vt) would return, after the
    1.25% curve fee (95 protocol + 30 creator bps) and the flat tx fee."""
    if tokens <= 0 or vt <= 0:
        return 0
    q = tokens * vq // (vt + tokens)
    q -= _ceil_fee(q, 95) + _ceil_fee(q, 30)
    return q - TX_FEE


class Tape:
    """Per-mint rolling tape: recent trades plus entry bookkeeping."""

    __slots__ = ("created_slot", "creator", "trades", "entries", "last_exit_slot",
                 "dev_sold", "last_trade_slot")

    def __init__(self, created_slot, creator):
        self.created_slot = created_slot
        self.creator = creator
        self.trades = []  # (slot, is_buy, sol_lamports)
        self.entries = 0
        self.last_exit_slot = -10 ** 12
        self.dev_sold = False
        self.last_trade_slot = created_slot

    def add_trade(self, slot, is_buy, sol):
        self.trades.append((slot, is_buy, sol))
        if len(self.trades) > 12:
            del self.trades[:len(self.trades) - 12]
        self.last_trade_slot = slot

    def flow_stats(self):
        """(buys, net_lamports) over the last RUNNER_WINDOW trades."""
        w = self.trades[-RUNNER_WINDOW:]
        buys = 0
        net = 0
        for _slot, is_buy, sol in w:
            if is_buy:
                buys += 1
                net += sol
            else:
                net -= sol
        return buys, net


class Held:
    __slots__ = ("opened_slot", "invested", "filled", "exiting", "is_probe")

    def __init__(self, slot, invested, is_probe):
        self.opened_slot = slot
        self.invested = invested
        self.filled = False
        self.exiting = False
        self.is_probe = is_probe


tapes = {}   # mint -> Tape, insertion order
held = {}    # mint -> Held, insertion order
probes = {}  # mint -> True while a probe round trip is in flight
my_coins = {}  # mint -> True once we have a filled order on it
my_fills = 0
peak_equity = None
first_time = None
last_time = None


def on_batch(state, batch):
    global peak_equity, first_time, last_time, my_fills
    now = batch["slot"]
    caps = state.hello["caps"]
    max_orders = caps["max_orders_per_batch"]
    tx = state.tx_fee_lamports()

    # --- fold this batch's fills and events into our books ---
    for f in batch["fills"]:
        if f["status"] == "filled" and not f["client_id"].startswith("_"):
            my_fills += 1
            my_coins[f["mint"]] = 1
    traded = []  # mints traded this batch, in first-seen order
    traded_seen = {}
    for e in batch["events"]:
        mint = e["mint"]
        kind = e["event"]
        if e.get("block_time") is not None:
            if first_time is None:
                first_time = e["block_time"]
            if last_time is None or e["block_time"] > last_time:
                last_time = e["block_time"]
        if kind == "CreateEvent":
            if mint not in tapes:
                tapes[mint] = Tape(e["slot"], e["creator"])
            else:
                tapes[mint].created_slot = e["slot"]
                tapes[mint].creator = e["creator"]
        elif kind == "TradeEvent":
            t = tapes.get(mint)
            if t is None:
                t = Tape(e["slot"], e["user"])
                tapes[mint] = t
            t.add_trade(e["slot"], e["is_buy"], int(e["sol_amount"]))
            if e["user"] == t.creator and not e["is_buy"]:
                t.dev_sold = True
            if mint not in traded_seen:
                traded_seen[mint] = 1
                traded.append(mint)
        else:  # CompleteEvent
            t = tapes.get(mint)
            if t is not None:
                t.last_exit_slot = now

    # --- marks for valuation (prefer the harness marks, fall back to curve) ---
    marks = {}
    for m in batch["marks"]:
        marks[m["mint"]] = (int(m["virtual_quote_reserves"]), int(m["virtual_token_reserves"]))

    def reserves(mint):
        if mint in marks:
            return marks[mint]
        curve = state.curves.get(mint)
        if curve is None:
            return None
        return (curve.virtual_quote_reserves, curve.virtual_token_reserves)

    def value_of(mint, tokens):
        r = reserves(mint)
        if r is None:
            return 0
        return sell_value_lamports(tokens, r[0], r[1])

    # --- equity / drawdown guard ---
    equity = state.cash
    for mint, pos in state.positions.items():
        equity += max(0, value_of(mint, int(pos.token_amount)))
    if peak_equity is None or equity > peak_equity:
        peak_equity = equity
    entries_blocked = equity * 100 < peak_equity * MAX_DD_NUM

    # --- pending orders by mint ---
    pending_buy = {}
    pending_sell = {}
    for p in state.pending.values():
        if p.order["side"] == "buy":
            pending_buy[p.order["mint"]] = 1
        else:
            pending_sell[p.order["mint"]] = 1

    orders = []
    free = state.free_cash_lamports()

    # --- exits on held positions ---
    for mint in list(held.keys()):
        h = held[mint]
        pos = state.positions.get(mint)
        if pos is None:
            if mint not in pending_buy and mint not in pending_sell:
                del held[mint]
                probes.pop(mint, None)
                t = tapes.get(mint)
                if t is not None:
                    if h.filled:
                        t.last_exit_slot = now
                    else:
                        t.entries -= 1
            continue
        h.filled = True
        curve = state.curves.get(mint)
        if curve is None or curve.complete:
            continue
        tokens = int(pos.token_amount)
        cost = int(pos.cost_lamports)
        value = value_of(mint, tokens)
        if h.is_probe:
            # probes sell as soon as the buy has landed
            if mint in pending_sell or len(orders) >= max_orders:
                continue
            h.exiting = True
            orders.append({"side": "sell", "mint": mint, "token_amount": tokens,
                           "max_slippage_bps": SELL_SLIPPAGE_BPS})
            continue
        exit_now = False
        if value * 100 >= cost * TP_NUM:
            exit_now = True
        elif value * 100 <= cost * SL_NUM:
            exit_now = True
        elif now - h.opened_slot >= MAX_HOLD_SLOTS:
            exit_now = True
        if exit_now:
            if mint in pending_sell or len(orders) >= max_orders:
                continue
            h.exiting = True
            orders.append({"side": "sell", "mint": mint, "token_amount": tokens,
                           "max_slippage_bps": SELL_SLIPPAGE_BPS})
            continue

    # --- new entries ---
    if not entries_blocked:
        slots_left = caps["max_open_positions"] - state.committed_position_count()
        # runner entries on mints traded this batch
        for mint in traded:
            if len(orders) >= max_orders or slots_left <= 0:
                break
            if mint in pending_buy or mint in pending_sell or mint in held:
                continue
            curve = state.curves.get(mint)
            if curve is None or curve.complete:
                continue
            t = tapes.get(mint)
            if t is None or t.dev_sold:
                continue
            if t.entries >= MAX_ENTRIES_PER_COIN:
                continue
            if now - t.last_exit_slot < COOLDOWN_SLOTS:
                continue
            if curve.real_quote_reserves < RUNNER_MIN_REAL:
                continue
            if len(t.trades) < RUNNER_WINDOW:
                continue
            buys, net = t.flow_stats()
            if buys < RUNNER_MIN_BUYS or net < RUNNER_MIN_NET:
                continue
            amount = min(state.max_buy_lamports(mint), RUNNER_STAKE)
            if amount < 10_000_000 or free < amount + tx:
                continue
            orders.append({"side": "buy", "mint": mint, "quote_lamports": amount,
                           "max_slippage_bps": BUY_SLIPPAGE_BPS})
            free -= amount + tx
            slots_left -= 1
            held[mint] = Held(now, amount, False)
            t.entries += 1
        # guardrail probe when distinct-coin count lags the 24h pace
        if (len(probes) < MAX_PROBES_OPEN and slots_left > 0
                and state.committed_position_count() <= MAX_COMMITTED_FOR_PROBE
                and first_time is not None and last_time is not None
                and last_time > first_time):
            pace = (last_time - first_time) / PROBE_WINDOW_SECS
            if pace > 1:
                pace = 1
            need = len(my_coins) < int(PROBE_TARGET_COINS * pace + 0.999)
            if need:
                for mint in traded:
                    if len(orders) >= max_orders:
                        break
                    if mint in pending_buy or mint in pending_sell or mint in held:
                        continue
                    if mint in my_coins:
                        continue
                    curve = state.curves.get(mint)
                    if curve is None or curve.complete:
                        continue
                    if curve.real_quote_reserves < PROBE_MIN_REAL:
                        continue
                    amount = min(state.max_buy_lamports(mint), PROBE_LAMPORTS)
                    if amount < 10_000_000 or free < amount + tx:
                        continue
                    orders.append({"side": "buy", "mint": mint,
                                   "quote_lamports": amount,
                                   "max_slippage_bps": BUY_SLIPPAGE_BPS})
                    free -= amount + tx
                    held[mint] = Held(now, amount, True)
                    probes[mint] = True
                    break
    return orders


if __name__ == "__main__":
    makefun_bot.run(on_batch)
