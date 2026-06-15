"""Carregamento e validação da configuração."""
import os
import yaml


class ConfigError(Exception):
    pass


def load_config(path: str) -> dict:
    with open(path, "r") as f:
        cfg = yaml.safe_load(f)

    mode = cfg.get("mode", "paper")
    if mode not in ("paper", "live"):
        raise ConfigError(f"mode inválido: {mode!r} (use 'paper' ou 'live')")

    # ------------------------------------------------------------------
    # TRAVA DE SEGURANÇA: modo live exige confirmação explícita por
    # variável de ambiente, além da config. Impede ir para produção por
    # acidente (ex.: editar o yaml errado).
    # ------------------------------------------------------------------
    if mode == "live":
        if os.environ.get("ARBBOT_CONFIRM_LIVE") != "yes":
            raise ConfigError(
                "Modo LIVE bloqueado: defina a variável de ambiente "
                "ARBBOT_CONFIRM_LIVE=yes para confirmar que você entende "
                "os riscos. Recomendado validar a estratégia em paper "
                "trading antes."
            )

    enabled = [n for n, e in cfg.get("exchanges", {}).items() if e.get("enabled")]
    if len(enabled) < 2:
        raise ConfigError("Arbitragem exige pelo menos 2 corretoras habilitadas.")

    if not cfg.get("symbols"):
        raise ConfigError("Nenhum símbolo configurado em 'symbols'.")

    strat = cfg.get("strategy", {})
    if strat.get("trade_amount_usd", 0) <= 0:
        raise ConfigError("strategy.trade_amount_usd deve ser > 0.")
    if strat.get("min_profit_pct", 0) < 0:
        raise ConfigError("strategy.min_profit_pct não pode ser negativo.")

    return cfg
