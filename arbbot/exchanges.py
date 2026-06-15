"""Conexão com as corretoras e manutenção dos orderbooks em tempo real.

Usa os websockets do ccxt (watch_order_book). Cada corretora/símbolo tem
uma task própria; falhas reconectam com backoff exponencial e, após
muitos erros consecutivos, a corretora é marcada como degradada (o
detector ignora seus dados até voltar a receber updates saudáveis).
"""
import asyncio
import logging
import time

import ccxt.pro as ccxtpro

log = logging.getLogger("arbbot.exchanges")


class BookStore:
    """Último orderbook conhecido por (exchange, symbol), com timestamp local."""

    def __init__(self):
        self._books = {}  # (exchange_id, symbol) -> {"bids":[], "asks":[], "local_ts": float}

    def update(self, exchange_id: str, symbol: str, orderbook: dict):
        self._books[(exchange_id, symbol)] = {
            "bids": orderbook.get("bids") or [],
            "asks": orderbook.get("asks") or [],
            "local_ts": time.time(),
        }

    def get(self, exchange_id: str, symbol: str):
        return self._books.get((exchange_id, symbol))

    def age_ms(self, exchange_id: str, symbol: str):
        book = self.get(exchange_id, symbol)
        if book is None:
            return None
        return (time.time() - book["local_ts"]) * 1000


class ExchangeManager:
    def __init__(self, cfg: dict, store: BookStore):
        self.cfg = cfg
        self.store = store
        self.clients = {}
        self.degraded = {}  # exchange_id -> bool
        self._consecutive_errors = {}
        self.max_consecutive_errors = cfg["risk"]["max_consecutive_errors"]

    async def start(self):
        for ex_id, ex_cfg in self.cfg["exchanges"].items():
            if not ex_cfg.get("enabled"):
                continue
            klass = getattr(ccxtpro, ex_id)
            params = {"enableRateLimit": True, "newUpdates": True}
            # Chaves só são necessárias em modo live; paper usa dados públicos.
            if self.cfg["mode"] == "live":
                params["apiKey"] = ex_cfg.get("api_key", "")
                params["secret"] = ex_cfg.get("secret", "")
                if ex_cfg.get("password"):
                    params["password"] = ex_cfg["password"]
            self.clients[ex_id] = klass(params)
            self.degraded[ex_id] = False
            self._consecutive_errors[ex_id] = 0

        # Valida conectividade e símbolos; corretora inacessível (ex.:
        # bloqueio geográfico, manutenção) é descartada em vez de
        # derrubar o bot — desde que sobrem pelo menos 2.
        failed = []
        for ex_id, client in list(self.clients.items()):
            try:
                await client.load_markets()
            except Exception as exc:
                log.error("Falha ao conectar em %s — corretora descartada "
                          "nesta sessão: %s", ex_id, exc)
                failed.append(ex_id)
                await client.close()
                del self.clients[ex_id]
                del self.degraded[ex_id]
                continue
            for symbol in self.cfg["symbols"]:
                if symbol not in client.markets:
                    raise RuntimeError(
                        f"Símbolo {symbol} não existe em {ex_id} — remova-o "
                        f"da config ou desabilite a corretora."
                    )
        if len(self.clients) < 2:
            raise RuntimeError(
                f"Arbitragem exige >= 2 corretoras acessíveis; disponíveis: "
                f"{list(self.clients) or 'nenhuma'} (falharam: {failed})")
        log.info("Conectado a: %s%s", ", ".join(self.clients),
                 f" (indisponíveis: {failed})" if failed else "")

    def watch_tasks(self):
        tasks = []
        for ex_id in self.clients:
            for symbol in self.cfg["symbols"]:
                tasks.append(self._watch_book(ex_id, symbol))
        return tasks

    async def _watch_book(self, ex_id: str, symbol: str):
        client = self.clients[ex_id]
        backoff = 1
        while True:
            try:
                # sem `limit` explícito: cada corretora aceita valores
                # diferentes; o default do ccxt funciona em todas
                book = await client.watch_order_book(symbol)
                self.store.update(ex_id, symbol, book)
                self._consecutive_errors[ex_id] = 0
                if self.degraded[ex_id]:
                    self.degraded[ex_id] = False
                    log.warning("%s voltou ao normal", ex_id)
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._consecutive_errors[ex_id] += 1
                n = self._consecutive_errors[ex_id]
                log.error("Erro no websocket %s %s (%d consecutivos): %s",
                          ex_id, symbol, n, exc)
                if n >= self.max_consecutive_errors and not self.degraded[ex_id]:
                    self.degraded[ex_id] = True
                    log.error("%s marcada como DEGRADADA — dados ignorados "
                              "pelo detector até reconectar", ex_id)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def close(self):
        for client in self.clients.values():
            try:
                await client.close()
            except Exception:
                pass
