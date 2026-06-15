"""Execução simulada (paper trading) com saldos por corretora.

Modelo de inventário: arbitragem cross-exchange sem transferir fundos
entre corretoras — compra na corretora barata gastando USDT e vende a
mesma quantidade na corretora cara usando o inventário da moeda base.
O saldo total da moeda base fica ~constante; o lucro aparece em USDT.

A simulação é conservadora: além do VWAP de profundidade calculado pelo
detector, aplica um buffer extra de slippage (config) para representar o
movimento do book entre a detecção e a execução real.
"""
import logging
from collections import defaultdict

log = logging.getLogger("arbbot.paper")


class PaperTrader:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.slippage = cfg["strategy"]["slippage_buffer_pct"] / 100
        initial = cfg["paper"]["initial_balances"]
        # saldo independente por corretora
        self.balances = {
            ex_id: defaultdict(float, dict(initial))
            for ex_id, ex_cfg in cfg["exchanges"].items()
            if ex_cfg.get("enabled")
        }
        self.total_pnl_usd = 0.0
        self.trades_executed = 0
        self.trades_skipped_balance = 0

    def execute(self, opp):
        """Tenta executar a oportunidade. Retorna dict do trade ou None."""
        base, quote = opp.symbol.split("/")
        buy_bal = self.balances[opp.buy_exchange]
        sell_bal = self.balances[opp.sell_exchange]

        # Preços efetivos com buffer de slippage (pior para nós nos 2 lados)
        buy_price = opp.buy_price * (1 + self.slippage)
        sell_price = opp.sell_price * (1 - self.slippage)

        buy_fee = self.cfg["exchanges"][opp.buy_exchange]["taker_fee"]
        sell_fee = self.cfg["exchanges"][opp.sell_exchange]["taker_fee"]

        amount = opp.amount_base
        cost = amount * buy_price * (1 + buy_fee)
        proceeds = amount * sell_price * (1 - sell_fee)

        # Verificação de saldo — igual seria no live
        if buy_bal[quote] < cost:
            self.trades_skipped_balance += 1
            log.warning("Saldo insuficiente de %s em %s (%.2f < %.2f) — pulado",
                        quote, opp.buy_exchange, buy_bal[quote], cost)
            return None
        if sell_bal[base] < amount:
            self.trades_skipped_balance += 1
            log.warning("Inventário insuficiente de %s em %s (%.6f < %.6f) — pulado",
                        base, opp.sell_exchange, sell_bal[base], amount)
            return None

        # Liquida os dois lados
        buy_bal[quote] -= cost
        buy_bal[base] += amount
        sell_bal[base] -= amount
        sell_bal[quote] += proceeds

        pnl = proceeds - cost
        self.total_pnl_usd += pnl
        self.trades_executed += 1

        log.info("PAPER TRADE #%d %s: compra %.6f @ %.4f em %s, vende @ %.4f "
                 "em %s | P&L %.4f USDT (%.4f%%) | P&L acumulado %.2f USDT",
                 self.trades_executed, opp.symbol, amount, buy_price,
                 opp.buy_exchange, sell_price, opp.sell_exchange,
                 pnl, pnl / cost * 100, self.total_pnl_usd)

        return {
            "amount_base": amount,
            "buy_price_effective": buy_price,
            "sell_price_effective": sell_price,
            "cost_usd": cost,
            "proceeds_usd": proceeds,
            "pnl_usd": pnl,
            "pnl_pct": pnl / cost * 100,
        }

    def summary(self) -> str:
        lines = [
            f"Trades executados: {self.trades_executed} | "
            f"pulados por saldo: {self.trades_skipped_balance} | "
            f"P&L total: {self.total_pnl_usd:.2f} USDT"
        ]
        for ex_id, bal in self.balances.items():
            assets = ", ".join(f"{a}={v:.6g}" for a, v in sorted(bal.items()) if v)
            lines.append(f"  {ex_id}: {assets}")
        return "\n".join(lines)
