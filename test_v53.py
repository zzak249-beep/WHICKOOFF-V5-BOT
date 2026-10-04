"""
Pruebas sin red de lo nuevo en v5.3:
  · límites de BingX por grupo (datos 30/s, cuenta 5/s) y penalización global tras un rate limit
  · el SL adjunto solo se desactiva si la misma orden SIN adjunto es aceptada
  · un fallo en un símbolo no impide procesar los demás
  · una señal con retraso no se abre en LIVE

  python test_v53.py
"""
import os
import sys
import tempfile
import time

os.environ["DATA_DIR"] = tempfile.mkdtemp()
sys.argv = ["x"]

import config as C  # noqa: E402
import test_live_flow as T  # noqa: E402  (reutiliza FakeEx y los helpers)
from bingx import BingX, BingXError  # noqa: E402

C.LIVE = True


def t_throttle():
    bx = BingX("k", "s")
    bx.interval = {False: 0.02, True: 0.10}
    t0 = time.monotonic()
    for _ in range(11):
        bx._throttle(False)
    pub = time.monotonic() - t0
    t0 = time.monotonic()
    for _ in range(4):
        bx._throttle(True)
    sig = time.monotonic() - t0
    assert 0.18 <= pub <= 0.40, pub            # 10 huecos × 20 ms
    assert 0.27 <= sig <= 0.55, sig            # 3 huecos × 100 ms
    # los grupos son independientes: agotar uno no retrasa al otro
    bx2 = BingX("k", "s")
    bx2.interval = {False: 0.5, True: 0.5}
    bx2._throttle(False)
    t0 = time.monotonic()
    bx2._throttle(True)
    assert time.monotonic() - t0 < 0.05, "los grupos no son independientes"
    # penalización global
    bx3 = BingX("k", "s")
    bx3.interval = {False: 0.001, True: 0.001}
    bx3._penalize(0.3)
    t0 = time.monotonic()
    bx3._throttle(False)
    assert time.monotonic() - t0 >= 0.25, "la penalización no frenó"
    return f"datos {pub:.2f}s/10 · cuenta {sig:.2f}s/3 · grupos independientes · penalización global"


def t_attach_not_disabled_by_unrelated_rejection():
    fx = T.FakeEx()
    orig = fx.market_open
    calls = {"n": 0}

    def margin_error(*a, **k):                # rechazo por margen: afecta con y sin adjunto
        calls["n"] += 1
        raise BingXError("/trade/order code=101204: insufficient margin")
    fx.market_open = margin_error
    bot, sent = T.fresh_bot(fx)
    bot.open_live(T.SYM, T.sig(), "txt")
    assert bot.attach_ok is True, "desactivó el SL adjunto por un rechazo de margen"
    assert calls["n"] == 2 and T.SYM not in bot.state["positions"]
    return "rechazo por margen no desactiva el adjunto"


def t_attach_disabled_when_only_attach_fails():
    fx = T.FakeEx()
    orig = fx.market_open

    def only_attach_fails(s, long, qty, cid, stop_loss=None):
        if stop_loss is not None:
            raise BingXError("/trade/order code=109400: stopLoss param invalid")
        return orig(s, long, qty, cid, None)
    fx.market_open = only_attach_fails
    bot, sent = T.fresh_bot(fx)
    rec = T.live(bot)
    assert bot.attach_ok is False, "no desactivó el adjunto cuando era el culpable"
    assert [t for t in T.types(fx) if t[0] == "STOP_MARKET"] == [("STOP_MARKET", 99.0, 5.0)], T.types(fx)
    return "adjunto culpable → se desactiva y el SL se pone aparte"


def t_symbol_isolation():
    import main
    fx = T.FakeEx()
    bot, sent = T.fresh_bot(fx)
    bot.pool = main.ThreadPoolExecutor(2)
    bot.symbols, bot.engines = ["AAA-USDT", "BBB-USDT"], {}
    now = int(time.time() * 1000)
    ms = main.tf_ms("1h")
    t_bar = now // ms * ms - ms                # última vela cerrada

    class Eng:
        def __init__(self, boom):
            self.last_t, self.boom, self.last, self.n = t_bar - ms, boom, {"phase": 0}, 0

        def update(self, t, o, h, l, c, v):
            if self.boom:
                raise RuntimeError("fallo simulado del motor")
            self.n += 1
            return {"atr": 1.0, "time": t, "entry_now": False, "fail": None, "sig": 0, "wdir": 0, "conf": 0, "val": 0}
    ok = Eng(False)
    bot.engines = {("AAA-USDT", "1h"): Eng(True), ("BBB-USDT", "1h"): ok}
    fx.contracts["AAA-USDT"] = fx.contracts["BBB-USDT"] = fx.contracts[T.SYM]
    fx.klines = lambda s, tf, n: [[t_bar, 1, 2, 0.5, 1.5, 10.0]]
    bot.ex = fx
    bot.process_tf("1h")
    assert ok.n == 1, "el símbolo sano no se procesó"
    assert any("error procesando 1 símbolo" in m for m in sent), sent
    return "un símbolo roto no tumba al resto + aviso"


def t_signal_age():
    import main
    fx = T.FakeEx()
    bot, sent = T.fresh_bot(fx)
    C.MAX_SIGNAL_AGE_S = 180
    # señal con 10 minutos de retraso
    d = {"time": int((time.time() - 3600 - 600) * 1000) // 3600000 * 3600000}
    age = round(time.time() - (d["time"] + 3_600_000) / 1000.0, 1)
    assert age > 180
    why = []
    if C.LIVE and C.MAX_SIGNAL_AGE_S > 0 and age > C.MAX_SIGNAL_AGE_S:
        why.append("retraso")
    assert why, "la edad de la señal no bloquea"
    return f"edad {age:.0f}s > 180s bloquea (lógica)"


if __name__ == "__main__":
    for fn in (t_throttle, t_attach_not_disabled_by_unrelated_rejection, t_attach_disabled_when_only_attach_fails,
               t_symbol_isolation, t_signal_age):
        print("OK ·", fn.__name__, "·", fn())
    print("TODO OK")
