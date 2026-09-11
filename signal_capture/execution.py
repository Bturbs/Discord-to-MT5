from dataclasses import asdict
import math
import time

from .risk import size_volume


class Rejected(ValueError):
    """No order was sent; it is safe to release the account gate."""


class Uncertain(RuntimeError):
    """An order may have executed. Retain the gate and never retry automatically."""


def finite_positive(value, name):
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise Rejected(f"Invalid {name}")
    return value


class Executor:
    def __init__(self, mt5, config, login, server):
        self.mt5, self.config = mt5, config
        self.login, self.server = login, server

    def account(self):
        m = self.mt5
        account, terminal = m.account_info(), m.terminal_info()
        if account is None or terminal is None or not terminal.connected:
            raise Rejected("MT5 is disconnected")
        if account.login != self.login or account.server != self.server:
            raise Rejected("MT5 account identity differs from configured account")
        if not self.config.dry_run:
            if not self.config.allow_real_account and account.trade_mode != m.ACCOUNT_TRADE_MODE_DEMO:
                raise Rejected("Only demo accounts are enabled")
            if not terminal.trade_allowed or terminal.tradeapi_disabled or not account.trade_allowed or not account.trade_expert:
                raise Rejected("Automated trading is disabled in the account or terminal")
        finite_positive(account.equity, "equity")
        return account

    def tick(self, symbol):
        tick = self.mt5.symbol_info_tick(symbol)
        if tick is None:
            raise Rejected("No quote")
        finite_positive(tick.ask, "ask")
        finite_positive(tick.bid, "bid")
        age = time.time() - tick.time_msc / 1000
        if tick.ask < tick.bid or not math.isfinite(age) or not -2 <= age <= self.config.max_tick_age_seconds:
            raise Rejected("Stale or invalid quote")
        return tick

    def plan(self, signal, channel, message_id):
        m = self.mt5
        account = self.account()
        symbol = channel.symbol
        if not m.symbol_select(symbol, True):
            raise Rejected("Broker symbol is unavailable")
        info = m.symbol_info(symbol)
        if info is None:
            raise Rejected("No symbol details")
        # Explicitly handle direction-only, close-only and disabled symbols.
        modes = (m.SYMBOL_TRADE_MODE_FULL, m.SYMBOL_TRADE_MODE_LONGONLY if signal.side == "LONG" else m.SYMBOL_TRADE_MODE_SHORTONLY)
        if info.trade_mode not in modes:
            raise Rejected("Symbol does not allow this trade direction")
        for name in ("point", "trade_tick_size", "volume_min", "volume_max", "volume_step"):
            finite_positive(getattr(info, name), name)
        for value in (signal.entry, signal.stop, signal.target):
            steps = value / info.trade_tick_size
            if not math.isclose(steps, round(steps), abs_tol=1e-6, rel_tol=0):
                raise Rejected("Signal price does not match the broker tick size")
        positions, orders = m.positions_get(), m.orders_get()
        if positions is None or orders is None:
            raise Rejected("Could not verify existing exposure")
        if len(positions) + len(orders) >= self.config.max_positions:
            raise Rejected("Account exposure count limit reached")
        if any(p.symbol == symbol for p in (*positions, *orders)):
            raise Rejected("Existing exposure on symbol; avoid netting or conflicting orders")
        tick = self.tick(symbol)
        price = tick.ask if signal.side == "LONG" else tick.bid
        if (tick.ask - tick.bid) / info.point > channel.max_spread_points + 1e-7:
            raise Rejected("Spread exceeds configured limit")
        if abs(price - signal.entry) / info.point > channel.max_entry_drift_points + 1e-7:
            raise Rejected("Market price has moved beyond the signal entry tolerance")
        stop_distance = info.trade_stops_level * info.point
        # Stops are triggered by bid for buys, ask for sells.
        if signal.side == "LONG":
            valid = signal.stop < tick.bid and signal.target > tick.ask and tick.bid - signal.stop >= stop_distance and signal.target - tick.bid >= stop_distance
        else:
            valid = signal.target < tick.bid and signal.stop > tick.ask and signal.stop - tick.ask >= stop_distance and tick.ask - signal.target >= stop_distance
        if not valid:
            raise Rejected("Stops are invalid at the current market price")
        side = m.ORDER_TYPE_BUY if signal.side == "LONG" else m.ORDER_TYPE_SELL
        adverse = channel.deviation_points * info.point
        worst = price + adverse if signal.side == "LONG" else price - adverse
        finite_positive(worst, "sizing entry")
        probe = info.volume_min
        profit = m.order_calc_profit(side, symbol, probe, worst, signal.stop)
        if profit is None or not math.isfinite(profit) or profit >= 0:
            raise Rejected("Broker could not calculate stop-loss cost")
        loss_per_lot = abs(profit) / probe + channel.commission_per_lot
        budget = account.equity * self.config.risk_percent / 100
        try:
            volume = size_volume(budget, loss_per_lot, info.volume_min, info.volume_max, info.volume_step)
        except ValueError as exc:
            raise Rejected(str(exc)) from exc
        # Recalculate the final quantity through MT5 before trusting the estimate.
        final_loss = m.order_calc_profit(side, symbol, volume, worst, signal.stop)
        if final_loss is None or not math.isfinite(final_loss) or final_loss >= 0:
            raise Rejected("Final volume loss calculation failed")
        estimated_loss = abs(final_loss) + channel.commission_per_lot * volume
        if estimated_loss > budget + 1e-8:
            raise Rejected("Rounded volume exceeds risk budget")
        margin = m.order_calc_margin(side, symbol, volume, worst)
        if margin is None or not math.isfinite(margin) or margin < 0 or not math.isfinite(account.margin_free) or margin > account.margin_free:
            raise Rejected("Insufficient or unknown free margin")
        # SYMBOL_FILLING_* is a bitmask; ORDER_FILLING_* is a different enum.
        if info.filling_mode & 1:
            filling = m.ORDER_FILLING_FOK
        elif info.filling_mode & 2:
            filling = m.ORDER_FILLING_IOC
        elif info.trade_exemode != m.SYMBOL_TRADE_EXECUTION_MARKET:
            filling = m.ORDER_FILLING_RETURN
        else:
            raise Rejected("No supported market filling policy")
        request = {"action": m.TRADE_ACTION_DEAL, "symbol": symbol, "volume": volume,
                   "type": side, "price": price, "sl": signal.stop, "tp": signal.target,
                   "deviation": channel.deviation_points, "magic": self.config.magic,
                   "comment": f"dc:{message_id}", "type_time": m.ORDER_TIME_GTC,
                   "type_filling": filling}
        check = m.order_check(request)
        if check is None or check.retcode != 0:
            raise Rejected(f"MT5 order_check failed: {None if check is None else check.retcode}")
        return {"request": request, "risk_budget": budget, "estimated_loss": estimated_loss,
                "account_currency": account.currency, "worst_entry": worst, "signal": asdict(signal),
                "point": info.point, "stop_distance": stop_distance}

    def send(self, plan, channel):
        account = self.account()
        tick = self.tick(channel.symbol)
        req = plan["request"]
        buy = req["type"] == self.mt5.ORDER_TYPE_BUY
        price = tick.ask if buy else tick.bid
        point = plan["point"]
        if plan["estimated_loss"] > account.equity * self.config.risk_percent / 100 + 1e-8:
            raise Rejected("Equity changed; planned loss exceeds the current budget")
        if (tick.ask - tick.bid) / point > channel.max_spread_points + 1e-7:
            raise Rejected("Spread widened during validation")
        if abs(price - plan["signal"]["entry"]) / point > channel.max_entry_drift_points + 1e-7:
            raise Rejected("Entry drift changed during validation")
        distance = plan["stop_distance"]
        if buy:
            valid = req["sl"] < tick.bid and req["tp"] > tick.ask and tick.bid - req["sl"] >= distance and req["tp"] - tick.bid >= distance
        else:
            valid = req["tp"] < tick.bid and req["sl"] > tick.ask and req["sl"] - tick.ask >= distance and tick.ask - req["tp"] >= distance
        if not valid:
            raise Rejected("Stops became invalid during validation")
        if (buy and price > plan["worst_entry"]) or (not buy and price < plan["worst_entry"]):
            raise Rejected("Quote moved past the price used for risk sizing")
        # The durable SUBMITTING record is written by the service before this call.
        try:
            result = self.mt5.order_send(req)
        except Exception as exc:
            raise Uncertain("MT5 submission raised an exception; reconcile broker history") from exc
        if result is None:
            raise Uncertain("MT5 returned no result; reconcile broker history")
        if result.retcode not in (self.mt5.TRADE_RETCODE_DONE, self.mt5.TRADE_RETCODE_DONE_PARTIAL):
            # Even a timeout/connection error must not trigger a second send.
            raise Uncertain(f"MT5 retcode {result.retcode}; reconcile orders, deals and positions")
        return {"retcode": result.retcode, "order": result.order, "deal": result.deal,
                "volume": result.volume, "price": result.price,
                "partial": result.retcode == self.mt5.TRADE_RETCODE_DONE_PARTIAL}
