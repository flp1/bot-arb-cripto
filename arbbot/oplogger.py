"""Logs em CSV para análise posterior da estratégia.

opportunities.csv — TODA oportunidade detectada acima do log_threshold,
executada ou não, com o motivo da rejeição quando houver. É a base para
decidir se a estratégia vale a pena: some net_profit_usd das linhas
executáveis ao longo de dias/semanas.

trades.csv — apenas as operações simuladas de fato, com preços efetivos
(slippage incluído) e P&L.
"""
import csv
import logging
import logging.handlers
import os
from datetime import datetime, timezone

OPP_FIELDS = [
    "timestamp_utc", "symbol", "buy_exchange", "sell_exchange",
    "buy_price", "sell_price", "amount_base", "amount_usd",
    "gross_spread_pct", "net_profit_pct", "net_profit_usd",
    "buy_fee_pct", "sell_fee_pct", "buy_book_age_ms", "sell_book_age_ms",
    "executed", "rejection_reason",
]

TRADE_FIELDS = [
    "timestamp_utc", "symbol", "buy_exchange", "sell_exchange",
    "amount_base", "buy_price_effective", "sell_price_effective",
    "cost_usd", "proceeds_usd", "pnl_usd", "pnl_pct",
]


def setup_logging(log_dir: str):
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    fh = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "bot.log"), maxBytes=10_000_000, backupCount=5)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)


class CsvLogger:
    def __init__(self, log_dir: str):
        self.opp_path = os.path.join(log_dir, "opportunities.csv")
        self.trade_path = os.path.join(log_dir, "trades.csv")
        self._ensure_header(self.opp_path, OPP_FIELDS)
        self._ensure_header(self.trade_path, TRADE_FIELDS)

    @staticmethod
    def _ensure_header(path, fields):
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            with open(path, "w", newline="") as f:
                csv.writer(f).writerow(fields)

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    def log_opportunity(self, opp, executed: bool, rejection_reason: str | None):
        row = [
            self._now(), opp.symbol, opp.buy_exchange, opp.sell_exchange,
            f"{opp.buy_price:.8f}", f"{opp.sell_price:.8f}",
            f"{opp.amount_base:.8f}", f"{opp.amount_usd:.2f}",
            f"{opp.gross_spread_pct:.5f}", f"{opp.net_profit_pct:.5f}",
            f"{opp.net_profit_usd:.5f}",
            f"{opp.buy_fee_pct:.4f}", f"{opp.sell_fee_pct:.4f}",
            f"{opp.buy_book_age_ms:.0f}", f"{opp.sell_book_age_ms:.0f}",
            int(executed), rejection_reason or "",
        ]
        with open(self.opp_path, "a", newline="") as f:
            csv.writer(f).writerow(row)

    def log_trade(self, opp, trade: dict):
        row = [
            self._now(), opp.symbol, opp.buy_exchange, opp.sell_exchange,
            f"{trade['amount_base']:.8f}",
            f"{trade['buy_price_effective']:.8f}",
            f"{trade['sell_price_effective']:.8f}",
            f"{trade['cost_usd']:.4f}", f"{trade['proceeds_usd']:.4f}",
            f"{trade['pnl_usd']:.5f}", f"{trade['pnl_pct']:.5f}",
        ]
        with open(self.trade_path, "a", newline="") as f:
            csv.writer(f).writerow(row)
