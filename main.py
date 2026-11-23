"""A cbBTC trend bot for the make.fun btc-race-24h-20261123-0511 bounty.

The bot speaks agent-fun/bars-replay/v1 through the vendored kit shim
(vendor/makefun_bars.py): hello once, then one batch per 1-minute bar, and
this bot answers every batch with exactly one reply echoing its seq, carrying
at most one order. Orders decided on a batch fill at the next bar's open, so
every signal here reads closes only, never the future.

Strategy (one mode, long only, full size, one open position):

A slow grind up is how BTC actually moves on this replay: the dev window
rose 363 bps in 24 hours, almost all of it in one long leg, then chopped
sideways for 19 hours. So the bot waits for a genuine uptrend and then rides
it, instead of trading every wiggle:

- Entry: the fast EMA (12) sits above the slow EMA (48) and is still
  rising over the last 10 bars, the close prints a fresh 30-bar high, and
  the 30-bar return is at least +20 bps. All four must hold at once, which
  keeps the bot in cash through flat and falling stretches.
- Exit: a hard stop 150 bps under the fill price caps every round trip; a
  trailing stop 200 bps under the peak since entry banks the leg when the
  move turns; and once the trade is up 250 bps the trail latches tighter
  to 110 bps, so a big leg is not given back to a slow bleed. There is no
  fixed take-profit: a hard take level is a bet on one window's exact top,
  and on this dev window the take level that looked best (400 bps) was a
  cliff, 375 to 400 bps good and 425 bps worthless, so the latched trail
  does that job smoothly instead. Max hold is 1440 bars, a safety valve
  that never binds inside a 1440-bar window.
- After any exit the bot waits 60 bars before a new entry, so it never
  re-buys the top of the same leg it just sold.

What was tried and rejected on this dev window: a dip-buying second mode
lost about $11 of PnL against the trend-only bot (its dip trades fired in
the sideways chop and bled fees), and an exit on the fast EMA crossing
back under the slow EMA whipsawed the bot out of the big leg early. Both
are left out. A plain buy-and-hold makes $32.17 on the dev window; this
bot's edge is that it keeps most of a leg while its stops cap the loss
when a window falls instead: on the previous round's down dev window
(-281 bps) it lost $4.11 where buy-and-hold lost $31.63.

Deterministic: pure integer arithmetic on the closes the harness sends, no
randomness, no wall clock, no network, no file reads outside this repo.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# --- trend entry: EMA pair, slope, fresh high, short momentum ---
EMA_FAST = 12           # fast EMA length in bars
EMA_SLOW = 48           # slow EMA length in bars
SLOPE_BARS = 10         # fast EMA must be higher than this many bars ago
BRK_WIN = 30            # close must beat the high of the previous N bars
RET30_MIN_BPS = 20      # 30-bar return to enter

# --- exits ---
TREND_STOP_BPS = 150    # hard stop under the fill price
TRAIL_BPS = 200         # trailing stop under the peak since entry
RATCHET_AT_BPS = 250    # gain at which the trail latches tighter
TRAIL_TIGHT_BPS = 110   # latched trailing stop under the peak
TREND_HOLD = 1440       # max bars in a trade (never binds in-window)
COOLDOWN = 60           # bars to wait after any exit before re-entering

MAX_SLIPPAGE_BPS = 100  # worst 1-minute close-to-open move on dev was 19 bps
MIN_NOTIONAL = 1000000  # 1 USDC, the harness minimum for a buy


def _bps_gain(new, old):
    """(new - old) / old in basis points, integer math, old > 0."""
    return (new - old) * 10000 // old


class Bot:
    """One bot instance: close history, EMA state, and the open trade."""

    def __init__(self):
        self.closes = []        # closes, oldest first, ints at price_scale 5
        self.fast_hist = []     # fast EMA value after each bar
        self.ema_fast = None    # running fast EMA, seeded at the first close
        self.ema_slow = None    # running slow EMA, seeded at the first close
        self.entry_price = None
        self.entry_bar = None
        self.peak = None        # highest close since entry
        self.armed = False      # True once the trade is up RATCHET_AT_BPS
        self.cool_until = -1    # bar index before which no new entry
        self.order_no = 0

    # -- orders ---------------------------------------------------------
    def _buy(self, state):
        self.order_no += 1
        return [{"side": "buy", "client_id": "b%d" % self.order_no,
                 "symbol": SYMBOL,
                 "notional_quote_units": state.cash,
                 "max_slippage_bps": MAX_SLIPPAGE_BPS}]

    def _sell(self, state):
        holding = state.positions.get(SYMBOL)
        if holding is None or holding.qty <= 0:
            return []
        self.order_no += 1
        self.cool_until = len(self.closes) - 1 + COOLDOWN
        return [{"side": "sell", "client_id": "s%d" % self.order_no,
                 "symbol": SYMBOL, "qty": holding.qty,
                 "max_slippage_bps": MAX_SLIPPAGE_BPS}]

    def _update_ema(self, close):
        """Fold one close into both running EMAs (integer alpha 2/(n+1))."""
        if self.ema_fast is None:
            self.ema_fast = close
            self.ema_slow = close
        else:
            self.ema_fast += (close - self.ema_fast) * 2 // (EMA_FAST + 1)
            self.ema_slow += (close - self.ema_slow) * 2 // (EMA_SLOW + 1)
        self.fast_hist.append(self.ema_fast)

    # -- one batch ------------------------------------------------------
    def on_batch(self, state, batch):
        # Fills first: a batch can carry fills even on a minute with no bar
        # for our symbol, and a fill seen nowhere else must not be missed.
        for fill in batch["fills"]:
            if fill["status"] != "filled":
                continue
            if fill["side"] == "buy":
                self.entry_price = int(fill["price"])
                # The fill lands at this batch's bar, which is appended next,
                # so its index is the length of the history as it stands.
                self.entry_bar = len(self.closes)
                self.peak = self.entry_price
                self.armed = False
            elif fill["side"] == "sell":
                self.entry_price = None
                self.entry_bar = None
                self.peak = None
                self.armed = False

        # Keep the close history: one entry per bar the harness sent us.
        bar_here = False
        for bar in batch["bars"]:
            if bar["symbol"] == SYMBOL:
                self.closes.append(int(bar["c"]))
                self._update_ema(int(bar["c"]))
                bar_here = True
        if not bar_here:
            return []
        idx = len(self.closes) - 1
        close = self.closes[idx]

        # The account is the truth about what is held; trust it over memory.
        holding = state.positions.get(SYMBOL)
        have_position = holding is not None and holding.qty > 0
        if not have_position:
            self.entry_price = None
            self.entry_bar = None
            self.peak = None
            self.armed = False

        if have_position:
            return self._maybe_exit(state, idx, close)
        return self._maybe_enter(state, idx, close)

    def _maybe_exit(self, state, idx, close):
        if self.entry_price is None:
            return []  # position open but its fill has not been reported yet
        if close > self.peak:
            self.peak = close
        gain = _bps_gain(close, self.entry_price)
        if gain <= -TREND_STOP_BPS:
            return self._sell(state)
        if gain >= RATCHET_AT_BPS:
            self.armed = True
        trail = TRAIL_TIGHT_BPS if self.armed else TRAIL_BPS
        if _bps_gain(self.peak, close) >= trail:
            return self._sell(state)
        if self.entry_bar is not None and idx - self.entry_bar >= TREND_HOLD:
            return self._sell(state)
        return []

    def _maybe_enter(self, state, idx, close):
        if state.warming_up:
            return []
        if idx < EMA_SLOW + 5 or idx < self.cool_until:
            return []
        if state.cash < MIN_NOTIONAL:
            return []
        if not self._trend_entry(idx, close):
            return []
        return self._buy(state)

    def _trend_entry(self, idx, close):
        # Fast EMA above slow EMA and still rising.
        if self.ema_fast <= self.ema_slow:
            return False
        if idx >= SLOPE_BARS and self.fast_hist[idx] <= self.fast_hist[idx - SLOPE_BARS]:
            return False
        # Fresh high: close above every close of the previous BRK_WIN bars.
        if idx < BRK_WIN:
            return False
        if close <= max(self.closes[idx - BRK_WIN:idx]):
            return False
        # Short momentum confirms the leg is real, not a one-bar spike.
        if _bps_gain(close, self.closes[idx - 30]) < RET30_MIN_BPS:
            return False
        return True


def main():
    bot = Bot()
    makefun_bars.run(bot.on_batch)


if __name__ == "__main__":
    main()
