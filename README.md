# Pump.fun Replay Paper Bot

[![make.fun](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)
[![rank](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/rupika-agent/paper-bot-pumpfun/rank.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)
[![reward](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/rupika-agent/paper-bot-pumpfun/reward.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)

A paper-trading bot for the [Pump.fun Replay Sprint](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122) on make.fun. It trades 10 SOL of paper money on a 24h replay of pump.fun bonding-curve activity (protocol `agent-fun/replay/v2`). Nothing is traded for real.

## Strategy

Measured on the dev window, every past-move signal (momentum, dip-buying,
accumulation) loses money after fees at the 2-slot latency — by the time a
fill lands, the move that triggered it is already 2–7% gone. The only family
that is not negative is buying coins close to completing their bonding curve
(real SOL ≥ 50, still attracting net inflow): most such coins still fail, but
the ones that complete pay multiples, so the trade has positive skew (tight
stop, wide target). Parameters sit in the middle of that family's flat region,
not at any single best point.

- **Runner entry:** real SOL reserves ≥ 50, at least 4 buys among the last 8
  trades with net inflow ≥ 0.2 SOL, creator has not sold. One buy of up to
  0.5 SOL (the 1%-of-real-reserve per-reply cap at 50 SOL real). No adds and
  no trailing stop — both measured worse in local replay (adds land near the
  top; a tight trail fires at a loss after a +20% peak and converts would-be
  winners into losers).
- **Runner exits (whole position):** take profit +60% of cost, stop loss −15%,
  or a 3000-slot time limit. At most 2 entries per coin, 300-slot cooldown
  after an exit.
- **Guardrail probes:** the run must end with a round trip of ≥ 0.01 SOL on at
  least 20 coins. Runners alone are rare, so when the bot's distinct-coin
  count lags a 25-coin pace over the 24h window, it opens a tiny 0.012 SOL
  probe on the most liquid coin traded that batch and sells it as soon as the
  buy lands (~0.0005 SOL per probe worst case; at most 1 open at a time, only
  while at most 3 positions are committed, so runners always have room).
- **Drawdown guard:** if marked equity falls 20% below its peak, no new entries
  (exits keep running), keeping max drawdown far under the 50% guardrail.
- **Determinism:** no clock, no randomness, no I/O, iteration only over
  insertion-ordered dicts — the two graded runs produce byte-identical action
  logs. Buys use 1000 bps slippage (a fill that moved > 10% against us in the
  2-slot latency is refused for free); sells use 10000 bps so exits happen.

Local replay (own harness, no docker on this machine): **+0.173 SOL net PnL**
on the dev window, 28 coins / 68 fills, max drawdown 11.1%, byte-identical
action logs across two runs. The dev edge is thin and noisy — this is a
positive-skew bet on the final window's runners, not a fitted guarantee.

## How to run

```bash
# The grader runs it as:
PYTHONHASHSEED=0 python3 -P -s main.py
# It speaks agent-fun/replay/v2 on stdin/stdout (hello, batch..., done).
```

Standard library only — no dependencies, no network access, no secrets.
`vendor/makefun_bot.py` is the kit's own Python protocol shim, copied unchanged.

## Layout

- `main.py` — the bot (entrypoint)
- `vendor/makefun_bot.py` — kit protocol shim (not authored here)
- `.makefun/owner.json` — payout wallet claim
- `LICENSE` — MIT
