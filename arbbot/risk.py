"""Gestor de risco — toda oportunidade passa por aqui antes de executar.

Cada verificação devolve um motivo de rejeição legível, que vai para o
CSV de oportunidades (coluna `rejection_reason`). Assim dá para auditar
depois por que o bot deixou de operar em cada caso.
"""
import logging
import os
import statistics
import time
from collections import deque
from datetime import datetime, timezone

log = logging.getLogger("arbbot.risk")

KILL_SWITCH_FILE = "STOP"


class RiskManager:
    def __init__(self, cfg: dict, store, base_dir: str):
        self.cfg = cfg["risk"]
        self.strategy = cfg["strategy"]
        self.symbols = cfg["symbols"]
        self.store = store
        self.base_dir = base_dir

        self.daily_pnl = 0.0
        self._pnl_day = self._today()
        self.halted = False
        self.halt_reason = ""
        self._trade_times = deque(maxlen=200)
        self._last_trade = {}  # (symbol, buy_ex, sell_ex) -> ts

    # ------------------------------------------------------------------
    def check(self, opp, exchange_manager) -> str | None:
        """Retorna None se aprovado, ou o motivo da rejeição."""
        self._roll_day()

        if self._kill_switch_active():
            return "kill_switch: arquivo STOP presente"

        if self.halted:
            return f"halted: {self.halt_reason}"

        if exchange_manager.degraded.get(opp.buy_exchange) or \
           exchange_manager.degraded.get(opp.sell_exchange):
            return "exchange_degraded"

        # Lucro mínimo
        if opp.net_profit_pct < self.strategy["min_profit_pct"]:
            return f"below_min_profit ({opp.net_profit_pct:.4f}%)"

        # Dados velhos = preço possivelmente irreal
        max_age = self.cfg["max_book_age_ms"]
        if opp.buy_book_age_ms > max_age or opp.sell_book_age_ms > max_age:
            return (f"stale_book (buy={opp.buy_book_age_ms:.0f}ms, "
                    f"sell={opp.sell_book_age_ms:.0f}ms)")

        # Liquidez insuficiente para o tamanho da ordem
        if not (opp.buy_liquidity_ok and opp.sell_liquidity_ok):
            return "insufficient_depth"

        # Spread bom demais costuma ser problema (feed travado, par
        # suspenso numa das corretoras, manutenção de saques)
        if opp.net_profit_pct > self.cfg["suspicious_spread_pct"]:
            return f"suspicious_spread ({opp.net_profit_pct:.2f}%)"

        # Preço fora da mediana das corretoras = feed possivelmente ruim
        reason = self._price_sanity(opp)
        if reason:
            return reason

        # Limite de operações por minuto
        now = time.time()
        recent = sum(1 for t in self._trade_times if now - t < 60)
        if recent >= self.cfg["max_trades_per_minute"]:
            return "rate_limit_trades_per_minute"

        # Cooldown por rota
        key = (opp.symbol, opp.buy_exchange, opp.sell_exchange)
        last = self._last_trade.get(key, 0)
        if now - last < self.strategy["cooldown_sec"]:
            return "cooldown"

        return None

    def register_trade(self, opp, realized_pnl_usd: float):
        now = time.time()
        self._trade_times.append(now)
        self._last_trade[(opp.symbol, opp.buy_exchange, opp.sell_exchange)] = now
        self.daily_pnl += realized_pnl_usd
        if self.daily_pnl <= -abs(self.cfg["max_daily_loss_usd"]):
            self.halted = True
            self.halt_reason = (f"limite de perda diária atingido "
                                f"({self.daily_pnl:.2f} USDT)")
            log.error("CIRCUIT BREAKER: %s — bot pausado até o próximo dia UTC",
                      self.halt_reason)

    # ------------------------------------------------------------------
    def _price_sanity(self, opp):
        """Compara o mid de cada lado com a mediana de todas as corretoras."""
        mids = []
        for (ex_id, symbol), _ in list(self.store._books.items()):
            if symbol != opp.symbol:
                continue
            book = self.store.get(ex_id, symbol)
            if book and book["bids"] and book["asks"]:
                mids.append((book["bids"][0][0] + book["asks"][0][0]) / 2)
        if len(mids) < 2:
            return "not_enough_price_sources"
        median = statistics.median(mids)
        limit = self.cfg["max_price_deviation_pct"]
        for label, price in (("buy", opp.buy_price), ("sell", opp.sell_price)):
            dev = abs(price - median) / median * 100
            if dev > limit:
                return f"price_deviation_{label} ({dev:.2f}% da mediana)"
        return None

    def _kill_switch_active(self) -> bool:
        return os.path.exists(os.path.join(self.base_dir, KILL_SWITCH_FILE))

    def _today(self):
        return datetime.now(timezone.utc).date()

    def _roll_day(self):
        today = self._today()
        if today != self._pnl_day:
            log.info("Novo dia UTC — P&L diário zerado (anterior: %.2f USDT)",
                     self.daily_pnl)
            self._pnl_day = today
            self.daily_pnl = 0.0
            if self.halted and "perda diária" in self.halt_reason:
                self.halted = False
                self.halt_reason = ""
