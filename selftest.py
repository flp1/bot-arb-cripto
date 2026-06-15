#!/usr/bin/env python3
"""Selftest offline: injeta orderbooks sintéticos e verifica o pipeline
completo — detecção, regras de risco, execução paper e escrita dos CSVs.
Rode sempre que mexer no código: venv/bin/python selftest.py
"""
import os
import sys
import tempfile
import time

from arbbot.config import load_config
from arbbot.detector import Detector
from arbbot.exchanges import BookStore
from arbbot.oplogger import CsvLogger
from arbbot.paper_trader import PaperTrader
from arbbot.risk import RiskManager


class FakeExchangeManager:
    def __init__(self, exchanges):
        self.clients = {e: None for e in exchanges}
        self.degraded = {e: False for e in exchanges}


def book(bid, ask, depth=50):
    return {"bids": [[bid, depth], [bid * 0.999, depth]],
            "asks": [[ask, depth], [ask * 1.001, depth]]}


def main():
    cfg = load_config("config.yaml")
    cfg["symbols"] = ["BTC/USDT"]
    store = BookStore()
    exm = FakeExchangeManager(["binance", "okx", "bybit"])
    detector = Detector(cfg, store, exm)

    tmp = tempfile.mkdtemp()
    risk = RiskManager(cfg, store, tmp)
    trader = PaperTrader(cfg)
    csvlog = CsvLogger(tmp)
    failures = []

    def check(name, cond):
        print(f"  [{'OK' if cond else 'FALHOU'}] {name}")
        if not cond:
            failures.append(name)

    # 1) Sem divergência de preço -> nenhuma oportunidade executável
    store.update("binance", "BTC/USDT", book(50000, 50001))
    store.update("okx", "BTC/USDT", book(50000, 50001))
    store.update("bybit", "BTC/USDT", book(50000, 50001))
    opps = [o for o in detector.scan()
            if risk.check(o, exm) is None]
    check("mercado alinhado não gera trade", len(opps) == 0)

    # 2) Divergência clara (binance barata, okx cara) -> executa
    store.update("binance", "BTC/USDT", book(49900, 49901))
    store.update("okx", "BTC/USDT", book(50200, 50201))
    opps = detector.scan()
    approved = [o for o in opps if risk.check(o, exm) is None]
    check("divergência de 0,6%% detectada", len(approved) >= 1)
    best = max(approved, key=lambda o: o.net_profit_pct)
    check("rota correta (compra binance, vende okx)",
          best.buy_exchange == "binance" and best.sell_exchange == "okx")
    check("lucro líquido desconta taxas (0,6%% bruto -> ~0,4%% líquido)",
          0.3 < best.net_profit_pct < 0.5)

    trade = trader.execute(best)
    check("paper trade executou", trade is not None)
    check("P&L positivo após slippage", trade["pnl_usd"] > 0)
    csvlog.log_trade(best, trade)
    csvlog.log_opportunity(best, True, None)
    risk.register_trade(best, trade["pnl_usd"])

    usdt_total = sum(b["USDT"] for b in trader.balances.values())
    initial_total = cfg["paper"]["initial_balances"]["USDT"] * 3
    check("lucro refletido no saldo total de USDT",
          abs((usdt_total - initial_total) - trade["pnl_usd"]) < 1e-6)
    btc_total = sum(b["BTC"] for b in trader.balances.values())
    check("inventário total de BTC conservado",
          abs(btc_total - cfg["paper"]["initial_balances"]["BTC"] * 3) < 1e-9)

    # 3) Cooldown bloqueia repetição imediata na mesma rota
    rej = risk.check(best, exm)
    check("cooldown bloqueia repetição", rej == "cooldown")

    # 4) Spread suspeito (5%) é rejeitado
    store.update("okx", "BTC/USDT", book(52500, 52501))
    sus = [o for o in detector.scan() if o.buy_exchange == "binance"
           and o.sell_exchange == "okx"][0]
    rej = risk.check(sus, exm)
    check("spread suspeito rejeitado", rej and "suspicious" in rej)

    # 5) Book velho é rejeitado
    store.update("okx", "BTC/USDT", book(50200, 50201))
    store._books[("okx", "BTC/USDT")]["local_ts"] = time.time() - 10
    stale = [o for o in detector.scan() if o.sell_exchange == "okx"][0]
    rej = risk.check(stale, exm)
    check("book velho rejeitado", rej and "stale" in rej)

    # 6) Corretora degradada é ignorada pelo detector
    store.update("okx", "BTC/USDT", book(50200, 50201))
    exm.degraded["okx"] = True
    opps = detector.scan()
    check("corretora degradada ignorada",
          not any("okx" in (o.buy_exchange, o.sell_exchange) for o in opps))
    exm.degraded["okx"] = False

    # 7) Kill switch
    open(os.path.join(tmp, "STOP"), "w").close()
    fresh = [o for o in detector.scan() if o.sell_exchange == "okx"][0]
    rej = risk.check(fresh, exm)
    check("kill switch (arquivo STOP) bloqueia", rej and "kill_switch" in rej)
    os.remove(os.path.join(tmp, "STOP"))

    # 8) Circuit breaker de perda diária
    loss = risk.daily_pnl + cfg["risk"]["max_daily_loss_usd"] + 1
    risk.register_trade(best, -loss)
    rej = risk.check(fresh, exm)
    check("circuit breaker de perda diária ativa", rej and "halted" in rej)

    # 9) CSVs escritos
    check("opportunities.csv escrito",
          sum(1 for _ in open(os.path.join(tmp, "opportunities.csv"))) >= 2)
    check("trades.csv escrito",
          sum(1 for _ in open(os.path.join(tmp, "trades.csv"))) >= 2)

    print()
    if failures:
        print(f"{len(failures)} verificações FALHARAM: {failures}")
        sys.exit(1)
    print("Todas as verificações passaram.")


if __name__ == "__main__":
    main()
