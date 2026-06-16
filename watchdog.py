#!/usr/bin/env python3
"""Watchdog de validação do bot de arbitragem.

Roda uma bateria determinística de checagens e imprime um relatório
estruturado. Pensado para ser chamado em loop (a cada ~10 min) por um
agente que só deve mexer no CÓDIGO quando houver falha de código real.

Categorias de resultado:
  PASS  — checagem ok
  WARN  — problema de AMBIENTE (IP/geo-bloqueio, rede, feed caído).
          NÃO é bug do bot; não deve disparar alteração de código.
  FAIL  — falha de CÓDIGO (selftest, CSV corrompido). Deve disparar
          correção pelo agente.

Exit code:
  0  nenhuma FAIL  (pode haver WARN)
  1  pelo menos uma FAIL

Uso:
  venv/bin/python watchdog.py            # valida e mantém o bot vivo
  venv/bin/python watchdog.py --no-start # não (re)inicia o bot
"""
import asyncio
import csv
import datetime as dt
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE, "logs")
PY = os.path.join(BASE, "venv", "bin", "python")
PID_FILE = os.path.join(BASE, "bot.pid")
STOP_FILE = os.path.join(BASE, "STOP")
VALIDATION_LOG = os.path.join(LOG_DIR, "validation.log")
STATUS_FILE = os.path.join(LOG_DIR, "STATUS.md")

# Idade máxima aceitável do bot.log (segundos) com o bot rodando.
MAX_LOG_AGE_SEC = 180
# Mínimo de corretoras acessíveis para a estratégia fazer sentido.
MIN_EXCHANGES = 2

results = []  # (status, nome, detalhe)


def add(status, name, detail=""):
    results.append((status, name, detail))
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))


# ----------------------------------------------------------------------
def check_selftest():
    """Pipeline offline (detecção, risco, execução, CSVs)."""
    try:
        r = subprocess.run([PY, os.path.join(BASE, "selftest.py")],
                           capture_output=True, text=True, timeout=120, cwd=BASE)
    except Exception as e:
        add("FAIL", "selftest", f"não executou: {e}")
        return
    if r.returncode == 0:
        add("PASS", "selftest", "todas as verificações do pipeline passaram")
    else:
        tail = (r.stdout + r.stderr).strip().splitlines()
        add("FAIL", "selftest", "; ".join(tail[-3:]) or "exit != 0")


def check_connectivity():
    """Conectividade real com as corretoras (depende do IP/região)."""
    try:
        import ccxt.pro as ccxtpro
    except Exception as e:
        add("FAIL", "import ccxt.pro", str(e))
        return

    async def probe(name):
        ex = getattr(ccxtpro, name)({"enableRateLimit": True})
        try:
            await asyncio.wait_for(ex.load_markets(), timeout=20)
            return name, True, f"{len(ex.markets)} mercados"
        except Exception as e:
            return name, False, f"{type(e).__name__}: {str(e)[:80]}"
        finally:
            try:
                await ex.close()
            except Exception:
                pass

    async def run_all():
        return await asyncio.gather(*(probe(n) for n in ("binance", "okx", "bybit")))

    try:
        res = asyncio.run(run_all())
    except Exception as e:
        add("WARN", "conectividade", f"erro inesperado: {e}")
        return

    ok = [n for n, good, _ in res if good]
    for n, good, detail in res:
        print(f"        - {n}: {'OK' if good else 'FALHOU'} ({detail})")
    if len(ok) >= MIN_EXCHANGES:
        add("PASS", "conectividade", f"{len(ok)}/3 acessíveis: {ok}")
    else:
        add("WARN", "conectividade",
            f"apenas {len(ok)}/3 acessíveis ({ok}) — geo-bloqueio/IP? "
            f"NÃO é bug do bot (veja README)")


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _read_pid():
    try:
        with open(PID_FILE) as f:
            return int(f.read().strip())
    except Exception:
        return None


def check_and_keep_bot_alive(allow_start):
    """Verifica se o bot está rodando; (re)inicia se permitido."""
    if os.path.exists(STOP_FILE):
        add("WARN", "processo do bot", "kill switch ativo (arquivo STOP existe)")
        return
    pid = _read_pid()
    if pid and _pid_alive(pid):
        add("PASS", "processo do bot", f"rodando (pid {pid})")
        return
    if not allow_start:
        add("WARN", "processo do bot", "não está rodando (--no-start)")
        return
    # (Re)inicia destacado, sem depender de setsid (ausente no macOS).
    os.makedirs(LOG_DIR, exist_ok=True)
    out = open(os.path.join(LOG_DIR, "run.out"), "ab")
    proc = subprocess.Popen(
        [PY, os.path.join(BASE, "run.py")],
        cwd=BASE, stdout=out, stderr=out,
        start_new_session=True)
    with open(PID_FILE, "w") as f:
        f.write(str(proc.pid))
    time.sleep(3)
    if _pid_alive(proc.pid):
        add("PASS", "processo do bot", f"(re)iniciado (pid {proc.pid})")
    else:
        add("FAIL", "processo do bot",
            "morreu logo após iniciar — veja logs/run.out")


def check_log_freshness():
    """Com o bot rodando, o bot.log deve estar sendo atualizado."""
    path = os.path.join(LOG_DIR, "bot.log")
    if not os.path.exists(path):
        add("WARN", "frescor do log", "bot.log ainda não existe")
        return
    age = time.time() - os.path.getmtime(path)
    pid = _read_pid()
    running = bool(pid and _pid_alive(pid))
    if not running:
        add("WARN", "frescor do log", "bot não está rodando; pulando")
        return
    if age <= MAX_LOG_AGE_SEC:
        add("PASS", "frescor do log", f"atualizado há {int(age)}s")
    else:
        add("WARN", "frescor do log",
            f"sem atualização há {int(age)}s (feed travado/desconectado?)")

    # Erros de websocket recentes são problema de ambiente (WARN).
    try:
        with open(path, errors="replace") as f:
            tail = f.readlines()[-60:]
    except Exception:
        return
    ws_err = sum(1 for l in tail if "Erro no websocket" in l)
    tracebacks = sum(1 for l in tail if "Traceback (most recent call last)" in l
                     or "Erro no loop do detector" in l)
    if tracebacks:
        add("FAIL", "exceções no bot.log",
            f"{tracebacks} traceback(s)/erro(s) de loop nas últimas linhas")
    elif ws_err >= 4:
        add("WARN", "websockets", f"{ws_err} erros de ws recentes (reconexão/IP)")


def check_csv_sanity():
    """CSVs devem ter cabeçalho válido e linhas parseáveis."""
    specs = {
        "opportunities.csv": "timestamp_utc",
        "trades.csv": "timestamp_utc",
    }
    for fname, first_col in specs.items():
        path = os.path.join(LOG_DIR, fname)
        if not os.path.exists(path):
            add("WARN", f"{fname}", "ainda não criado")
            continue
        try:
            with open(path, newline="") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                rows = sum(1 for _ in reader)
            if not header or header[0] != first_col:
                add("FAIL", f"{fname}", f"cabeçalho inesperado: {header}")
            else:
                add("PASS", f"{fname}", f"{rows} linha(s) de dados")
        except Exception as e:
            add("FAIL", f"{fname}", f"ilegível: {e}")


# ----------------------------------------------------------------------
def main():
    allow_start = "--no-start" not in sys.argv
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"=== Watchdog — {now} ===")

    check_selftest()
    check_csv_sanity()
    check_and_keep_bot_alive(allow_start)
    check_log_freshness()
    check_connectivity()

    fails = [r for r in results if r[0] == "FAIL"]
    warns = [r for r in results if r[0] == "WARN"]
    verdict = "FAIL" if fails else ("WARN" if warns else "PASS")

    print(f"\n=== Veredito: {verdict} "
          f"({len(fails)} FAIL, {len(warns)} WARN, "
          f"{len(results) - len(fails) - len(warns)} PASS) ===")

    # Relatório persistente para inspeção humana/agente.
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(VALIDATION_LOG, "a") as f:
        f.write(f"{now}  {verdict}  "
                f"fail={[r[1] for r in fails]} warn={[r[1] for r in warns]}\n")
    with open(STATUS_FILE, "w") as f:
        f.write(f"# Status da validação\n\n_{now}_\n\n")
        f.write(f"**Veredito: {verdict}**\n\n")
        for status, name, detail in results:
            f.write(f"- `{status}` **{name}** — {detail}\n")
        if fails:
            f.write("\n## Ação esperada\n\nHá falha(s) de CÓDIGO — "
                    "corrigir em branch + PR.\n")
        elif warns:
            f.write("\n## Observação\n\nApenas avisos de ambiente "
                    "(IP/rede/feed). Não alterar o código por isso.\n")

    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
