"""
retreinar.py — Pipeline de retreino diário dos agentes.

O que faz:
  1. Exporta todas as rodadas ao vivo do SQLite para CSV temporário
  2. Carrega o CSV histórico original (30k rodadas)
  3. Combina histórico + ao vivo em ordem cronológica
  4. Retreina AgenteTemporal, AgenteCovariancia, AgenteTemperatura
  5. Grava um log do retreino em data/retreino.log

Uso manual:  python retreinar.py
Uso via server.py: importado e chamado automaticamente a cada 24h (00:00 BRT)
"""

import sys, os, logging
from datetime import datetime
from pathlib import Path

# Garante que roda do diretório correto
ROOT = Path(__file__).parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import banco
from loader_csv import carregar
from memoria import Rodada

log = logging.getLogger("retreinar")


def retreinar(orq, verbose: bool = True) -> dict:
    """
    Retreina os agentes do orquestrador com dados combinados (histórico + ao vivo).

    Args:
        orq: instância de Orquestrador já existente no server.py
        verbose: se True, imprime progresso no console

    Returns:
        dict com estatísticas do retreino
    """
    inicio = datetime.now()
    Path("data").mkdir(exist_ok=True)
    csv_ao_vivo = "data/ao_vivo_retreino.csv"

    # ── 1. Exportar dados ao vivo do SQLite ────────────────────────────
    n_ao_vivo = banco.exportar_rodadas_para_retreino(csv_ao_vivo)
    if verbose:
        log.info(f"Exportadas {n_ao_vivo} rodadas ao vivo para {csv_ao_vivo}")

    # ── 2. Carregar CSV histórico original ────────────────────────────
    csv_historico = "data/brabet_30k.csv"
    rodadas_hist = []
    if Path(csv_historico).exists():
        rodadas_hist = carregar(csv_historico)
        if verbose:
            log.info(f"Histórico: {len(rodadas_hist)} rodadas de {csv_historico}")

    # ── 3. Carregar rodadas ao vivo exportadas ────────────────────────
    rodadas_ao_vivo = []
    if Path(csv_ao_vivo).exists() and n_ao_vivo > 0:
        rodadas_ao_vivo = _carregar_csv_ao_vivo(csv_ao_vivo)
        if verbose:
            log.info(f"Ao vivo: {len(rodadas_ao_vivo)} rodadas carregadas")

    # ── 4. Combinar e ordenar por timestamp ──────────────────────────
    todas = rodadas_hist + rodadas_ao_vivo
    todas.sort(key=lambda r: r.timestamp)

    # Usar 80% para treino (mais conservador que os 70% originais)
    corte = int(len(todas) * 0.80)
    treino = todas[:corte]

    if verbose:
        log.info(f"Total combinado: {len(todas)} | Treino: {len(treino)} rodadas")

    # ── 5. Retreinar agentes por Alvo ─────────────────────────────────
    from config import ALVOS_ATIVOS
    from orquestrador import Orquestrador
    
    # O orquestrador passado como argumento é ignorado neste novo fluxo,
    # pois instanciamos um novo orquestrador por alvo para serializar no banco.
    for alvo in ALVOS_ATIVOS:
        if verbose: log.info(f"Treinando modelo para alvo {alvo}x...")
        orq_treino = Orquestrador(alvo)
        orq_treino.temporal.carregar_historico_csv(treino)
        orq_treino.covariancia.carregar_historico_csv(treino)
        orq_treino.temperatura.carregar_historico_csv(treino)
        orq_treino.risco.carregar_historico_csv(treino)
        orq_treino.salvar_modelo()

    # ── 6. Log do retreino ────────────────────────────────────────────
    duracao = (datetime.now() - inicio).total_seconds()
    resultado = {
        "timestamp":   datetime.now().isoformat(),
        "n_historico": len(rodadas_hist),
        "n_ao_vivo":   len(rodadas_ao_vivo),
        "n_total":     len(todas),
        "n_treino":    len(treino),
        "duracao_s":   round(duracao, 2),
    }

    log_path = Path("data/retreino.log")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"{resultado}\n")

    if verbose:
        log.info(f"Retreino concluido em {duracao:.1f}s — {resultado}")

    return resultado


def _carregar_csv_ao_vivo(caminho: str) -> list[Rodada]:
    """Carrega o CSV exportado pelo banco.exportar_rodadas_para_retreino()."""
    import csv
    from datetime import datetime, timezone
    from memoria import Rodada

    rodadas = []
    with open(caminho, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            try:
                mult = float(row.get("Mult", 0))
                data = row.get("Data", "").strip()
                hora = row.get("Horario", "").strip()
                temp = int(row.get("Temperatura", 0) or 0)

                if data and hora:
                    try:
                        ts = datetime.strptime(f"{data} {hora}", "%d/%m/%Y %H:%M:%S")
                    except Exception:
                        ts = datetime.now()
                else:
                    ts = datetime.now()

                r = Rodada(multiplicador=mult, timestamp=ts, temperatura=temp)
                rodadas.append(r)
            except Exception:
                continue

    return rodadas


# ── Execução direta ────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    from orquestrador import Orquestrador
    banco.inicializar()
    orq = Orquestrador()
    resultado = retreinar(orq, verbose=True)
    print(f"\nReTreino concluido:")
    for k, v in resultado.items():
        print(f"  {k}: {v}")
