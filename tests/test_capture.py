from dataclasses import replace
from types import SimpleNamespace as NS
import time

import pytest

from signal_capture.config import Channel, Config, SOURCE_BOT_ID, load_config
from signal_capture.execution import Executor, Rejected, Uncertain
from signal_capture.ledger import SQLiteLedger, Busy
from signal_capture.parser import parse_signal, extract_text
from signal_capture.risk import size_volume
from signal_capture.service import Service, Event

SAMPLE = "LONG entry 29335.25\n🎯 29455.75\n🛑 29264.50"
CHANNEL = Channel("MNQ_BROKER", 20, 30, 10, 2)
CFG = Config({123: CHANNEL})


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    SYMBOL_TRADE_MODE_FULL = 4
    SYMBOL_TRADE_MODE_LONGONLY = 1
    SYMBOL_TRADE_MODE_SHORTONLY = 2
    SYMBOL_TRADE_EXECUTION_MARKET = 2
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    TRADE_ACTION_DEAL = 1
    ORDER_TIME_GTC = 0
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_DONE_PARTIAL = 10010

    def __init__(self):
        self.account_data = NS(login=42, server="Demo", equity=100000, margin_free=50000,
                               currency="USD", trade_mode=0, trade_allowed=True, trade_expert=True)
        self.terminal = NS(connected=True, trade_allowed=True, tradeapi_disabled=False)
        self.info = NS(point=0.01, trade_tick_size=0.25, volume_min=1, volume_max=100,
                       volume_step=1, trade_stops_level=0, filling_mode=1, trade_exemode=2, trade_mode=4)
        self.quote = NS(ask=29335.25, bid=29335.0, time_msc=time.time() * 1000)
        self.positions, self.orders, self.sent = [], [], []
        self.result = NS(retcode=10009, order=111, deal=222, volume=3, price=29335.25)
        self.check_code = 0

    def account_info(self): return self.account_data
    def terminal_info(self): return self.terminal
    def symbol_select(self, *_): return True
    def symbol_info(self, *_): return self.info
    def symbol_info_tick(self, *_): return self.quote
    def positions_get(self): return self.positions
    def orders_get(self): return self.orders
    def order_calc_profit(self, side, symbol, volume, entry, stop):
        return (stop - entry) * (1 if side == self.ORDER_TYPE_BUY else -1) * volume * 2
    def order_calc_margin(self, side, symbol, volume, price): return 1000 * volume
    def order_check(self, request): return NS(retcode=self.check_code)
    def order_send(self, request):
        self.sent.append(request.copy())
        return self.result


def make_service(tmp_path, cfg=CFG, mt5=None):
    mt5 = mt5 or FakeMT5()
    ledger = SQLiteLedger(tmp_path / "audit.db")
    return Service(cfg, Executor(mt5, cfg, 42, "Demo"), ledger), mt5


def event(**overrides):
    return replace(Event("1234567890123456789", SOURCE_BOT_ID, 123, time.time(), SAMPLE), **overrides)


def test_exact_user_sample():
    signal = parse_signal(SAMPLE)
    assert (signal.side, signal.entry, signal.target, signal.stop) == ("LONG", 29335.25, 29455.75, 29264.5)


def test_short_and_discord_bold():
    s = parse_signal("**SHORT entry 29335.25**\r\n🎯 29264.50\r\n🛑 29455.75")
    assert s.side == "SHORT"


@pytest.mark.parametrize("text", [SAMPLE + "\nmove stop", SAMPLE + "\n🎯 29500", SAMPLE + "\n" + SAMPLE,
                                 SAMPLE.replace("29264.50", "29460"), SAMPLE.replace("29455.75", "29000"),
                                 SAMPLE.replace("29335.25", "NaN"), SAMPLE.replace("29335.25", "29330-29340"),
                                 "CANCEL " + SAMPLE, "LONG entry 0\n🎯 1\n🛑 0", "", "LONG entry 100"])
def test_ambiguous_or_invalid_messages_rejected(text):
    with pytest.raises(ValueError): parse_signal(text)


def test_embed_fields_extract():
    msg = NS(content="", embeds=[NS(title="LONG entry 29335.25", description=None,
                                  fields=[NS(name="🎯", value="29455.75"), NS(name="🛑", value="29264.50")])])
    assert parse_signal(extract_text(msg)) == parse_signal(SAMPLE)


@pytest.mark.parametrize("budget,loss,minimum,maximum,step,expected", [
    (500, 143.7, 1, 100, 1, 3), (100, 300, .01, 100, .01, .33),
    (100, 30, .25, 100, .25, 3.25), (10000, 10, 1, 5, 1, 5),
    (.3, 1, .1, 10, .1, .3)])
def test_round_down(budget, loss, minimum, maximum, step, expected):
    result = size_volume(budget, loss, minimum, maximum, step)
    assert result == expected
    assert result * loss <= budget + 1e-8


@pytest.mark.parametrize("args", [(50, 143.7, 1, 100, 1), (100, 0, 1, 100, 1),
                                  (100, float("nan"), 1, 100, 1), (100, 1, 1, 100, 0)])
def test_unsafe_volume_rejected(args):
    with pytest.raises(ValueError): size_volume(*args)


def test_dry_run_and_durable_duplicate(tmp_path):
    service, mt5 = make_service(tmp_path)
    ev = event()
    assert service.process(ev) == "DRY_RUN"
    assert not mt5.sent
    service2, _ = make_service(tmp_path)
    assert service2.process(ev) == "duplicate"
    record = service.ledger.get(f"msg-{ev.message_id}")
    assert record["plan"]["request"]["symbol"] == "MNQ_BROKER"
    assert record["plan"]["request"]["volume"] == 3
    assert record["plan"]["estimated_loss"] <= 500
    assert service.ledger.get("execution-gate") is None


@pytest.mark.parametrize("overrides", [{"author_id": 9}, {"channel_id": 9}, {"webhook": True}])
def test_source_scope(tmp_path, overrides):
    service, mt5 = make_service(tmp_path)
    ev = event(**overrides)
    assert service.process(ev) == "ignored"
    assert service.ledger.get(f"msg-{ev.message_id}") is None
    assert not mt5.sent


@pytest.mark.parametrize("age", [120, -30])
def test_stale_and_future_messages(tmp_path, age):
    service, mt5 = make_service(tmp_path)
    assert service.process(event(created_at=time.time() - age)) == "REJECTED"
    assert not mt5.sent


def test_send_is_claimed_before_execution(tmp_path):
    service, mt5 = make_service(tmp_path, replace(CFG, dry_run=False))
    original = mt5.order_send
    ev = event()
    def send(request):
        assert service.ledger.get(f"msg-{ev.message_id}")["status"] == "SUBMITTING"
        assert service.ledger.get("execution-gate") is not None
        return original(request)
    mt5.order_send = send
    assert service.process(ev) == "EXECUTED"
    assert service.process(ev) == "duplicate"
    assert len(mt5.sent) == 1


@pytest.mark.parametrize("retcode", [None, 10012, 10008, 10030])
def test_uncertain_submission_blocks_following_orders(tmp_path, retcode):
    service, mt5 = make_service(tmp_path, replace(CFG, dry_run=False))
    mt5.result = None if retcode is None else NS(retcode=retcode)
    ev = event()
    with pytest.raises(Uncertain): service.process(ev)
    assert service.process(ev) == "duplicate"
    assert service.ledger.get(f"msg-{ev.message_id}")["status"] == "SUBMITTING"
    with pytest.raises(Busy): service.process(event(message_id="next"))
    assert len(mt5.sent) == 1


def test_audit_failure_before_send_never_submits(tmp_path):
    service, mt5 = make_service(tmp_path, replace(CFG, dry_run=False))
    def fail(*_): raise OSError("storage unavailable")
    service.ledger.put = fail
    with pytest.raises(OSError): service.process(event())
    assert not mt5.sent
    assert service.ledger.get("execution-gate")


def test_audit_failure_after_send_is_never_retried(tmp_path):
    service, mt5 = make_service(tmp_path, replace(CFG, dry_run=False))
    original = service.ledger.put
    def fail(key, value):
        if value["status"] == "EXECUTED": raise OSError("storage unavailable")
        original(key, value)
    service.ledger.put = fail
    ev = event()
    with pytest.raises(OSError): service.process(ev)
    restarted, _ = make_service(tmp_path, replace(CFG, dry_run=False), mt5)
    assert restarted.process(ev) == "duplicate"
    with pytest.raises(Busy): restarted.process(event(message_id="next"))
    assert len(mt5.sent) == 1


@pytest.mark.parametrize("mutation", [
    lambda m: setattr(m.account_data, "login", 99),
    lambda m: setattr(m.account_data, "server", "Other"),
    lambda m: setattr(m.account_data, "trade_mode", 2),
    lambda m: setattr(m.account_data, "equity", float("nan")),
    lambda m: setattr(m.account_data, "margin_free", 0),
    lambda m: setattr(m.terminal, "tradeapi_disabled", True),
    lambda m: setattr(m.terminal, "connected", False),
    lambda m: setattr(m.quote, "time_msc", 0),
    lambda m: setattr(m.quote, "ask", 29336.25),
    lambda m: setattr(m.quote, "bid", 29330),
    lambda m: setattr(m, "positions", None),
    lambda m: setattr(m, "orders", [NS(symbol="MNQ_BROKER")]),
    lambda m: setattr(m.info, "trade_mode", 0),
    lambda m: setattr(m.info, "trade_tick_size", 1),
    lambda m: setattr(m.info, "filling_mode", 0),
    lambda m: setattr(m, "check_code", 10019),
    lambda m: setattr(m, "order_calc_profit", lambda *_: None),
])
def test_broker_checks_fail_closed(tmp_path, mutation):
    service, mt5 = make_service(tmp_path, replace(CFG, dry_run=False))
    mutation(mt5)
    assert service.process(event()) == "REJECTED"
    assert not mt5.sent
    assert service.ledger.get("execution-gate") is None


def test_partial_fill_is_not_topped_up(tmp_path):
    service, mt5 = make_service(tmp_path, replace(CFG, dry_run=False))
    mt5.result.retcode = 10010
    mt5.result.volume = 1
    ev = event()
    assert service.process(ev) == "EXECUTED"
    assert service.ledger.get(f"msg-{ev.message_id}")["result"]["partial"]
    assert len(mt5.sent) == 1


def test_ioc_mask_is_not_order_enum(tmp_path):
    service, mt5 = make_service(tmp_path)
    mt5.info.filling_mode = 2
    ev = event()
    assert service.process(ev) == "DRY_RUN"
    assert service.ledger.get(f"msg-{ev.message_id}")["plan"]["request"]["type_filling"] == mt5.ORDER_FILLING_IOC


def test_example_requires_channel_and_symbol(tmp_path):
    from pathlib import Path
    text = (Path(__file__).parents[1] / "config.example.toml").read_text()
    config = tmp_path / "config.toml"
    config.write_text(text)
    with pytest.raises(ValueError): load_config(config)
    config.write_text(text.replace("REPLACE_WITH_DISCORD_CHANNEL_ID", "123").replace("REPLACE_WITH_EXACT_BROKER_SYMBOL", "MNQU26"))
    assert load_config(config).channels[123].symbol == "MNQU26"


@pytest.mark.parametrize("replacement", ["nan", "-1", "101", "true"])
def test_invalid_risk_config(tmp_path, replacement):
    config = tmp_path / "config.toml"
    config.write_text(f'risk_percent = {replacement}\n[channels."123"]\nsymbol="MNQ"\nmax_entry_drift_points=20\nmax_spread_points=30\ndeviation_points=10\n')
    with pytest.raises(ValueError): load_config(config)


@pytest.mark.parametrize("mutation", [
    lambda m: setattr(m.account_data, "equity", 1),
    lambda m: setattr(m.quote, "bid", 29330),
    lambda m: setattr(m.quote, "ask", 29335.5),
    lambda m: setattr(m.quote, "time_msc", 0),
])
def test_changed_market_after_plan_cannot_submit(tmp_path, mutation):
    service, mt5 = make_service(tmp_path, replace(CFG, dry_run=False))
    plan = service.executor.plan(parse_signal(SAMPLE), CHANNEL, "123")
    mutation(mt5)
    with pytest.raises(Rejected): service.executor.send(plan, CHANNEL)
    assert not mt5.sent


def test_independent_connections_cannot_hold_account_gate_together(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from signal_capture.ledger import account_gate
    path = tmp_path / "audit.db"
    SQLiteLedger(path)
    barrier = threading.Barrier(2)
    def acquire(message):
        ledger = SQLiteLedger(path)
        barrier.wait(timeout=5)
        try:
            with account_gate(ledger, message):
                # Simulate a crash: winner leaves the durable gate behind.
                raise RuntimeError("crash")
        except Busy:
            return "blocked"
        except RuntimeError:
            return "claimed"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(acquire, ["one", "two"]))
    assert sorted(results) == ["blocked", "claimed"]


def test_short_uses_bid_and_short_loss_calculation(tmp_path):
    service, mt5 = make_service(tmp_path, replace(CFG, dry_run=False))
    mt5.quote.bid = 29335.25
    mt5.quote.ask = 29335.5
    ev = event(text="SHORT entry 29335.25\n🎯 29264.50\n🛑 29455.75")
    assert service.process(ev) == "EXECUTED"
    assert mt5.sent[0]["type"] == mt5.ORDER_TYPE_SELL
    assert mt5.sent[0]["price"] == mt5.quote.bid
    assert mt5.sent[0]["volume"] == 2
