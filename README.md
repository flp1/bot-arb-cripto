# Bot de Arbitragem Cross-Exchange (Binance / OKX / Bybit)

Monitora os orderbooks das 3 corretoras via websocket em tempo real,
detecta diferenças de preço entre elas e simula a execução (**paper
trading**) registrando tudo em CSV para você decidir, com dados, se a
estratégia compensa antes de arriscar dinheiro real.

## Como funciona a estratégia

Arbitragem espacial com **modelo de inventário**: você mantém USDT e as
moedas em todas as corretoras. Quando o preço diverge, o bot compra na
corretora barata e vende a mesma quantidade na cara **simultaneamente**
— sem transferir fundos entre corretoras (transferência é lenta demais
e mataria a oportunidade). O lucro aparece como aumento do saldo total
em USDT.

O cálculo de lucro é realista:
- usa o **VWAP da profundidade do book** (não só o topo) para o tamanho
  configurado da ordem;
- desconta a **taxa taker dos dois lados**;
- na simulação aplica ainda um **buffer extra de slippage** (config).

## Instalação e uso

```bash
cd /opt/bots/teste
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python run.py            # usa config.yaml
```

Não precisa de API keys para o paper trading — só dados públicos.

## ⚠️ Restrição geográfica de IP (LEIA ANTES DE RODAR)

**As corretoras bloqueiam o acesso a partir de IPs de regiões restritas.**
Isso NÃO é um bug do bot — é política de cada corretora. Os bloqueios
mais comuns observados:

- **Binance** — responde HTTP `451` (*"Service unavailable from a
  restricted location"*) para IPs de regiões/datacenters não elegíveis
  (ex.: IPs de datacenter dos EUA na `binance.com`).
- **Bybit** — responde HTTP `403` (CloudFront: *"configured to block
  access from your country"*).
- **OKX** — costuma funcionar na maioria das regiões.

Como a arbitragem exige **pelo menos 2 corretoras acessíveis**, se o IP
do servidor for bloqueado por duas delas o bot aborta na inicialização
com algo como:

```
RuntimeError: Arbitragem exige >= 2 corretoras acessíveis; disponíveis: ['okx'] (falharam: ['binance', 'bybit'])
```

**Antes de rodar, valide a conectividade do IP atual:**

```bash
venv/bin/python -c "
import asyncio, ccxt.pro as ccxtpro
async def test(name):
    ex = getattr(ccxtpro, name)({'enableRateLimit': True})
    try:
        await ex.load_markets()
        print(f'{name}: OK ({len(ex.markets)} mercados)')
    except Exception as e:
        print(f'{name}: FALHOU -> {type(e).__name__}: {str(e)[:120]}')
    finally:
        await ex.close()
async def main():
    for n in ['binance','okx','bybit']:
        await test(n)
asyncio.run(main())
"
```

**Soluções possíveis:**

1. Rodar a partir de um servidor/IP em região elegível (evite IPs de
   datacenter dos EUA para a `binance.com`).
2. Usar VPN/proxy em região permitida.
3. Habilitar no `config.yaml` apenas corretoras acessíveis a partir do
   IP atual (mínimo de 2).

Confira sempre os termos de cada corretora para a jurisdição do servidor
que você for usar.

Deixe rodando por alguns dias e depois avalie:

```bash
venv/bin/python analyze.py logs/
```

## Arquivos gerados (em `logs/`)

| Arquivo | Conteúdo |
|---|---|
| `opportunities.csv` | toda oportunidade detectada, executada ou não, com motivo da rejeição |
| `trades.csv` | operações simuladas com preços efetivos e P&L |
| `bot.log` | log geral (rotativo, 10 MB × 5) |

## Proteções implementadas

1. **Trava de modo live** — `mode: live` no yaml não basta; exige também
   `ARBBOT_CONFIRM_LIVE=yes` no ambiente, e a execução real está
   deliberadamente não implementada até a estratégia ser validada.
2. **Kill switch** — crie um arquivo chamado `STOP` na pasta do bot e ele
   para de operar imediatamente (continua logando). Apague para retomar.
3. **Dados velhos** — orderbook com mais de `max_book_age_ms` é rejeitado
   (protege contra feed congelado/atrasado).
4. **Sanidade de preço** — preço que desvia mais de
   `max_price_deviation_pct` da mediana das corretoras é rejeitado
   (protege contra feed corrompido).
5. **Spread suspeito** — lucro "bom demais" (> `suspicious_spread_pct`)
   não é operado: quase sempre é par suspenso, manutenção ou dado ruim.
6. **Circuit breaker de perda diária** — atingiu `max_daily_loss_usd`,
   para até o próximo dia UTC.
7. **Limite de operações/minuto** — protege contra loop descontrolado.
8. **Corretora degradada** — após N erros consecutivos de websocket, os
   dados daquela corretora são ignorados até reconectar.
9. **Checagem de liquidez** — só opera se o book tem profundidade para a
   ordem inteira.
10. **Checagem de saldo** — simula exatamente a restrição real de
    inventário por corretora.
11. **Cooldown por rota** — evita disparar repetidamente sobre o mesmo book.

## Como interpretar os resultados

- A coluna `net_profit_pct` do `opportunities.csv` já desconta as taxas.
  Some `net_profit_usd` das linhas com `executed=1` por dia.
- Compare com o P&L do `trades.csv` (inclui slippage simulado) — esse é
  o número mais próximo da realidade.
- Se a maior parte das rejeições for `below_min_profit`, o spread entre
  essas corretoras está estreito demais — normal em mercados eficientes
  com taxa de 0,1%. Reduzir a taxa (desconto BNB, tier VIP, ordens
  maker) muda completamente a viabilidade.
- Rode por **pelo menos 1–2 semanas**, incluindo períodos de
  volatilidade, antes de tirar conclusões.

## Avisos importantes

- **Restrição geográfica de IP**: Binance e Bybit bloqueiam IPs de
  regiões restritas (HTTP 451 / 403). É preciso rodar de um IP elegível
  ou via VPN/proxy, com pelo menos 2 corretoras acessíveis. Veja a seção
  *"Restrição geográfica de IP"* acima.
- Paper trading é **otimista por natureza**: no live existe latência de
  ordem, fila, rejeições e fills parciais. Considere os resultados do
  paper como teto, não como expectativa.
- As taxas no `config.yaml` (0,10% taker) são as padrão — confira as
  suas reais em cada corretora e ajuste, pois elas dominam o resultado.
- Arbitragem entre grandes corretoras em pares líquidos é disputada por
  players com colocation e taxas menores; spreads líquidos positivos são
  raros e rápidos. Os logs vão te mostrar exatamente isso, com dados.
