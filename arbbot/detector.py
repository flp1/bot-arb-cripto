"""Detecção de oportunidades de arbitragem cross-exchange.

Para cada símbolo e cada rota ordenada (comprar na corretora A, vender na
B), calcula o preço efetivo de execução percorrendo a profundidade do
orderbook (VWAP) para o tamanho configurado — não usa só o topo do book,
porque o topo raramente tem liquidez para a ordem inteira.
"""
import time
from dataclasses import dataclass
from itertools import permutations


@dataclass
class Opportunity:
    ts: float
    symbol: str
    buy_exchange: str
    sell_exchange: str
    buy_price: float          # VWAP efetivo de compra (asks)
    sell_price: float         # VWAP efetivo de venda (bids)
    amount_base: float        # quantidade da moeda base
    amount_usd: float         # notional aproximado
    gross_spread_pct: float   # spread bruto
    net_profit_usd: float     # lucro líquido após taxas dos dois lados
    net_profit_pct: float
    buy_fee_pct: float
    sell_fee_pct: float
    buy_book_age_ms: float
    sell_book_age_ms: float
    buy_liquidity_ok: bool
    sell_liquidity_ok: bool


def vwap_fill(levels, target_base: float):
    """Preço médio para executar `target_base` contra os níveis do book.

    Retorna (preco_medio, quantidade_preenchida). Se o book não tem
    profundidade suficiente, a quantidade preenchida é menor que o alvo.
    """
    filled = 0.0
    cost = 0.0
    # nível pode ter campos extras além de [preço, quantidade] em
    # algumas corretoras — usa só os dois primeiros
    for level in levels:
        price, amount = level[0], level[1]
        take = min(amount, target_base - filled)
        cost += take * price
        filled += take
        if filled >= target_base - 1e-12:
            break
    if filled <= 0:
        return None, 0.0
    return cost / filled, filled


class Detector:
    def __init__(self, cfg: dict, store, exchange_manager):
        self.cfg = cfg
        self.store = store
        self.exm = exchange_manager
        self.trade_amount_usd = cfg["strategy"]["trade_amount_usd"]
        self.fees = {
            ex_id: ex_cfg["taker_fee"]
            for ex_id, ex_cfg in cfg["exchanges"].items()
            if ex_cfg.get("enabled")
        }
        # melhor spread líquido visto desde a última leitura de status,
        # mesmo abaixo do threshold — dá visibilidade de que o bot está
        # vivo e de quão longe o mercado está de uma oportunidade
        self.best_seen = None  # (net_pct, symbol, buy_ex, sell_ex)

    def pop_best_seen(self):
        best, self.best_seen = self.best_seen, None
        return best

    def _track_best(self, net_pct, symbol, buy_ex, sell_ex):
        if self.best_seen is None or net_pct > self.best_seen[0]:
            self.best_seen = (net_pct, symbol, buy_ex, sell_ex)

    def scan(self):
        """Retorna a lista de oportunidades do snapshot atual dos books."""
        opportunities = []
        active = [ex for ex in self.exm.clients if not self.exm.degraded[ex]]
        for symbol in self.cfg["symbols"]:
            for buy_ex, sell_ex in permutations(active, 2):
                opp = self._check_route(symbol, buy_ex, sell_ex)
                if opp is not None:
                    opportunities.append(opp)
        return opportunities

    def _check_route(self, symbol, buy_ex, sell_ex):
        buy_book = self.store.get(buy_ex, symbol)
        sell_book = self.store.get(sell_ex, symbol)
        if not buy_book or not sell_book:
            return None
        if not buy_book["asks"] or not sell_book["bids"]:
            return None

        # Dimensiona a ordem em moeda base pelo preço do topo do ask
        top_ask = buy_book["asks"][0][0]
        amount_base = self.trade_amount_usd / top_ask

        buy_price, buy_filled = vwap_fill(buy_book["asks"], amount_base)
        sell_price, sell_filled = vwap_fill(sell_book["bids"], amount_base)
        if buy_price is None or sell_price is None:
            return None

        buy_fee = self.fees[buy_ex]
        sell_fee = self.fees[sell_ex]

        # Custo de comprar amount_base e receita de vendê-la, com taxas
        cost = amount_base * buy_price * (1 + buy_fee)
        proceeds = amount_base * sell_price * (1 - sell_fee)
        net_usd = proceeds - cost
        net_pct = net_usd / cost * 100
        gross_pct = (sell_price - buy_price) / buy_price * 100

        self._track_best(net_pct, symbol, buy_ex, sell_ex)
        if net_pct < self.cfg["strategy"]["log_threshold_pct"]:
            return None

        return Opportunity(
            ts=time.time(),
            symbol=symbol,
            buy_exchange=buy_ex,
            sell_exchange=sell_ex,
            buy_price=buy_price,
            sell_price=sell_price,
            amount_base=amount_base,
            amount_usd=amount_base * buy_price,
            gross_spread_pct=gross_pct,
            net_profit_usd=net_usd,
            net_profit_pct=net_pct,
            buy_fee_pct=buy_fee * 100,
            sell_fee_pct=sell_fee * 100,
            buy_book_age_ms=self.store.age_ms(buy_ex, symbol) or 0,
            sell_book_age_ms=self.store.age_ms(sell_ex, symbol) or 0,
            buy_liquidity_ok=buy_filled >= amount_base * 0.999,
            sell_liquidity_ok=sell_filled >= amount_base * 0.999,
        )
