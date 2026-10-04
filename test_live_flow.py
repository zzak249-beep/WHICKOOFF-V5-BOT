"""
Prueba sin red de la gestión LIVE (main.py) contra un mini-exchange en memoria:
apertura con SL adjunto + TP1/TP2, TP1 parcial → SL a breakeven con la cantidad restante, cierre y cálculo de R.

  python test_live_flow.py
"""
import math
import os
import sys
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ.setdefault("MODE", "LIVE")
os.environ.setdefault("CONFIRM_LIVE", "SI")
sys.argv = ["x"]

import config as C  # noqa: E402
import main  # noqa: E402
from notify import Journal  # noqa: E402

C.LIVE = True
C.RISK_PCT, C.LEVERAGE, C.MAX_CONCURRENT, C.MAX_TOTAL_POSITIONS = 0.5, 5, 2, 4
C.FEE_PCT, C.TP1_FRACTION, C.MOVE_SL_TO_BE, C.TRAIL_ATR, C.TIME_STOP_BARS = 0.05, 0.5, True, 0.0, 0
C.THROTTLE_N = 0
SYM = "TEST-USDT"


class FakeEx:
    def __init__(self):
        self.contracts = {SYM: {"cls": "crypto", "cls_label": "cripto", "name": "TEST", "api_open": True, "pp": 2,
                                "qp": 3, "tick": 0.01, "min_qty": 0.001, "min_usdt": 2.0}}
        self.px, self.pos, self.orders, self.n, self.opened, self.cids = 100.0, None, {}, 0, 0, set()

    # mercado / cuenta
    def price(self, s): return self.px
    def balance(self): return 1000.0, 1000.0
    def set_margin_mode(self, *a): pass
    def set_leverage(self, *a): pass
    def hedge_mode(self): return True
    def fmt_qty(self, s, q): return math.floor(q * 1000 + 1e-9) / 1000
    def fmt_px(self, s, p): return round(p, 2)

    def positions(self, symbol=None):
        return [self.pos] if self.pos and abs(float(self.pos["positionAmt"])) > 0 else []

    # órdenes
    def _add(self, kind, long, qty, px):
        self.n += 1
        oid = str(1000 + self.n)
        self.orders[oid] = {"orderId": oid, "type": kind, "side": "SELL" if long else "BUY", "qty": qty,
                            "stopPrice": px, "status": "NEW", "symbol": SYM, "positionSide": "LONG" if long else "SHORT"}
        return oid

    def market_open(self, s, long, qty, cid, stop_loss=None):
        self.opened += 1
        self.cids.add(cid)
        self.pos = {"symbol": s, "positionSide": "LONG" if long else "SHORT", "positionAmt": str(qty), "avgPrice": str(self.px)}
        if stop_loss is not None:
            self._add("STOP_MARKET", long, qty, stop_loss)

    def exit_order(self, s, long, kind, qty, px, client_id=None): return self._add(kind, long, qty, px)
    def market_close(self, s, long, qty): self.pos = None
    def open_orders(self, symbol=None): return [o for o in self.orders.values() if o["status"] == "NEW"]
    def stop_orders(self, s, long): return [o for o in self.open_orders() if o["type"] in ("STOP_MARKET", "STOP")]
    def order_exists(self, s, cid): return cid in self.cids   # solo existe si la orden llegó a entrar

    def cancel(self, s, oid):
        if oid in self.orders and self.orders[oid]["status"] == "NEW":
            self.orders[oid]["status"] = "CANCELED"
        return True

    def get_order(self, s, order_id=None, client_id=None):
        o = self.orders.get(str(order_id))
        return {} if not o else {"status": o["status"], "avgPrice": str(o["stopPrice"])}

    # simulación del mercado: llena una orden y reduce la posición
    def fill(self, oid):
        o = self.orders[oid]
        assert o["status"] == "NEW", f"orden {oid} no estaba viva ({o['status']})"
        o["status"] = "FILLED"
        left = round(float(self.pos["positionAmt"]) - o["qty"], 6)
        self.pos["positionAmt"] = str(max(left, 0.0))
        if left <= 1e-9:
            self.pos = None
            for x in self.open_orders():  # BingX cancela las reducing órdenes al cerrar la posición
                x["status"] = "CANCELED"


def fresh_bot(fake):
    b = object.__new__(main.Bot)
    b.ex, b.attach_ok, b.running = fake, True, True
    sent = []
    b.tg = type("TG", (), {"send": lambda self, t: sent.append(t)})()
    b.journal = Journal(os.environ["DATA_DIR"])
    b.state = {"positions": {}, "sims": {}, "daily": {"day": main.utc_day(), "r": 0.0, "n": 0},
               "stats": {"n": 0, "wins": 0, "sum_r": 0.0, "gw": 0.0, "gl": 0.0},
               "last_trade": 0, "idle_warned": False, "paused": False}
    return b, sent


def sig():
    return {"side": "LONG", "entry": 100.0, "sl": 99.0, "tp1": 101.0, "tp2": 102.0, "rr": 2.0, "kind": "test",
            "conf": 70, "val": 70, "risk_pct": 1.0, "tf": "1h"}


def live(bot):
    bot.open_live(SYM, sig(), "txt")
    assert SYM in bot.state["positions"], "la posición no quedó registrada"
    return bot.state["positions"][SYM]


def types(fake):
    return sorted((o["type"], o["stopPrice"], o["qty"]) for o in fake.open_orders())


def scenario_tp1_then_be():
    fx = FakeEx()
    bot, sent = fresh_bot(fx)
    rec = live(bot)
    assert rec["qty"] == 5.0, rec["qty"]                      # 1000 × 0.5% / 1 de riesgo
    assert types(fx) == [("STOP_MARKET", 99.0, 5.0), ("TAKE_PROFIT_MARKET", 101.0, 2.5),
                         ("TAKE_PROFIT_MARKET", 102.0, 2.5)], types(fx)
    tp1 = next(o["orderId"] for o in fx.open_orders() if o["stopPrice"] == 101.0)
    fx.fill(tp1)
    bot.manage()
    assert rec["half"] and rec["be"], "no detectó TP1"
    assert types(fx) == [("STOP_MARKET", 100.0, 2.5), ("TAKE_PROFIT_MARKET", 102.0, 2.5)], types(fx)
    fx.fill(next(o["orderId"] for o in fx.open_orders() if o["type"] == "STOP_MARKET"))   # salta el BE
    bot.manage()
    st = bot.state["stats"]
    assert SYM not in bot.state["positions"] and st["n"] == 1
    assert abs(st["sum_r"] - (0.5 * 1.0 + 0.5 * 0.0 - 0.1)) < 1e-6, st["sum_r"]   # 0.5R − 0.1R de comisiones
    assert not fx.open_orders(), "quedaron órdenes huérfanas"
    return "TP1 → BE: +0.40R, sin huérfanas"


def scenario_sl():
    fx = FakeEx()
    bot, _ = fresh_bot(fx)
    live(bot)
    fx.fill(next(o["orderId"] for o in fx.open_orders() if o["type"] == "STOP_MARKET"))
    bot.manage()
    st = bot.state["stats"]
    assert st["n"] == 1 and abs(st["sum_r"] - (-1.0 - 0.1)) < 1e-6, st["sum_r"]
    assert not fx.open_orders(), "quedaron órdenes huérfanas"
    return "SL directo: −1.10R, sin huérfanas"


def scenario_tp2():
    fx = FakeEx()
    bot, _ = fresh_bot(fx)
    live(bot)
    fx.fill(next(o["orderId"] for o in fx.open_orders() if o["stopPrice"] == 101.0))
    bot.manage()
    fx.fill(next(o["orderId"] for o in fx.open_orders() if o["stopPrice"] == 102.0))
    bot.manage()
    st = bot.state["stats"]
    assert st["n"] == 1 and abs(st["sum_r"] - (0.5 * 1.0 + 0.5 * 2.0 - 0.1)) < 1e-6, st["sum_r"]
    return "TP1 + TP2: +1.40R"


def scenario_chase_and_stop_guardian():
    fx = FakeEx()
    bot, sent = fresh_bot(fx)
    fx.px = 100.8                                              # ya avanzó 0.8R > CHASE_MAX_R
    bot.open_live(SYM, sig(), "txt")
    assert fx.opened == 0 and SYM not in bot.state["positions"], "persiguió el precio"
    fx.px = 100.0
    rec = live(bot)
    for o in fx.stop_orders(SYM, True):                        # alguien borra el stop a mano
        fx.cancel(SYM, o["orderId"])
    bot.manage()
    assert [t for t in types(fx) if t[0] == "STOP_MARKET"] == [("STOP_MARKET", 99.0, 5.0)], types(fx)
    assert any("repuesto" in m for m in sent), "el guardián no avisó"
    return "no persigue + guardián repone el stop"


if __name__ == "__main__":
    for fn in (scenario_tp1_then_be, scenario_sl, scenario_tp2, scenario_chase_and_stop_guardian):
        print("OK ·", fn.__name__, "·", fn())
    print("TODO OK")
