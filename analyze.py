#!/usr/bin/env python3
"""Resumo dos CSVs gerados pelo bot — responde "a estratégia está funcionando?".

Uso: python3 analyze.py [logs/]
"""
import csv
import sys
from collections import Counter, defaultdict


def main():
    log_dir = sys.argv[1] if len(sys.argv) > 1 else "logs"

    rows = []
    try:
        with open(f"{log_dir}/opportunities.csv") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        print(f"Nenhum {log_dir}/opportunities.csv encontrado — rode o bot primeiro.")
        return

    if not rows:
        print("CSV vazio — nenhuma oportunidade registrada ainda.")
        return

    executed = [r for r in rows if r["executed"] == "1"]
    rejections = Counter(r["rejection_reason"] for r in rows if r["executed"] == "0")

    print(f"Oportunidades registradas: {len(rows)}")
    print(f"  período: {rows[0]['timestamp_utc']} → {rows[-1]['timestamp_utc']}")
    print(f"  executadas (paper): {len(executed)}")
    print("\nMotivos de rejeição:")
    for reason, n in rejections.most_common():
        print(f"  {n:6d}  {reason}")

    by_route = defaultdict(lambda: [0, 0.0])
    for r in executed:
        key = f"{r['symbol']} {r['buy_exchange']}->{r['sell_exchange']}"
        by_route[key][0] += 1
        by_route[key][1] += float(r["net_profit_usd"])

    if executed:
        total = sum(float(r["net_profit_usd"]) for r in executed)
        print(f"\nLucro líquido estimado (antes do slippage extra): {total:.2f} USDT")
        print("Por rota:")
        for route, (n, pnl) in sorted(by_route.items(), key=lambda x: -x[1][1]):
            print(f"  {route}: {n} trades, {pnl:.2f} USDT")

    # P&L real simulado (com slippage) vem do trades.csv
    try:
        with open(f"{log_dir}/trades.csv") as f:
            trades = list(csv.DictReader(f))
        if trades:
            pnl = sum(float(t["pnl_usd"]) for t in trades)
            wins = sum(1 for t in trades if float(t["pnl_usd"]) > 0)
            print(f"\nPaper trades executados: {len(trades)} | "
                  f"vencedores: {wins} ({wins/len(trades)*100:.0f}%) | "
                  f"P&L com slippage: {pnl:.2f} USDT")
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    main()
