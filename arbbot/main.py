"""Loop principal do bot de arbitragem."""
import asyncio
import logging
import os
import signal
import time

from .config import load_config
from .detector import Detector
from .exchanges import BookStore, ExchangeManager
from .oplogger import CsvLogger, setup_logging
from .paper_trader import PaperTrader
from .risk import RiskManager

log = logging.getLogger("arbbot.main")


class Bot:
    def __init__(self, config_path: str):
        self.base_dir = os.path.dirname(os.path.abspath(config_path))
        self.cfg = load_config(config_path)

        log_dir = os.path.join(self.base_dir, self.cfg["logging"]["dir"])
        setup_logging(log_dir)

        self.store = BookStore()
        self.exm = ExchangeManager(self.cfg, self.store)
        self.detector = Detector(self.cfg, self.store, self.exm)
        self.risk = RiskManager(self.cfg, self.store, self.base_dir)
        self.csv = CsvLogger(log_dir)

        if self.cfg["mode"] == "paper":
            self.trader = PaperTrader(self.cfg)
        else:
            # Estrutura preparada, mas execução real desabilitada de
            # propósito até a estratégia ser validada no paper trading.
            raise NotImplementedError(
                "Execução LIVE ainda não implementada. Valide a estratégia "
                "com os CSVs do paper trading primeiro.")

        self._stop = asyncio.Event()
        # Throttle de log por rota para oportunidades não executadas:
        # (symbol, buy_ex, sell_ex) -> último timestamp registrado.
        self._log_times = {}

    # ------------------------------------------------------------------
    async def run(self):
        log.info("=== Bot de arbitragem iniciado — modo %s ===",
                 self.cfg["mode"].upper())
        log.info("Pares: %s | tamanho por trade: %s USDT | lucro mínimo: %s%%",
                 ", ".join(self.cfg["symbols"]),
                 self.cfg["strategy"]["trade_amount_usd"],
                 self.cfg["strategy"]["min_profit_pct"])
        log.info("Kill switch: crie um arquivo chamado STOP em %s para pausar",
                 self.base_dir)

        await self.exm.start()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._stop.set)

        tasks = [asyncio.create_task(t) for t in self.exm.watch_tasks()]
        tasks.append(asyncio.create_task(self._detector_loop()))
        tasks.append(asyncio.create_task(self._status_loop()))

        await self._stop.wait()
        log.info("Encerrando...")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.exm.close()
        log.info("Resumo final:\n%s", self.trader.summary())

    # ------------------------------------------------------------------
    async def _detector_loop(self):
        interval = self.cfg["strategy"]["scan_interval_sec"]
        log_cooldown = self.cfg["strategy"].get("log_cooldown_sec", 0)
        while not self._stop.is_set():
            try:
                for opp in self.detector.scan():
                    rejection = self.risk.check(opp, self.exm)
                    if rejection is None:
                        trade = self.trader.execute(opp)
                        if trade is not None:
                            self.csv.log_trade(opp, trade)
                            self.risk.register_trade(opp, trade["pnl_usd"])
                            self.csv.log_opportunity(opp, True, None)
                        else:
                            self.csv.log_opportunity(opp, False,
                                                     "insufficient_balance")
                    elif self._should_log(opp, log_cooldown):
                        # Oportunidades não executadas (inclui as "quase lá")
                        # entram no CSV com throttle por rota para não inundar.
                        self.csv.log_opportunity(opp, False, rejection)
            except Exception:
                log.exception("Erro no loop do detector")
            await asyncio.sleep(interval)

    def _should_log(self, opp, cooldown):
        if cooldown <= 0:
            return True
        key = (opp.symbol, opp.buy_exchange, opp.sell_exchange)
        now = time.time()
        if now - self._log_times.get(key, 0) >= cooldown:
            self._log_times[key] = now
            return True
        return False

    async def _status_loop(self):
        interval = self.cfg["logging"]["status_interval_sec"]
        while not self._stop.is_set():
            await asyncio.sleep(interval)
            degraded = [e for e, d in self.exm.degraded.items() if d]
            best = self.detector.pop_best_seen()
            best_txt = (f" | melhor spread: {best[0]:.4f}% {best[1]} "
                        f"{best[2]}->{best[3]}" if best else "")
            log.info("STATUS | P&L dia: %.2f USDT | %s%s%s",
                     self.risk.daily_pnl,
                     self.trader.summary().splitlines()[0],
                     best_txt,
                     f" | DEGRADADAS: {degraded}" if degraded else "")


def main():
    import sys
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    bot = Bot(config_path)
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
