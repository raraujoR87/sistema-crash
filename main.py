# main.py — Ponto de entrada. Loop principal assíncrono.
# Modo 1 (padrão): simula ingestão com dados aleatórios (mock)
# Modo 2: passa --csv <arquivo> para replay de dados históricos ao vivo
# Modo 3: --ao-vivo para aguardar dados reais do scraper (Fase 2)

import asyncio
import logging
import random
import sys
from datetime import datetime
from pathlib import Path

import banco
from config import ALVO, MAX_GALE, LOG_PATH
from loader_csv import carregar
from memoria import Memoria
from notificador import Notificador
from orquestrador import Orquestrador

# ── Logging ───────────────────────────────────────────────────────────────────
Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("main")


# ── Estado da sessão ──────────────────────────────────────────────────────────
class EstadoSessao:
    def __init__(self):
        self.ativa       = False
        self.gale_atual  = 0
        self.stake_atual = 0.0
        self.sinal_id    = None
        self.ultimo_estado = "AGUARDAR"


async def processar_rodada(
    mult: float,
    ts: datetime,
    memoria: Memoria,
    orq: Orquestrador,
    notificador: Notificador,
    sessao: EstadoSessao,
):
    rodada = memoria.registrar(mult, ts)
    banco.gravar_rodada(rodada)

    sinal = orq.processar(memoria)

    # Mostrar apenas quando há mudança relevante de estado
    if sinal.estado != sessao.ultimo_estado or sinal.estado in ("ENTRAR", "ATENCAO", "ABORTAR"):
        await notificador.enviar(sinal.mensagem, sinal.estado)
        sessao.ultimo_estado = sinal.estado

    # ── Gestão de sessão de entrada ──────────────────────────────────────────
    if not sessao.ativa:
        if sinal.estado == "ENTRAR":
            sessao.ativa = True
            sessao.gale_atual = 0
            from config import STAKE_BASE
            sessao.stake_atual = STAKE_BASE
            sessao.sinal_id = banco.gravar_sinal(sinal, rodada.bloco_id)
            logger.info(f"Sessão ABERTA — alvo {ALVO}x | G{MAX_GALE} | R${sessao.stake_atual:.2f}")
    else:
        if mult >= ALVO:
            banco.gravar_resultado(sessao.sinal_id, True, mult, sessao.gale_atual)
            orq.risco.registrar_resultado(True)
            await notificador.enviar(
                f"[GREEN] {mult}x >= {ALVO}x — G{sessao.gale_atual} | Sessao encerrada.",
                "ENTRAR"
            )
            sessao.ativa = False
            sessao.gale_atual = 0
        else:
            sessao.gale_atual += 1
            if sessao.gale_atual > MAX_GALE:
                banco.gravar_resultado(sessao.sinal_id, False, mult, sessao.gale_atual - 1)
                orq.risco.registrar_resultado(False)
                await notificador.enviar(
                    f"[STOP] G{MAX_GALE} esgotado ({mult}x). Sessao encerrada.",
                    "ABORTAR"
                )
                sessao.ativa = False
                sessao.gale_atual = 0
            else:
                sessao.stake_atual *= 2
                logger.info(f"Red {mult}x — acionando G{sessao.gale_atual} (R${sessao.stake_atual:.2f})")


# ── Modo mock (sem CSV) ───────────────────────────────────────────────────────
async def loop_mock(memoria, orq, notificador, sessao):
    logger.info("Modo MOCK iniciado — dados aleatórios. Ctrl+C para parar.")
    while True:
        mult = round(random.choices(
            [random.uniform(1.0, 1.5), random.uniform(1.5, 3.0), random.uniform(3.0, 15.0)],
            weights=[0.35, 0.50, 0.15],
        )[0], 2)
        await processar_rodada(mult, datetime.now(), memoria, orq, notificador, sessao)
        await asyncio.sleep(3)


# ── Modo replay CSV ───────────────────────────────────────────────────────────
async def loop_csv(caminho: str, memoria, orq, notificador, sessao):
    logger.info(f"Modo REPLAY — arquivo: {caminho}")
    rodadas = carregar(caminho)

    # Calibrar agentes com 70% dos dados
    corte = int(len(rodadas) * 0.70)
    orq.carregar_historico(rodadas[:corte])
    logger.info(f"Agentes calibrados com {corte} rodadas históricas.")

    # Replay dos 30% restantes ao vivo
    for r in rodadas[corte:]:
        await processar_rodada(r.multiplicador, r.timestamp, memoria, orq, notificador, sessao)
        await asyncio.sleep(0.1)   # velocidade de replay (ajustar conforme necessário)

    logger.info("Replay concluído.")
    resumo = banco.resumo_sessao()
    print(f"\n{'='*50}")
    print(f"RESUMO DA SESSÃO DE HOJE")
    print(f"Sinais    : {resumo['total_sinais_hoje']}")
    print(f"Acertos   : {resumo['acertos']}  ({resumo['taxa_acerto']:.1%})")
    print(f"Gale médio: G{resumo['gale_medio']:.1f}")
    print(f"{'='*50}")


# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    banco.inicializar()
    memoria    = Memoria()
    orq        = Orquestrador()
    notificador = Notificador()
    sessao     = EstadoSessao()

    args = sys.argv[1:]

    if "--csv" in args:
        idx = args.index("--csv")
        caminho = args[idx + 1] if idx + 1 < len(args) else ""
        if not caminho:
            print("Uso: python main.py --csv <caminho_csv>")
            return
        await loop_csv(caminho, memoria, orq, notificador, sessao)
    else:
        await loop_mock(memoria, orq, notificador, sessao)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nSistema encerrado pelo usuário.")
