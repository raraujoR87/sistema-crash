# backtest.py — Simula o motor rodada a rodada sobre dados históricos.
# Gera relatório: P&L por bloco de 10 min, taxa de acerto, EV real.
#
# Uso: python backtest.py data/meus_dados.csv

import sys
from collections import defaultdict
from datetime import datetime

from loader_csv import carregar
from memoria import Memoria
from orquestrador import Orquestrador
from config import ALVOS_ATIVOS, MAX_GALE, STAKE_BASE
ALVO = ALVOS_ATIVOS[0]


def rodar_backtest(caminho_csv: str):
    rodadas = carregar(caminho_csv)
    if len(rodadas) < 50:
        print("Poucas rodadas para backtest significativo.")
        return

    # Dividir: 70% treino (calibração), 30% teste
    corte = int(len(rodadas) * 0.70)
    treino = rodadas[:corte]
    teste  = rodadas[corte:]

    print(f"\n{'='*60}")
    print(f"BACKTEST — Alvo {ALVO}x | Gale máx G{MAX_GALE} | Stake R${STAKE_BASE}")
    print(f"Treino: {len(treino)} rodadas | Teste: {len(teste)} rodadas")
    print(f"{'='*60}")

    # Inicializar motor com dados de treino
    orq = Orquestrador(alvo=ALVO)
    orq.carregar_historico(treino)

    mem = Memoria()
    for r in treino:
        mem.registrar(r.multiplicador, r.timestamp)

    # Métricas
    sinais_emitidos = 0
    acertos = 0
    perdas_totais = 0
    pl_total = 0.0
    por_bloco: dict[str, dict] = defaultdict(lambda: {"sinais": 0, "acertos": 0, "pl": 0.0})
    sessao_ativa = False
    gale_atual = 0
    stake_atual = STAKE_BASE

    print("\nSimulando fase de teste...\n")

    for i, rodada in enumerate(teste):
        mem.registrar(rodada.multiplicador, rodada.timestamp)
        sinal = orq.processar(mem)

        if not sessao_ativa:
            if sinal.estado == "ENTRAR":
                sessao_ativa = True
                gale_atual = 0
                stake_atual = STAKE_BASE
                sinais_emitidos += 1
                bloco = rodada.bloco_id

        if sessao_ativa:
            mult = rodada.multiplicador
            if mult >= ALVO:
                lucro = stake_atual * (ALVO - 1)
                pl_total += lucro
                acertos += 1
                por_bloco[bloco]["acertos"] += 1
                por_bloco[bloco]["pl"] += lucro
                por_bloco[bloco]["sinais"] += 1
                orq.registrar_resultado(1.0, True, mult, gale_atual)
                sessao_ativa = False
                gale_atual = 0
            else:
                pl_total -= stake_atual
                por_bloco[bloco]["pl"] -= stake_atual
                gale_atual += 1
                if gale_atual > MAX_GALE:
                    perdas_totais += 1
                    por_bloco[bloco]["sinais"] += 1
                    orq.registrar_resultado(1.0, False, mult, MAX_GALE)
                    sessao_ativa = False
                    gale_atual = 0
                    stake_atual = STAKE_BASE
                else:
                    stake_atual *= 2

    # Relatório
    taxa = acertos / sinais_emitidos if sinais_emitidos > 0 else 0
    print(f"Sinais emitidos : {sinais_emitidos}")
    print(f"Acertos (G0-G{MAX_GALE}): {acertos}  ({taxa:.1%})")
    print(f"Perdas totais   : {perdas_totais}")
    print(f"P&L total       : R$ {pl_total:.2f}")
    print(f"\n{'='*60}")
    print("Top 10 blocos por P&L:\n")

    blocos_sorted = sorted(por_bloco.items(), key=lambda x: x[1]["pl"], reverse=True)
    print(f"{'Bloco':<8} {'Sinais':>7} {'Acertos':>8} {'Taxa':>7} {'P&L':>10}")
    print("=" * 44)
    for bloco, m in blocos_sorted[:10]:
        s = m["sinais"]
        a = m["acertos"]
        t = a / s if s > 0 else 0
        print(f"{bloco:<8} {s:>7} {a:>8} {t:>7.1%} {m['pl']:>9.2f}")

    print(f"\n{'='*60}")
    print("5 piores blocos (evitar):\n")
    for bloco, m in blocos_sorted[-5:]:
        s = m["sinais"]
        a = m["acertos"]
        t = a / s if s > 0 else 0
        print(f"{bloco:<8} {s:>7} {a:>8} {t:>7.1%} {m['pl']:>9.2f}")

    print(f"\n{'='*60}")
    print("Backtest concluído.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python backtest.py <caminho_csv>")
        print("Exemplo: python backtest.py data/brabet_30k.csv")
        sys.exit(1)
    rodar_backtest(sys.argv[1])
