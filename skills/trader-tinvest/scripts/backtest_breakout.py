#!/usr/bin/env python3
"""Offline one-share cash-only breakout simulation, not a broker/executor.

Input: instrument={uid,kind:"share",currency,lot_size,price_tick,cash_tick};
dataset={kind:"synthetic"|"historical_unverified",source,as_of,
  corporate_actions:"none_declared",constant_lot_and_tick:true};
config={initial_cash,entry_lookback,exit_lookback,max_hold_bars,
  commission_bps,half_spread_bps,slippage_bps,max_participation,
  settlement_lag_bars}; holdout_start = exact bar open_time string;
bars=[{open_time,close_time,open,high,low,close,volume_lots,is_complete:true}].
Prices, cash, bps and fractions are plain decimal strings. Counts are positive
JSON integers (volume can be zero). Times must include timezone. See the full
research contract and limitations in ../references/backtest-breakout.md.

Signal at close[i]: enter if close > max(high[i-N:i]); exit if close <
min(low[i-M:i]) or holding age >= max_hold_bars (entry bar counts as 1).
Simulated fills use NEXT open with adverse spread/slippage and tick rounding.
Volume cap uses only PREVIOUS bar volume. BUY attempts once, unfilled remainder
cancelled; SELL persists until fully filled. Sale proceeds settle only after
the configured positive number of bars. Open/unsettled assets remain in equity.
There is no forced final close or fill of a last-bar signal.

Development and holdout start flat with independent copies of initial cash.
Preceding bars are indicator warmup only; no pending signal crosses the split.
Each period has cash and cost-matched buy-once-and-hold benchmarks. Data and
holdout independence remain UNVERIFIED; no profitability is established.
No network, credentials, optimization, real orders or validator-ready outputs.
"""
import argparse
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
import hashlib
import json
import re


STRATEGY = "cash-share-breakout-v1"
MAX_BARS = 10000


def require(condition, message):
    if not condition:
        raise ValueError(message)


def obj(value, name):
    require(isinstance(value, dict), name + " must be an object")
    return value


def text(value, name):
    require(isinstance(value, str) and bool(value.strip()), name + " is missing")
    return value


def number(value, name, positive=False):
    require(isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}(?:\.[0-9]{1,9})?", value) is not None,
            name + " must be a bounded nonnegative plain decimal string")
    result = Decimal(value)
    require(not positive or result > 0, name + " must be positive")
    return result


def integer(value, name, minimum=1, maximum=10000):
    require(type(value) is int and minimum <= value <= maximum, name + " is outside integer bounds")
    return value


def timestamp(value, name):
    value = text(value, name)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(name + " must be ISO time") from None
    require(result.tzinfo is not None and result.utcoffset() is not None, name + " must include timezone")
    return result


def display(value):
    return format(value, "f")


def fraction(value):
    return format(value.quantize(Decimal("0.000000001")), "f")


def rounded(value, tick, upward):
    return (value / tick).to_integral_value(rounding=ROUND_CEILING if upward else ROUND_FLOOR) * tick


def parse(payload):
    payload = obj(payload, "input")
    instrument, dataset, config = (obj(payload.get(key), key) for key in ("instrument", "dataset", "config"))
    text(instrument.get("uid"), "instrument UID")
    require(instrument.get("kind") == "share", "only a single share instrument is supported")
    require(isinstance(instrument.get("currency"), str) and
            re.fullmatch(r"[a-z]{3}", instrument["currency"]) is not None, "currency must be lowercase code")
    lot = integer(instrument.get("lot_size"), "lot_size", maximum=2 ** 31 - 1)
    tick = number(instrument.get("price_tick"), "price_tick", positive=True)
    cash_tick = number(instrument.get("cash_tick"), "cash_tick", positive=True)
    require(dataset.get("kind") in ("synthetic", "historical_unverified"), "unsupported dataset kind")
    text(dataset.get("source"), "dataset source")
    as_of = timestamp(dataset.get("as_of"), "dataset as_of")
    require(dataset.get("corporate_actions") == "none_declared" and
            dataset.get("constant_lot_and_tick") is True, "corporate actions or changing lot/tick are unsupported")
    cfg = {key: integer(config.get(key), key, maximum=1000) for key in
           ("entry_lookback", "exit_lookback", "max_hold_bars", "settlement_lag_bars")}
    cfg["initial_cash"] = number(config.get("initial_cash"), "initial_cash", positive=True)
    for key in ("commission_bps", "half_spread_bps", "slippage_bps"):
        cfg[key] = number(config.get(key), key)
        require(cfg[key] <= 1000, key + " exceeds the model bound")
    cfg["max_participation"] = number(config.get("max_participation"), "max_participation", positive=True)
    require(cfg["max_participation"] <= 1, "max_participation must not exceed one")
    raw_bars = payload.get("bars")
    require(isinstance(raw_bars, list) and 4 <= len(raw_bars) <= MAX_BARS, "bars must be a bounded nonempty history")
    bars, previous_end = [], None
    for record in raw_bars:
        record = obj(record, "bar")
        start, end = timestamp(record.get("open_time"), "open_time"), timestamp(record.get("close_time"), "close_time")
        require(start < end <= as_of and (previous_end is None or start >= previous_end),
                "bars are future-dated, overlapping, duplicate or unordered")
        previous_end = end
        require(record.get("is_complete") is True, "incomplete candles are unsupported")
        prices = {key: number(record.get(key), key, positive=True) for key in ("open", "high", "low", "close")}
        require(prices["low"] <= min(prices["open"], prices["close"]) and
                prices["high"] >= max(prices["open"], prices["close"]), "inconsistent OHLC")
        require(all(value % tick == 0 for value in prices.values()), "bar prices do not match constant tick")
        volume = integer(record.get("volume_lots"), "volume_lots", minimum=0, maximum=2 ** 63 - 1)
        bars.append(dict(prices, open_time=start, close_time=end, volume_lots=volume))
    split_time = timestamp(payload.get("holdout_start"), "holdout_start")
    matches = [i for i, bar in enumerate(bars) if bar["open_time"] == split_time]
    require(len(matches) == 1, "holdout_start must match one bar open time")
    split = matches[0]
    warmup = max(cfg["entry_lookback"], cfg["exit_lookback"])
    require(warmup + 2 <= split <= len(bars) - 2, "both periods need warmup and at least two evaluated bars")
    return instrument, dataset, cfg, bars, warmup, split, lot, tick, cash_tick


def simulate(bars, start, end, cfg, lot, tick, cash_tick, buy_hold=False):
    cash, lots = cfg["initial_cash"], 0
    unsettled, fills, curve, signals, closed, skips = [], [], [], [], [], []
    pending = {"side": "BUY", "reason": "buy_once_hold", "signal_index": None} if buy_hold else None
    entry_index, episode = None, None
    commissions, shortfall, turnover = Decimal(0), Decimal(0), Decimal(0)
    peak, max_dd = cash, Decimal(0)
    fee_rate = cfg["commission_bps"] / 10000
    impact = (cfg["half_spread_bps"] + cfg["slippage_bps"]) / 10000

    def fee(notional):
        return rounded(notional * fee_rate, cash_tick, True)

    for i in range(start, end):
        bar = bars[i]
        cash += sum((amount for release, amount in unsettled if release <= i), Decimal(0))
        unsettled = [(release, amount) for release, amount in unsettled if release > i]
        if pending is not None:
            side = pending["side"]
            price = rounded(bar["open"] * (1 + impact if side == "BUY" else 1 - impact), tick, side == "BUY")
            cap = int((Decimal(bars[i - 1]["volume_lots"]) * cfg["max_participation"]).to_integral_value(rounding=ROUND_FLOOR))
            quantity = min(lots, cap) if side == "SELL" else 0
            if side == "BUY":
                left, right = 0, min(cap, int(cash // (price * lot)))
                while left < right:
                    mid = (left + right + 1) // 2
                    charge = price * lot * mid
                    if charge + fee(charge) <= cash:
                        left = mid
                    else:
                        right = mid - 1
                quantity = left
            if side == "SELL" and quantity and price > 0 and fee(price * lot * quantity) > price * lot * quantity:
                quantity = 0  # Do not manufacture a payable/negative unsettled balance.
            if quantity and price > 0:
                notional = price * lot * quantity
                commission = fee(notional)
                if side == "BUY":
                    cash -= notional + commission
                    lots = quantity
                    entry_index = i
                    episode = {"entry_index": i, "entry_lots": quantity, "entry_cost": notional + commission,
                               "net_proceeds": Decimal(0)}
                else:
                    proceeds = notional - commission
                    unsettled.append((i + cfg["settlement_lag_bars"], proceeds))
                    lots -= quantity
                    episode["net_proceeds"] += proceeds
                    if lots == 0:
                        closed.append({"entry_index": episode["entry_index"], "exit_index": i,
                                       "lots": episode["entry_lots"],
                                       "net_pnl": display(episode["net_proceeds"] - episode["entry_cost"])})
                        entry_index, episode = None, None
                commissions += commission
                shortfall += abs(price - bar["open"]) * lot * quantity
                turnover += notional
                fills.append({"bar_index": i, "open_time": bar["open_time"].isoformat(), "side": side,
                              "signal_index": pending["signal_index"], "reason": pending["reason"],
                              "lots": quantity, "units": quantity * lot, "reference_open": display(bar["open"]),
                              "price": display(price), "commission": display(commission), "notional": display(notional)})
            else:
                skips.append({"bar_index": i, "side": side, "reason": "volume_cash_fee_or_price_constraint"})
            if side == "BUY" or lots == 0:
                pending = None
        require(cash >= 0 and lots >= 0, "internal cash/position invariant failed")
        unsettled_cash = sum((amount for _, amount in unsettled), Decimal(0))
        require(unsettled_cash >= 0, "internal unsettled cash invariant failed")
        equity = cash + unsettled_cash + lots * lot * bar["close"]
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
        curve.append({"bar_index": i, "close_time": bar["close_time"].isoformat(), "available_cash": display(cash),
                      "unsettled_proceeds": display(unsettled_cash), "position_lots": lots, "equity": display(equity)})
        if not buy_hold and pending is None:
            if lots:
                exit_low = min(b["low"] for b in bars[i - cfg["exit_lookback"]:i])
                reason = "channel_exit" if bar["close"] < exit_low else (
                    "max_hold" if i - entry_index + 1 >= cfg["max_hold_bars"] else None)
                if reason:
                    pending = {"side": "SELL", "reason": reason, "signal_index": i}
            elif bar["close"] > max(b["high"] for b in bars[i - cfg["entry_lookback"]:i]):
                pending = {"side": "BUY", "reason": "breakout", "signal_index": i}
            if pending is not None:
                signals.append(dict(pending))
    pnl = equity - cfg["initial_cash"]
    return {"start_index": start, "end_index_exclusive": end,
            "initial_cash": display(cfg["initial_cash"]), "final_equity": display(equity),
            "net_pnl_before_tax": display(pnl), "return_fraction": fraction(pnl / cfg["initial_cash"]),
            "gross_pnl_same_fills": display(pnl + commissions + shortfall),
            "commissions": display(commissions), "execution_shortfall": display(shortfall),
            "turnover_notional": display(turnover), "max_close_drawdown_fraction": fraction(max_dd),
            "open_position_lots": lots, "unsettled_proceeds": display(unsettled_cash),
            "pending_signal": pending, "closed_episodes": closed, "fills": fills, "signals": signals,
            "skipped_fills": skips, "equity_curve": curve}


def run(payload):
    """Return an offline simulation, never a claim of strategy validation."""
    try:
        with localcontext() as context:
            context.prec = 100
            instrument, dataset, cfg, bars, start, split, lot, tick, cash_tick = parse(payload)
            params = {"strategy": STRATEGY, "instrument": payload["instrument"], "config": payload["config"],
                      "holdout_start": payload["holdout_start"]}
            digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                                           allow_nan=False).encode("utf-8")).hexdigest()
            periods = {}
            for name, lo, hi in (("development", start, split), ("holdout", split, len(bars))):
                period = simulate(bars, lo, hi, cfg, lot, tick, cash_tick)
                period["benchmarks"] = {"cash_final_equity": display(cfg["initial_cash"]),
                                        "buy_once_hold": simulate(bars, lo, hi, cfg, lot, tick, cash_tick, True)}
                periods[name] = period
            return {"status": "SIMULATED", "strategy": STRATEGY, "strategy_validated": False,
                    "ready_for_validators": False, "holdout_independence_verified": False,
                    "data_kind": dataset["kind"], "source": dataset["source"], "currency": instrument["currency"],
                    "parameters_sha256": digest(params), "input_sha256": digest(payload),
                    "periods": periods,
                    "limitations": ["single_share_constant_lot_tick_no_corporate_actions",
                                    "unverified_history_and_session_completeness", "approximate_next_open_fills",
                                    "previous_volume_is_not_available_opening_liquidity", "bar_based_settlement_assumption",
                                    "no_taxes_dividends_or_interest", "no_live_guards_or_daily_loss_gate",
                                    "no_forced_final_sale_or_proof_of_holdout_independence"]}
    except (ValueError, TypeError, KeyError, ArithmeticError, RecursionError):
        return {"status": "BLOCK", "strategy_validated": False, "ready_for_validators": False,
                "reason": "Invalid, incomplete, inconsistent or unsupported backtest inputs"}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def invalid_constant(_):
    raise ValueError("nonfinite JSON number")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="normalized offline history/config JSON; no credentials")
    args = parser.parse_args()
    try:
        with open(args.input, encoding="utf-8") as stream:
            result = run(json.load(stream, object_pairs_hook=unique_object, parse_constant=invalid_constant))
    except (OSError, ValueError, RecursionError):
        result = run(None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "SIMULATED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
