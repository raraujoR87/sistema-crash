# server.py — Servidor central: SSE tipminer → 5 agentes → WebSocket dashboard
import asyncio, json, logging, sys, ssl, math
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from aiohttp import web

import config as cfg
from loader_csv import carregar
from memoria import Memoria
from orquestrador import Orquestrador
from retreinar import retreinar
import banco

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("server")

LIVE_URL = "https://api.core.public.tipminer.com/v1/crash/rounds/a339589c-d6f2-41ea-b7b1-8835f2bdeee6/live"
CSV_PATH = "data/brabet_30k.csv"
PORT     = 8765
SSL_CTX  = ssl.create_default_context()

# ── Estado global ─────────────────────────────────────────────────────────────
memoria  = Memoria()
orquestradores = {alvo: Orquestrador(alvo) for alvo in cfg.ALVOS_ATIVOS}
clientes: set[web.WebSocketResponse] = set()
historico_rodadas: list[dict] = []
_n_hist_treino = 0
_n_vivo_treino = 0
_atencao_consecutivos = 0   # rastreia rounds em ATENCAO para antecedência do Telegram

# ── Config de estratégia de recuperação ───────────────────────────────────────
estrategia_cfg = {
    "nome":          "martingale",   # flat|dalembert|fibonacci|martingale|masaniello
    "recuperacao":   "total",        # total|parcial
    "meta_recuperar": 100.0,         # % do prejuízo a recuperar (100=total)
    "session_n":     10,             # Masaniello: tamanho da sessão
    "session_t":     20.0,           # Masaniello: lucro-alvo da sessão (R$)
    "stakes_custom": [],             # Stakes manuais G0,G1,G2... (override tudo)
    "alerta_antecedencia": 1,        # Rounds de ATENCAO antes de alertar (1 round)
    "telegram_modo": "antecedencia", # "entrada" | "antecedencia"
    "stats_intervalo_min": 30,       # intervalo para envio periódico (minutos)
    "telegram_eventos": {
        "entrar":    True,
        "resultado": True,
        "gale":      True,
        "atencao":   True,
    },
    "modo_manutencao": False,
}

# Sequência Fibonacci para até 8 níveis
_FIB = [1, 1, 2, 3, 5, 8, 13, 21]

# ── Cálculo dinâmico de progressão de gale por alvo ──────────────────────────
def _max_gale_para_alvo(alvo: float) -> int:
    """Número recomendado de gales conforme o alvo:
    - Alvos baixos (≤1.5x): prob alta → 3 gales cobrem 98%+ dos casos
    - Alvos médios (≤2x):   prob ~50% → 2 gales
    - Alvos altos (>2x):    prob baixa → 1 gale (evitar exposição excessiva)
    """
    if alvo <= 1.50: return 3
    if alvo <= 2.00: return 2
    if alvo <= 3.00: return 1
    return 1

def _progressao_recuperacao(alvo: float, stake_base: float, max_gale: int | None = None) -> list[float]:
    """Calcula stakes para cada nível de gale que garantem recuperar TODAS as
    perdas anteriores e ainda gerar o lucro original (stake_base * (alvo-1)).

    Fórmula:  stake_k = (perdas_acumuladas + lucro_alvo) / (alvo - 1)
    """
    if max_gale is None:
        max_gale = _max_gale_para_alvo(alvo)
    p = alvo - 1.0          # profit ratio por unidade apostada
    lucro_alvo = stake_base * p
    stakes = [round(stake_base, 2)]
    for _ in range(max_gale):
        perdas_acum = sum(stakes)
        proximo = (perdas_acum + lucro_alvo) / p
        stakes.append(round(proximo, 2))
    return stakes  # stakes[0]=G0, stakes[1]=G1, etc.

def _stake_para_nivel(nivel: int, base: float, max_nivel: int) -> float:
    """Calcula stake para o nível de gale conforme a estratégia configurada."""
    nome = estrategia_cfg["nome"]
    custom = estrategia_cfg["stakes_custom"]
    if custom and nivel < len(custom):
        return float(custom[nivel])
    if nome == "flat":
        return base
    if nome == "dalembert":
        return base * (1 + nivel)
    if nome == "fibonacci":
        return base * _FIB[min(nivel, len(_FIB) - 1)]
    if nome == "masaniello":
        # Masaniello: cada aposta = T / (N × p) onde p=taxa histórica
        n  = max(estrategia_cfg["session_n"], 1)
        t  = estrategia_cfg["session_t"]
        p  = 0.60  # proxy; idealmente vem do orquestrador ativo
        s  = t / (n * max(p, 0.01))
        # Progressão suave dentro da sessão
        return round(s * (1 + nivel * 0.25), 2)
    # Martingale (padrão)
    return base * (2 ** nivel)

# ── Estado de sessão ──────────────────────────────────────────────────────────
class Sessao:
    def __init__(self):
        self.ativa             = False
        self.alvo_atual        = cfg.ALVOS_ATIVOS[0]
        self.gale_nivel        = 0
        self._progressao: list[float] = []   # stakes G0,G1,G2... para o ciclo atual
        self.stake_atual       = cfg.STAKE_BASE
        self.pl_sessao         = 0.0    # P&L acumulado da sessão (não zera)
        self.pl_ciclo          = 0.0    # lucro/prejuízo do ciclo aberto (reseta ao abrir)
        self.entradas          = 0
        self.greens            = 0
        self.stops             = 0
        self.greens_por_gale   = {}
        self.greens_consecutivos = 0
        self.motivo_espera     = ""
        self.modo_recuperacao  = False
        self.deficit           = 0.0    # valor a recuperar (prejuízo acumulado)
        self.recuperado        = 0.0    # quanto já foi recuperado no modo recuperação
        self.masaniello_rodada = 0
        # Peak tracking para recuperação ao valor máximo atingido
        self._pl_peak          = 0.0    # maior P&L já registrado na sessão
        self.rodadas_obs       = 0      # total de rodadas observadas (incrementa a cada round)
        self.ultima_data       = ""     # armazena a data atual (YYYY-MM-DD) para zerar no dia seguinte

    def verificar_virada_dia(self, ts: datetime):
        data_atual = ts.strftime("%Y-%m-%d")
        if self.ultima_data and data_atual != self.ultima_data:
            log.info("Sessao: Virada de dia detectada. Zerando contadores estatísticos.")
            self.pl_sessao = 0.0
            self.entradas = 0
            self.greens = 0
            self.stops = 0
            self.greens_por_gale = {}
            self.greens_consecutivos = 0
            self.modo_recuperacao = False
            self.deficit = 0.0
            self.recuperado = 0.0
            self.masaniello_rodada = 0
            self._pl_peak = 0.0
            self.rodadas_obs = 0
            # Nao encerramos o ciclo atual (pl_ciclo, gale_nivel) para nao quebrar gales abertos
        self.ultima_data = data_atual
        self.salvar_estado()

    def _calcular_progressao(self, alvo: float) -> list[float]:
        """Usa gale matemático correto para o alvo escolhido.
        Estratégia custom ou Masaniello substituem o cálculo padrão.
        """
        custom = estrategia_cfg.get("stakes_custom", [])
        if custom:
            return [float(v) for v in custom]
        nome  = estrategia_cfg.get("nome", "martingale")
        # Respeita MAX_GALE configurado pelo usuário; usa heurística só como fallback
        max_g = cfg.MAX_GALE if cfg.MAX_GALE >= 0 else _max_gale_para_alvo(alvo)
        base  = cfg.STAKE_BASE
        if nome == "flat":
            return [base] * (max_g + 1)
        if nome == "fibonacci":
            return [round(base * _FIB[min(i, len(_FIB)-1)], 2) for i in range(max_g + 1)]
        if nome == "dalembert":
            return [round(base * (1 + i), 2) for i in range(max_g + 1)]
        if nome == "masaniello":
            n = max(estrategia_cfg.get("session_n", 10), 1)
            t = estrategia_cfg.get("session_t", 20.0)
            p = 0.60
            s = t / (n * max(p, 0.01))
            return [round(s * (1 + i * 0.25), 2) for i in range(max_g + 1)]
        # Martingale com recuperação matemática exata
        return _progressao_recuperacao(alvo, base, max_g)

    def iniciar(self, alvo: float):
        self.ativa          = True
        self.alvo_atual     = alvo
        self.gale_nivel     = 0
        self._progressao    = self._calcular_progressao(alvo)
        self.stake_atual    = self._progressao[0]
        self.pl_ciclo       = 0.0
        self.masaniello_rodada += 1
        self.salvar_estado()

    @property
    def max_gale(self) -> int:
        return max(len(self._progressao) - 1, 0)

    def resultado_green(self):
        lucro_bruto  = self.stake_atual * (self.alvo_atual - 1.0)
        perdas_ciclo = sum(self._progressao[:self.gale_nivel])  # stakes G0..Gk-1
        ganho_liq    = lucro_bruto - perdas_ciclo
        self.pl_sessao += ganho_liq
        self._pl_peak   = max(self._pl_peak, self.pl_sessao)
        self.greens    += 1
        self.greens_consecutivos += 1
        self.entradas  += 1
        self.greens_por_gale[self.gale_nivel] = self.greens_por_gale.get(self.gale_nivel, 0) + 1
        self.ativa       = False
        self.gale_nivel  = 0
        self.pl_ciclo    = 0.0
        # Atualiza modo recuperação
        if self.modo_recuperacao:
            self.recuperado += max(ganho_liq, 0)
            meta = self.deficit * estrategia_cfg.get("meta_recuperar", 100.0) / 100
            if self.recuperado >= meta:
                self.modo_recuperacao = False
                self.deficit    = 0.0
                self.recuperado = 0.0
        self._progressao = self._calcular_progressao(self.alvo_atual)
        self.stake_atual = self._progressao[0]
        self.salvar_estado()

    def resultado_stop(self):
        perdas_total   = sum(self._progressao[:self.gale_nivel + 1])
        self.pl_sessao -= perdas_total
        self.pl_ciclo   = 0.0
        self.stops     += 1
        self.greens_consecutivos = 0
        self.entradas  += 1
        self.ativa       = False
        self.gale_nivel  = 0
        # Entra em modo de recuperação se P&L negativo ou abaixo do peak
        if self.pl_sessao < 0 and not self.modo_recuperacao:
            self.modo_recuperacao = True
            self.deficit    = abs(self.pl_sessao)
            self.recuperado = 0.0
        elif self.modo_recuperacao:
            # Atualiza déficit acumulado
            self.deficit = abs(min(self.pl_sessao, 0))
        self._progressao = self._calcular_progressao(self.alvo_atual)
        self.stake_atual = self._progressao[0]
        self.salvar_estado()

    def avancar_gale(self):
        self.gale_nivel += 1
        if self.gale_nivel < len(self._progressao):
            self.stake_atual = self._progressao[self.gale_nivel]
        else:
            # Fallback: continua a progressão matematicamente
            stake_extra = _progressao_recuperacao(self.alvo_atual, cfg.STAKE_BASE, self.gale_nivel)
            self.stake_atual = stake_extra[-1] if stake_extra else cfg.STAKE_BASE
        self.salvar_estado()

    def salvar_estado(self):
        try:
            import json as _json
            import banco as _banco
            dados = {
                "pl_sessao":        self.pl_sessao,
                "entradas":         self.entradas,
                "greens":           self.greens,
                "stops":            self.stops,
                "greens_por_gale":  {str(k): v for k, v in self.greens_por_gale.items()},
                "greens_consecutivos": self.greens_consecutivos,
                "modo_recuperacao": self.modo_recuperacao,
                "deficit":          self.deficit,
                "recuperado":       self.recuperado,
                "masaniello_rodada": self.masaniello_rodada,
                "_pl_peak":          self._pl_peak,
                "alvo_atual":        self.alvo_atual,
                "rodadas_obs":       self.rodadas_obs,
                "ultima_data":       self.ultima_data,
            }
            _banco.salvar_config("sessao_estado", _json.dumps(dados))
        except Exception as e:
            log.warning(f"Sessao: falha ao salvar estado — {e}")

    def carregar_estado(self):
        try:
            import json as _json
            import banco as _banco
            val = _banco.ler_config("sessao_estado")
            if val:
                dados = _json.loads(val)
                self.pl_sessao         = float(dados.get("pl_sessao", 0.0))
                self.entradas          = int(dados.get("entradas", 0))
                self.greens            = int(dados.get("greens", 0))
                self.greens_consecutivos = int(dados.get("greens_consecutivos", 0))
                self.stops             = int(dados.get("stops", 0))
                self.greens_por_gale   = {int(k): int(v) for k, v in dados.get("greens_por_gale", {}).items()}
                self.modo_recuperacao  = bool(dados.get("modo_recuperacao", False))
                self.deficit           = float(dados.get("deficit", 0.0))
                self.recuperado        = float(dados.get("recuperado", 0.0))
                self.masaniello_rodada = int(dados.get("masaniello_rodada", 0))
                self._pl_peak          = float(dados.get("_pl_peak", 0.0))
                self.alvo_atual        = float(dados.get("alvo_atual", self.alvo_atual))
                self.rodadas_obs       = int(dados.get("rodadas_obs", 0))
                self.ultima_data       = dados.get("ultima_data", "")
                # Re-calcula progressão com base no alvo restaurado
                self._progressao       = self._calcular_progressao(self.alvo_atual)
                self.stake_atual       = self._progressao[0]
                log.info(f"Sessao: estado restaurado do banco (P&L=R${self.pl_sessao:.2f})")
        except Exception as e:
            log.warning(f"Sessao: falha ao carregar estado — {e}")

    def to_dict(self):
        win_rate = (self.greens / self.entradas * 100) if self.entradas else 0
        meta_rec = self.deficit * estrategia_cfg.get("meta_recuperar", 100.0) / 100
        prog     = [round(s, 2) for s in self._progressao]
        return {
            "ativa":            self.ativa,
            "alvo_atual":       self.alvo_atual,
            "gale_nivel":       self.gale_nivel,
            "max_gale":         self.max_gale,
            "stake_atual":      round(self.stake_atual, 2),
            "progressao":       prog,           # G0,G1,G2... para o ciclo atual
            "pl_sessao":        round(self.pl_sessao, 2),
            "pl_peak":          round(self._pl_peak, 2),
            "entradas":         self.entradas,
            "greens":           self.greens,
            "stops":            self.stops,
            "win_rate":         round(win_rate, 2),
            "motivo_espera":    self.motivo_espera,
            "stake_base":       cfg.STAKE_BASE,
            "estrategia":       estrategia_cfg.get("nome", "martingale"),
            "modo_recuperacao": self.modo_recuperacao,
            "deficit":          round(self.deficit, 2),
            "recuperado":       round(self.recuperado, 2),
            "meta_recuperar":   round(meta_rec, 2),
            **{f"greens_g{k}": v for k, v in self.greens_por_gale.items()},
            "rodadas_obs":      self.rodadas_obs,
        }

sessao = Sessao()


async def broadcast(msg: dict):
    texto = json.dumps(msg, ensure_ascii=False)
    mortos = set()
    for ws in list(clientes):
        try:
            await ws.send_str(texto)
        except Exception:
            mortos.add(ws)
    clientes.difference_update(mortos)


async def loop_retreino():
    """Retreat automático diariamente às 00:00 (horário local)."""
    while True:
        agora = datetime.now()
        # Próxima meia-noite
        proxima = agora.replace(hour=0, minute=0, second=0, microsecond=0)
        if proxima <= agora:
            from datetime import timedelta
            proxima = proxima + timedelta(days=cfg.RETREINO_DIAS)
        espera = (proxima - agora).total_seconds()
        log.info(f"Proximo retreino agendado em {espera/3600:.1f}h ({proxima.strftime('%d/%m %H:%M')})")
        await asyncio.sleep(espera)
        try:
            # Passamos None porque orquestradores agora é um dict. 
            # O retreinar.py instância internamente.
            resultado_retreino = retreinar(None, verbose=True)
            await broadcast({
                "tipo": "retreino",
                "resultado": resultado_retreino,
                "msg": f"Retreino diario concluido: {resultado_retreino['n_total']} rodadas ({resultado_retreino['n_ao_vivo']} ao vivo)",
            })
            log.info(f"Retreino automatico concluido: {resultado_retreino}")
        except Exception as e:
            log.error(f"Erro no retreino automatico: {e}")


async def processar_round(resultado: float, instant: str, tipo: str, temp: int):
    ts = datetime.fromisoformat(instant.replace("Z", "+00:00")).astimezone()
    rodada = memoria.registrar(resultado, ts, temperatura=temp)
    sessao.rodadas_obs += 1
    sessao.verificar_virada_dia(ts)
    banco.gravar_rodada(rodada)

    # Se a sessão estiver ativa, ficamos presos ao alvo dela. Senão, testamos todos e pegamos o melhor.
    melhor_alvo = sessao.alvo_atual if sessao.ativa else None
    melhor_sinal = None
    melhor_ev = -float('inf')
    
    # Processa todos os orquestradores
    resultados_orq = {}
    for alvo, orq in orquestradores.items():
        s = orq.processar(memoria)
        resultados_orq[alvo] = s
        
        # Pega EV (ou usa score como fallback)
        ev = s.dados_agentes.get("risco", {}).get("ev", 0)
        
        # Se não há sessão ativa, escolhemos o alvo com maior EV (ou maior score se der empate)
        if not sessao.ativa:
            if ev > melhor_ev or (ev == melhor_ev and (melhor_sinal is None or s.score_final > melhor_sinal.score_final)):
                melhor_ev = ev
                melhor_alvo = alvo
                melhor_sinal = s

    # Sinal escolhido para operar/mostrar no painel
    sinal = resultados_orq[melhor_alvo]
    orq_ativo = orquestradores[melhor_alvo]
    
    # Grava o sinal gerado associando-o ao timestamp da rodada atual
    banco.gravar_sinal(sinal, rodada.bloco_id, rodada.timestamp.isoformat())

    pesos_map = {
        "streak":      cfg.PESO_AGENTE_STREAK,
        "margem":      cfg.PESO_AGENTE_MARGEM,
        "temporal":    cfg.PESO_AGENTE_TEMPORAL,
        "covariancia": cfg.PESO_AGENTE_COVARIANCA,
        "temperatura": cfg.PESO_AGENTE_TEMPERATURA,
        "risco":       cfg.PESO_AGENTE_RISCO,
        "ia_gemini":   cfg.PESO_AGENTE_IA,
        "rtp":         cfg.PESO_AGENTE_RTP,
        "tendencia":   0.10,
        "bloco":       0.12,
        "padrao":      0.13,
    }

    vereditos_raw = [
        {
            "agente": v.agente,
            "score":  round(v.score, 3),
            "estado": v.estado,
            "motivo": v.motivo,
            "peso":   round(pesos_map.get(v.agente, 0.0), 2),
            "dados":  v.dados if hasattr(v, "dados") else {},
        }
        for v in sinal.vereditos
    ]

    # ── Lógica de sessão automática ───────────────────────────────────────────
    if estrategia_cfg.get("modo_manutencao", False):
        acao = {
            "tipo": "manutencao",
            "estado": "MANUTENÇÃO",
            "titulo": "MODO MANUTENÇÃO ATIVO",
            "sub": "O sistema está em modo de manutenção. Operações e entradas suspensas.",
            "cor": "red"
        }
    else:
        acao = _calcular_acao(resultado, sinal, melhor_alvo, orq_ativo, vereditos_raw)

    entrada_hist = {
        "result":  resultado,
        "type":    tipo,
        "temp":    temp,
        "instant": instant,
        "bloco":   rodada.bloco_id,
        "cat":     cfg.classificar_rodada(resultado, melhor_alvo),
    }
    historico_rodadas.append(entrada_hist)
    if len(historico_rodadas) > 30:
        historico_rodadas.pop(0)

    payload = {
        "tipo":      "round",
        "round":     entrada_hist,
        "historico": historico_rodadas[-15:],
        "vereditos": vereditos_raw,
        "sinal": {
            "estado":   sinal.estado,
            "score":    sinal.score_final,
            "mensagem": sinal.mensagem,
            "dados":    sinal.dados_agentes,
        },
        "inteligencia_global": orq_ativo.obter_dados_inteligencia_global(),
        "acao":   acao,
        "sessao": sessao.to_dict(),
        "stats": {
            "total_mem":      memoria.total(),
            "bloco_atual":    memoria.bloco_atual(),
            "alvo":           melhor_alvo,
            "max_gale":       cfg.MAX_GALE,
            "stake_base":     cfg.STAKE_BASE,
            "ultimo_retreino": orq_ativo._ultimo_retreino.strftime("%d/%m %H:%M") if orq_ativo._ultimo_retreino else "—",
            "n_hist_treino":  _n_hist_treino,
            "n_vivo_treino":  _n_vivo_treino,
            "calibracao":     orq_ativo.estatisticas_calibracao(),
            "pausa_red":      orq_ativo._rounds_de_pausa_pos_red,
        },
    }

    # Monta convergência estruturada para o dashboard
    conv = _calcular_convergencia(sinal, vereditos_raw)
    payload["convergencia"] = conv

    # Blame report + heatmap de blocos
    payload["blame_report"]    = orq_ativo.padrao.blame_report()
    payload["bloco_relatorio"] = orq_ativo.bloco.relatorio_blocos()

    await broadcast(payload)
    log.info(f"Round {resultado}x [{tipo}] -> {sinal.estado} score={sinal.score_final:.3f} | acao={acao['tipo']}")

    # Controle de antecedência do Telegram
    global _atencao_consecutivos
    modo_tg = estrategia_cfg.get("telegram_modo", "entrada")
    if sinal.estado == "ATENCAO" and not sessao.ativa:
        _atencao_consecutivos += 1
    else:
        _atencao_consecutivos = 0

    # Decide quando enviar alertas ao Telegram
    tg_eventos = estrategia_cfg.get("telegram_eventos", {})
    antec       = estrategia_cfg.get("alerta_antecedencia", 0)

    if acao["tipo"] == "resultado" and tg_eventos.get("resultado", True):
        asyncio.create_task(_telegram_notificar_acao(acao, sinal=sinal, alvo=melhor_alvo))

    elif acao["tipo"] == "gale" and tg_eventos.get("gale", False):
        asyncio.create_task(_telegram_notificar_acao(acao, sinal=sinal, alvo=melhor_alvo))

    elif acao["tipo"] == "entrar" and tg_eventos.get("entrar", True):
        # Envia sinal de entrada SEMPRE que a ação for entrar, independente do modo
        asyncio.create_task(_telegram_notificar_acao(acao, sinal=sinal, alvo=melhor_alvo))
        # Se for um sinal forte, envia mensagem de marketing focado na convergência e nos próximos minutos
        if sinal.score_final >= 0.65:
            asyncio.create_task(_telegram_enviar_marketing("entrada_forte"))

    elif acao["tipo"] == "atencao":
        # Modo antecedência: envia alerta prévio quando acumula N rounds de ATENCAO
        if (modo_tg == "antecedencia" and antec > 0
                and _atencao_consecutivos >= antec
                and tg_eventos.get("atencao", False)):
            asyncio.create_task(_telegram_notificar_acao(acao, sinal=sinal, alvo=melhor_alvo))




def _salvar_padrao_state():
    """Persiste estado do AgentePadrao e AgenteBloco em disco."""
    try:
        import json as _json
        from pathlib import Path as _Path
        base = _Path(__file__).parent
        orq_ref = orquestradores.get(sessao.alvo_atual)
        if orq_ref is None:
            return
        base.joinpath("padrao_state.json").write_text(
            _json.dumps(orq_ref.padrao.to_dict(), default=str), "utf-8")
        base.joinpath("bloco_state.json").write_text(
            _json.dumps(orq_ref.bloco.to_dict(), default=str), "utf-8")
    except Exception as e:
        log.warning(f"Estado: falha ao salvar — {e}")


def _calcular_acao(resultado: float, sinal, alvo: float, orq_ref, vereditos_raw: list = None) -> dict:
    """Decide a ação do jogador com base no resultado desta rodada e no sinal."""
    vereditos_raw = vereditos_raw or []

    # Resolve resultado de sessão ativa primeiro
    if sessao.ativa:
        if resultado >= sessao.alvo_atual:
            gale_final = sessao.gale_nivel
            sessao.resultado_green()
            # Feedback positivo → AgentePadrao e AgenteBloco aprendem
            orq_ref.registrar_resultado(sinal.score_final, acertou=True, mult_real=resultado, gale_nivel=gale_final)
            _salvar_padrao_state()
            # Marketing: celebração pós-green apenas se greens_consecutivos >= 3
            if sessao.greens_consecutivos >= 3:
                asyncio.create_task(_telegram_enviar_marketing("vitoria"))
            return {
                "tipo":   "resultado",
                "estado": "GREEN",
                "titulo": f"GREEN! {resultado:.2f}x",
                "sub":    f"Lucro na entrada G{gale_final}. Sessao encerrada.",
                "cor":    "green",
                "resultado": resultado,
                "gale_nivel": gale_final,
            }
        else:
            if sessao.gale_nivel < sessao.max_gale:
                sessao.avancar_gale()
                prog_str = " → ".join(f"G{i}=R${s:.0f}" for i,s in enumerate(sessao._progressao))
                return {
                    "tipo":   "gale",
                    "estado": f"GALE {sessao.gale_nivel}",
                    "titulo": f"GALE {sessao.gale_nivel} — Aposte R${sessao.stake_atual:.0f}",
                    "sub":    f"Resultado: {resultado:.2f}x < {sessao.alvo_atual}x | {prog_str}",
                    "cor":    "amber",
                    "resultado": resultado,
                    "gale_nivel": sessao.gale_nivel,
                }
            else:
                gale_nivel_stop = sessao.gale_nivel
                perdas_str = f"R${sum(sessao._progressao):.0f}"
                sessao.resultado_stop()
                # Feedback negativo → AgentePadrao e AgenteBloco aprendem
                orq_ref.registrar_resultado(sinal.score_final, acertou=False, mult_real=resultado, gale_nivel=gale_nivel_stop)
                _salvar_padrao_state()
                # Marketing: alerta pós-stop
                asyncio.create_task(_telegram_enviar_marketing("stop"))
                return {
                    "tipo":   "resultado",
                    "estado": "STOP",
                    "titulo": f"STOP — G{gale_nivel_stop} esgotado",
                    "sub":    f"Resultado: {resultado:.2f}x. Prejuízo ciclo: {perdas_str}",
                    "cor":    "red",
                    "resultado": resultado,
                    "gale_nivel": gale_nivel_stop,
                }

    # Período de aquecimento — bloqueia entradas até ter contexto ao vivo suficiente
    warmup_faltam = sinal.dados_agentes.get("_warmup_faltam", 0)
    if warmup_faltam > 0:
        return {
            "tipo":   "aguardar",
            "estado": f"AQUECENDO {warmup_faltam}",
            "titulo": f"Aquecendo sistema — {warmup_faltam} rounds restantes",
            "sub":    f"Aguardando contexto ao vivo suficiente para análise confiável (min 20 rounds)",
            "cor":    "gray",
            "_warmup": warmup_faltam,
        }

    # Sessão inativa — avaliar novo sinal
    if sinal.estado == "ENTRAR":
        sessao.iniciar(alvo)
        # Registra entrada nos agentes de feedback (padrao + bloco)
        _ts_entrada = datetime.now()
        orq_ref.padrao.registrar_entrada(
            sinal.dados_agentes.get("padrao", {}).get("fv", []),
            _ts_entrada,
            vereditos=vereditos_raw,
        )
        orq_ref.bloco.registrar_entrada(_ts_entrada)
        n_favor = sum(1 for v in vereditos_raw if v["estado"] in ("ENTRAR","ATENCAO"))
        return {
            "tipo":   "entrar",
            "estado": "ENTRAR",
            "titulo": f"ENTRAR — R${cfg.STAKE_BASE:.0f} em {alvo}x",
            "sub":    f"Ultima rodada: {resultado:.2f}x | Consenso: {n_favor}/6 favoraveis | Score {sinal.score_final:.3f}",
            "cor":    "green",
            "resultado": resultado,
        }
    elif sinal.estado == "ATENCAO":
        motivo = _motivo_atencao(sinal, vereditos_raw)
        sessao.motivo_espera = motivo
        return {
            "tipo":   "atencao",
            "estado": "ATENCAO",
            "titulo": f"PREPARAR — Sinal se formando",
            "sub":    f"Ultima rodada: {resultado:.2f}x | {motivo}",
            "cor":    "amber",
        }
    elif sinal.estado == "ABORTAR":
        motivo = _motivo_abortar(sinal, vereditos_raw)
        sessao.motivo_espera = motivo
        return {
            "tipo":   "abortar",
            "estado": "NAO ENTRAR",
            "titulo": "NAO ENTRAR — Risco elevado",
            "sub":    f"Ultima rodada: {resultado:.2f}x | {motivo}",
            "cor":    "red",
        }
    else:
        motivo = _motivo_aguardar(sinal, vereditos_raw)
        sessao.motivo_espera = motivo
        return {
            "tipo":   "aguardar",
            "estado": "AGUARDAR",
            "titulo": "AGUARDAR",
            "sub":    f"Ultima rodada: {resultado:.2f}x | {motivo}",
            "cor":    "gray",
        }


def _motivo_aguardar(sinal, vereditos_raw: list) -> str:
    dados = sinal.dados_agentes or {}
    streak = dados.get("streak_atual", 0)
    ema    = dados.get("ema_rapida")
    bloqueios = []
    positivos = []

    if streak == 0:
        bloqueios.append("sem compressão de reds")
    for v in vereditos_raw:
        if v["estado"] in ("ENTRAR","ATENCAO"):
            positivos.append(f"{v['agente'].upper()}({v['score']:.2f})")
        if v["agente"] == "temperatura" and v["dados"].get("temp_atual",0) <= 1:
            bloqueios.append("temp mínima (retenção severa)")
    if ema and ema > cfg.LIMIAR_MERCADO_SOLTO:
        bloqueios.append(f"mercado solto (EMA={ema:.2f})")

    partes = []
    if bloqueios:
        partes.append("Bloqueio: " + ", ".join(bloqueios))
    if positivos:
        partes.append("Positivo: " + ", ".join(positivos))
    return " | ".join(partes) if partes else f"score {sinal.score_final:.3f} abaixo do limiar"


def _motivo_atencao(sinal, vereditos_raw: list) -> str:
    dados  = sinal.dados_agentes or {}
    streak = dados.get("streak_atual", 0)
    score  = sinal.score_final
    favor  = [v for v in vereditos_raw if v["estado"] in ("ENTRAR","ATENCAO")]
    faltam = [v for v in vereditos_raw if v["estado"] == "AGUARDAR"]
    desc   = ", ".join(f"{v['agente'].upper()}" for v in favor[:3])
    falt   = ", ".join(f"{v['agente'].upper()}" for v in faltam[:3])
    return f"Score {score:.3f} | {streak} reds | Favor: {desc}" + (f" | Falta: {falt}" if falt else "")


def _motivo_abortar(sinal, vereditos_raw: list) -> str:
    for v in vereditos_raw:
        if v["estado"] == "ABORTAR":
            return v["motivo"]
    return "Risco elevado detectado — aguardar reestabilizacao"


def _calcular_convergencia(sinal, vereditos_raw: list) -> dict:
    """Mapa de convergência entre agentes para o dashboard."""
    entrar  = [v for v in vereditos_raw if v["estado"] == "ENTRAR"]
    atencao = [v for v in vereditos_raw if v["estado"] == "ATENCAO"]
    aguardar= [v for v in vereditos_raw if v["estado"] == "AGUARDAR"]
    abortar = [v for v in vereditos_raw if v["estado"] == "ABORTAR"]

    dados = sinal.dados_agentes or {}
    streak_atual = dados.get("streak_atual", 0)
    streak_v = next((v for v in vereditos_raw if v["agente"]=="streak"), {})
    gatilho  = streak_v.get("dados",{}).get("gatilho_dinamico", 4)
    progresso_streak = round(min(streak_atual / max(gatilho,1), 1.0), 3)

    temp_v   = next((v for v in vereditos_raw if v["agente"]=="temperatura"), {})
    temp_val = temp_v.get("dados",{}).get("temp_atual", 0)

    return {
        "n_entrar":  len(entrar),
        "n_atencao": len(atencao),
        "n_aguardar":len(aguardar),
        "n_abortar": len(abortar),
        "favor":     [{"agente":v["agente"],"score":v["score"],"motivo":v["motivo"]} for v in entrar+atencao],
        "contra":    [{"agente":v["agente"],"score":v["score"],"motivo":v["motivo"]} for v in aguardar],
        "streak_progresso": progresso_streak,
        "streak_atual": streak_atual,
        "gatilho":    gatilho,
        "temperatura": temp_val,
        "score_final": round(sinal.score_final, 3),
        "limiar":      round(dados.get("_limiar_efetivo", cfg.SCORE_MINIMO_ENTRADA), 3),
    }


async def atualizar_dashboard_ia():
    alvo = sessao.alvo_atual
    orq = orquestradores.get(alvo)
    if not orq:
        return
    sinal = orq.processar(memoria)
    pesos_map = {
        "streak":      cfg.PESO_AGENTE_STREAK,
        "margem":      cfg.PESO_AGENTE_MARGEM,
        "temporal":    cfg.PESO_AGENTE_TEMPORAL,
        "covariancia": cfg.PESO_AGENTE_COVARIANCA,
        "temperatura": cfg.PESO_AGENTE_TEMPERATURA,
        "risco":       cfg.PESO_AGENTE_RISCO,
        "ia_gemini":   cfg.PESO_AGENTE_IA,
    }
    vereditos_raw = [
        {
            "agente": v.agente,
            "score":  round(v.score, 3),
            "estado": v.estado,
            "motivo": v.motivo,
            "peso":   round(pesos_map.get(v.agente, 0.0), 2),
            "dados":  v.dados if hasattr(v, "dados") else {},
        }
        for v in sinal.vereditos
    ]
    payload = {
        "tipo": "ia_update",
        "vereditos": vereditos_raw,
        "sinal": {
            "estado":   sinal.estado,
            "score":    sinal.score_final,
            "mensagem": sinal.mensagem,
            "dados":    sinal.dados_agentes,
        },
        "convergencia": _calcular_convergencia(sinal, vereditos_raw)
    }
    await broadcast(payload)


# ── Loop SSE ──────────────────────────────────────────────────────────────────
async def sse_loop():
    headers = {
        "User-Agent":    "Mozilla/5.0 Chrome/125.0.0.0",
        "Accept":        "text/event-stream",
        "Cache-Control": "no-cache",
        "Origin":        "https://www.tipminer.com",
        "Referer":       "https://www.tipminer.com/br/cassinos/brabet/crash",
    }
    while True:
        try:
            connector = aiohttp.TCPConnector(ssl=SSL_CTX)
            async with aiohttp.ClientSession(connector=connector) as session:
                log.info(f"Conectando SSE -> {LIVE_URL}")
                async with session.get(LIVE_URL, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=None, connect=10)) as resp:
                    log.info(f"SSE conectado -> status {resp.status}")
                    await broadcast({"tipo": "status", "msg": "Conectado ao feed ao vivo"})
                    buf = ""
                    async for linha in resp.content:
                        buf += linha.decode("utf-8", errors="ignore")
                        while "\n\n" in buf:
                            bloco, buf = buf.split("\n\n", 1)
                            evento = _parse_sse(bloco)
                            if evento and evento.get("event") != "heartbeat" and "data" in evento:
                                try:
                                    d = json.loads(evento["data"])
                                    if "result" in d:
                                        await processar_round(
                                            float(d["result"]),
                                            d.get("instant", datetime.now(timezone.utc).isoformat()),
                                            d.get("type", ""),
                                            int(d.get("temperature", 0)),
                                        )
                                except Exception as e:
                                    log.warning(f"Erro parse round: {e}")
        except Exception as e:
            log.error(f"SSE desconectado: {e} — reconectando em 5s")
            await broadcast({"tipo": "status", "msg": f"Reconectando... ({e})"})
            await asyncio.sleep(5)


def _parse_sse(bloco: str) -> dict:
    result = {}
    for linha in bloco.strip().splitlines():
        if ": " in linha:
            k, v = linha.split(": ", 1)
            if k == "event": result["event"] = v
            elif k == "data": result["data"] = v
    return result


async def historico_handler(request):
    """Retorna as últimas N rodadas do banco de dados (historico de crashs)."""
    try:
        limite = int(request.query.get("limit", "500"))
        rodadas = banco.ler_historico_db(limite=limite)
        return web.json_response({"status": "ok", "rodadas": rodadas})
    except Exception as e:
        log.error(f"Erro no endpoint /historico: {e}")
        return web.json_response({"status": "error", "msg": str(e)}, status=500)

# ── WebSocket handler ──────────────────────────────────────────────────────────
async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    clientes.add(ws)
    log.info(f"Cliente conectado ({len(clientes)} total)")

    alvo = sessao.alvo_atual
    orq_ativo = orquestradores[alvo]
    _w_cfg = banco.ler_config("widgets_config")
    widgets_cfg_dict = {}
    if _w_cfg:
        try:
            widgets_cfg_dict = json.loads(_w_cfg)
        except Exception:
            pass
    sinal = orq_ativo.processar(memoria)
    pesos_map = {
        "streak":      cfg.PESO_AGENTE_STREAK,
        "margem":      cfg.PESO_AGENTE_MARGEM,
        "temporal":    cfg.PESO_AGENTE_TEMPORAL,
        "covariancia": cfg.PESO_AGENTE_COVARIANCA,
        "temperatura": cfg.PESO_AGENTE_TEMPERATURA,
        "risco":       cfg.PESO_AGENTE_RISCO,
        "ia_gemini":   cfg.PESO_AGENTE_IA,
    }
    vereditos_raw = [
        {
            "agente": v.agente,
            "score":  round(v.score, 3),
            "estado": v.estado,
            "motivo": v.motivo,
            "peso":   round(pesos_map.get(v.agente, 0.0), 2),
            "dados":  v.dados if hasattr(v, "dados") else {},
        }
        for v in sinal.vereditos
    ]
    convergencia = _calcular_convergencia(sinal, vereditos_raw)

    await ws.send_str(json.dumps({
        "tipo":      "init",
        "historico": historico_rodadas[-15:],
        "sessao":    sessao.to_dict(),
        "inteligencia_global": orq_ativo.obter_dados_inteligencia_global(),
        "stats": {
            "total_mem":      memoria.total(),
            "alvo":           alvo,
            "max_gale":       cfg.MAX_GALE,
            "stake_base":     cfg.STAKE_BASE,
            "n_hist_treino":  _n_hist_treino,
            "n_vivo_treino":  _n_vivo_treino,
            "ultimo_retreino": orq_ativo._ultimo_retreino.strftime("%d/%m %H:%M") if orq_ativo._ultimo_retreino else "—",
            "calibracao":     orq_ativo.estatisticas_calibracao(),
        },
        "vereditos": vereditos_raw,
        "sinal": {
            "estado":   sinal.estado,
            "score":    sinal.score_final,
            "mensagem": sinal.mensagem,
            "dados":    sinal.dados_agentes,
        },
        "convergencia": convergencia,
        "blame_report": orq_ativo.padrao.blame_report(),
        "bloco_relatorio": orq_ativo.bloco.relatorio_blocos(),
        "estrategia": estrategia_cfg,
        # Credenciais Telegram restauradas do banco (para preencher campos no dashboard)
        "telegram": {
            "token":   cfg.TELEGRAM_TOKEN   or "",
            "chat_id": cfg.TELEGRAM_CHAT_ID or "",
            "conectado": bool(cfg.TELEGRAM_TOKEN and cfg.TELEGRAM_CHAT_ID),
            "modo":    estrategia_cfg.get("telegram_modo", "entrada"),
            "antecedencia": estrategia_cfg.get("alerta_antecedencia", 0),
            "eventos": estrategia_cfg.get("telegram_eventos", {}),
        },
        "gemini": {
            "configurado": bool(cfg.GEMINI_API_KEY),
        },
        "widgets_config": widgets_cfg_dict,
    }))

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await _handle_ws_msg(json.loads(msg.data))
    finally:
        clientes.discard(ws)
    return ws


async def _handle_ws_msg(msg: dict):
    """Processa mensagens enviadas pelo dashboard (config, resultado manual)."""
    global _n_hist_treino, _n_vivo_treino
    tipo = msg.get("tipo")

    if tipo == "config":
        if "stake_base" in msg:
            cfg.STAKE_BASE = float(msg["stake_base"])
        if "max_gale" in msg:
            cfg.MAX_GALE = int(msg["max_gale"])
        if "alvo" in msg:
            novo_alvo = round(float(msg["alvo"]), 2)
            # Garante que o orquestrador existe para esse alvo
            if novo_alvo not in orquestradores:
                orquestradores[novo_alvo] = Orquestrador(novo_alvo)
                log.info(f"Orquestrador criado para alvo={novo_alvo}x")
            # Força o alvo como único ativo (remove os outros do conjunto ativo)
            cfg.ALVOS_ATIVOS = [novo_alvo]
            # Atualiza alvo da sessão (se não estiver em ciclo ativo)
            if not sessao.ativa:
                sessao.alvo_atual = novo_alvo
        log.info(f"Config atualizada: ALVO={cfg.ALVOS_ATIVOS} STAKE={cfg.STAKE_BASE} MAX_GALE={cfg.MAX_GALE}")
        max_g = cfg.MAX_GALE
        prog  = _progressao_recuperacao(sessao.alvo_atual, cfg.STAKE_BASE, max_g)
        await broadcast({"tipo": "config_ok", "alvo": sessao.alvo_atual,
                         "stake_base": cfg.STAKE_BASE, "max_gale": cfg.MAX_GALE,
                         "progressao": prog})

    elif tipo == "resultado_manual":
        # Jogador marca resultado manualmente
        resultado = msg.get("resultado")  # "green" ou "stop"
        if sessao.ativa:
            if resultado == "green":
                sessao.resultado_green()
            elif resultado == "stop":
                sessao.resultado_stop()
            await broadcast({"tipo": "sessao", "sessao": sessao.to_dict()})

    elif tipo == "retreinar":
        # Retreino manual solicitado pelo dashboard
        try:
            resultado = retreinar(None, verbose=True)
            _n_hist_treino = resultado["n_historico"]
            _n_vivo_treino = resultado["n_ao_vivo"]
            
            # Recarrega orquestradores em memória com os dados salvos
            for o in orquestradores.values():
                o.carregar_modelo()
                o._ultimo_retreino = __import__("datetime").datetime.now()
                
            await broadcast({
                "tipo": "retreino",
                "resultado": resultado,
                "msg": f"Retreino manual concluido: {resultado['n_total']} rodadas",
            })
        except Exception as e:
            log.error(f"Erro no retreino manual: {e}")
            await broadcast({"tipo": "status", "msg": f"Erro no retreino: {e}"})

    elif tipo == "cancelar_sessao":
        sessao.ativa = False
        sessao.gale_nivel = 0
        sessao.stake_atual = _stake_para_nivel(0, cfg.STAKE_BASE, cfg.MAX_GALE)
        sessao.salvar_estado()
        await broadcast({"tipo": "sessao", "sessao": sessao.to_dict()})

    elif tipo == "config_gemini":
        key = msg.get("api_key", "").strip()
        if key:
            cfg.GEMINI_API_KEY = key
            banco.salvar_config("gemini_api_key", key)
            log.info("Gemini: API key salva no banco")
            await broadcast({"tipo": "gemini_status", "ok": True, "msg": "✅ Gemini configurado"})
        else:
            await broadcast({"tipo": "gemini_status", "ok": False, "msg": "❌ Chave inválida"})

    elif tipo == "config_estrategia":
        # Atualiza estratégia de recuperação via WebSocket
        for campo in ("nome","recuperacao","meta_recuperar","session_n","session_t",
                      "stakes_custom","alerta_antecedencia","telegram_modo","telegram_eventos","modo_manutencao"):
            if campo in msg:
                estrategia_cfg[campo] = msg[campo]
        if "modo_manutencao" in msg:
            banco.salvar_config("modo_manutencao", str(msg["modo_manutencao"]))
        log.info(f"Estrategia atualizada: {estrategia_cfg}")
        max_g = cfg.MAX_GALE if cfg.MAX_GALE >= 0 else _max_gale_para_alvo(sessao.alvo_atual)
        await broadcast({"tipo": "config_ok", "alvo": sessao.alvo_atual,
                         "stake_base": cfg.STAKE_BASE,
                         "max_gale": max_g,
                         "progressao": _progressao_recuperacao(sessao.alvo_atual, cfg.STAKE_BASE, max_g),
                         "estrategia": estrategia_cfg})

    elif tipo == "telegram_config":
        token   = msg.get("token", "").strip()
        chat_id = msg.get("chat_id", "").strip()

        if token and chat_id:
            cfg.TELEGRAM_TOKEN   = token
            cfg.TELEGRAM_CHAT_ID = chat_id
            # Persiste no banco — token e chat_id são as únicas configs gravadas
            banco.salvar_config("telegram_token",   token)
            banco.salvar_config("telegram_chat_id", chat_id)
            log.info("Telegram: credenciais salvas no banco de dados")

        # Atualiza modo e eventos do Telegram (apenas em memória, ajustáveis a qualquer momento)
        for campo in ("telegram_modo", "telegram_eventos", "alerta_antecedencia"):
            if campo in msg:
                estrategia_cfg[campo] = msg[campo]

        # Testa conexão
        ok = await _telegram_send("✅ CrashIQ conectado ao Telegram!") if cfg.TELEGRAM_TOKEN and cfg.TELEGRAM_CHAT_ID else False
        await broadcast({"tipo": "telegram_status", "ok": ok,
                         "msg": "Telegram conectado!" if ok else "Erro na conexão Telegram"})

    elif tipo == "telegram_stats":
        await _telegram_enviar_stats()

    elif tipo == "salvar_widgets_config":
        cfg_val = msg.get("config")
        if cfg_val:
            import json as _json
            banco.salvar_config("widgets_config", _json.dumps(cfg_val))
            log.info("Widgets config salva no banco")

    elif tipo == "solicitar_insight":
        try:
            dados = await _dados_insight_ia()
            texto, gif_url = await gerar_insight_periodico(dados)
        except Exception as e:
            log.warning(f"solicitar_insight (geração) erro: {e}")
            texto, gif_url = f"⚠️ Erro ao gerar insight: {e}", ""

        # Broadcast ao dashboard independente do Telegram
        await broadcast({"tipo": "marketing_insight", "texto": texto, "gif": gif_url})

        # Telegram separado — falha não impede o dashboard de receber
        if cfg.TELEGRAM_TOKEN and cfg.TELEGRAM_CHAT_ID:
            try:
                await _telegram_send_animation(gif_url, texto)
            except Exception as e:
                log.warning(f"solicitar_insight (telegram) erro: {e}")

    elif tipo == "config_intervalo_stats":
        mins = int(msg.get("minutos", 30))
        estrategia_cfg["stats_intervalo_min"] = max(5, min(mins, 480))
        await broadcast({"tipo": "config_ok", "stats_intervalo_min": estrategia_cfg["stats_intervalo_min"]})


# ── Telegram ──────────────────────────────────────────────────────────────────
from agents.agente_marketing import gerar_mensagem_marketing, gerar_insight_periodico

async def _telegram_send(texto: str) -> bool:
    if not cfg.TELEGRAM_TOKEN or not cfg.TELEGRAM_CHAT_ID:
        log.warning("Telegram: token ou chat_id não configurado — mensagem não enviada")
        return False
    url = f"https://api.telegram.org/bot{cfg.TELEGRAM_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as s:
            r = await s.post(url, json={"chat_id": cfg.TELEGRAM_CHAT_ID, "text": texto,
                                        "parse_mode": "Markdown"}, timeout=aiohttp.ClientTimeout(total=5))
            if r.status != 200:
                body = await r.text()
                log.warning(f"Telegram HTTP {r.status}: {body[:300]}")
                return False
            return True
    except Exception as e:
        log.warning(f"Telegram erro: {e}")
        return False

async def _telegram_send_animation(gif_url: str, caption: str = "") -> bool:
    """Envia GIF/animação ao Telegram com legenda."""
    if not cfg.TELEGRAM_TOKEN or not cfg.TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{cfg.TELEGRAM_TOKEN}/sendAnimation"
    try:
        async with aiohttp.ClientSession() as s:
            r = await s.post(url, json={
                "chat_id":    cfg.TELEGRAM_CHAT_ID,
                "animation":  gif_url,
                "caption":    caption[:1024],
                "parse_mode": "Markdown",
            }, timeout=aiohttp.ClientTimeout(total=8))
            if r.status != 200:
                body = await r.text()
                log.warning(f"Telegram GIF HTTP {r.status}: {body[:300]}")
                return False
            return True
    except Exception as e:
        log.warning(f"Telegram gif erro: {e}")
        return False


def _dados_marketing() -> dict:
    """Monta dicionário de contexto para o AgenteMarketing."""
    s  = sessao.to_dict()
    alvo = sessao.alvo_atual
    o  = orquestradores.get(alvo)
    tend = o.tendencia.analisar(memoria).dados if o and hasattr(o, 'tendencia') else {}
    sinal = o.processar(memoria) if o else None
    
    agentes_list = []
    if sinal:
        for v in sinal.vereditos:
            agentes_list.append({
                "agente": v.agente,
                "estado": v.estado,
                "score": round(v.score, 2),
                "motivo": v.motivo
            })

    return {
        "pl_sessao":         s.get("pl_sessao", 0),
        "win_rate":          s.get("win_rate", 0),
        "entradas":          s.get("entradas", 0),
        "greens":            s.get("greens", 0),
        "stops":             s.get("stops", 0),
        "greens_seq":        sessao.greens_consecutivos,
        "stops_seq":         o.padrao._stops_consec if o and hasattr(o, 'padrao') else 0,
        "modo_recuperacao":  s.get("modo_recuperacao", False),
        "tendencia":         tend.get("fase", "NEUTRA"),
        "bloco_atual":       o.bloco._key(datetime.now()) if o and hasattr(o, 'bloco') else "—",
        "bloco_wr":          o.bloco._stats(o.bloco._key(datetime.now())).get("wr") if o and hasattr(o, 'bloco') else None,
        "melhores_blocos":   o.bloco.melhores_blocos(3) if o and hasattr(o, 'bloco') else [],
        "piores_blocos":     o.bloco.piores_blocos(2) if o and hasattr(o, 'bloco') else [],
        "insight_bloco":     o.bloco.insight_str() if o and hasattr(o, 'bloco') else "",
        "blame_report":      o.padrao.blame_report() if o and hasattr(o, 'padrao') else [],
        "agentes_analise":   agentes_list,
        "score_consolidado": sinal.score_final if sinal else 0.5,
        "sinal_consolidado": sinal.estado if sinal else "AGUARDAR",
    }


async def _telegram_enviar_marketing(evento: str = "geral"):
    """Gera e envia mensagem motivacional via AgenteMarketing."""
    dados = _dados_marketing()
    try:
        texto, gif_url = await gerar_mensagem_marketing(dados)
        ok = await _telegram_send_animation(gif_url, texto)
        if not ok:
            await _telegram_send(texto)
    except Exception as e:
        log.warning(f"AgenteMarketing erro: {e}")


async def _dados_insight_ia() -> dict:
    """Monta payload rico para o Gemini: inclui análise dos agentes IA em tempo real."""
    base = _dados_marketing()

    # Enriquece com análise atual da tendência
    try:
        alvo = sessao.alvo_atual
        o = orquestradores[alvo]
        veredito_tend = o.tendencia.analisar(memoria)
        veredito_bloco = o.bloco.analisar(memoria)
        blame = o.padrao.blame_report()

        base["veredito_tendencia"] = veredito_tend.estado
        base["score_tendencia"]    = veredito_tend.score
        base["motivo_tendencia"]   = veredito_tend.motivo
        base["veredito_bloco"]     = veredito_bloco.estado
        base["motivo_bloco"]       = veredito_bloco.motivo
        base["blame_report"]       = blame

        # Agentes com maior índice de erro nas últimas entradas
        culpados = [b for b in blame if b.get("loss_rate", 0) >= 0.55 and b.get("total", 0) >= 5]
        base["agentes_problematicos"] = culpados

        # Resumo das últimas 10 rodadas ao vivo
        ultimas = memoria.snapshot()[-10:]
        if ultimas:
            acima_alvo = sum(1 for r in ultimas if r.multiplicador >= alvo)
            base["ultimas_10_pct_alvo"] = round(acima_alvo / len(ultimas), 2)
            base["ultimas_10_n"]        = len(ultimas)
    except Exception as e:
        log.warning(f"_dados_insight_ia enriquecimento: {e}")

    return base


async def _tarefa_periodica():
    """Envia stats + insight Gemini periodicamente. Intervalo re-lido a cada ciclo."""
    while True:
        # Re-lê intervalo a cada ciclo — mudanças de config tomam efeito no próximo ciclo
        intervalo_min = estrategia_cfg.get("stats_intervalo_min", 30)
        await asyncio.sleep(intervalo_min * 60)

        if not cfg.TELEGRAM_TOKEN or not cfg.TELEGRAM_CHAT_ID:
            continue  # Telegram não configurado, aguarda próximo ciclo

        # Stats
        try:
            await _telegram_enviar_stats()
            await asyncio.sleep(3)
        except Exception as e:
            log.warning(f"Tarefa periódica (stats) erro: {e}")

        # Insight IA — geração e broadcast independentes do Telegram
        try:
            dados = await _dados_insight_ia()
            texto, gif_url = await gerar_insight_periodico(dados)
        except Exception as e:
            log.warning(f"Tarefa periódica (geração insight) erro: {e}")
            texto, gif_url = "", ""

        if texto:
            await broadcast({"tipo": "marketing_insight", "texto": texto, "gif": gif_url})
            log.info(f"Insight periódico enviado ao dashboard (intervalo={intervalo_min}min)")
            try:
                ok = await _telegram_send_animation(gif_url, texto)
                if not ok:
                    await _telegram_send(texto)
                else:
                    log.info("Insight periódico enviado ao Telegram")
            except Exception as e:
                log.warning(f"Tarefa periódica (telegram gif) erro: {e}")
                # Tenta enviar só o texto como fallback
                try:
                    await _telegram_send(texto)
                except Exception:
                    pass


def _telegram_tendencia_str(alvo: float | None = None) -> str:
    """Pega tendência atual do agente de tendência do orquestrador."""
    try:
        o    = orquestradores.get(alvo or sessao.alvo_atual)
        dados = o.tendencia.analisar(memoria).dados
        fase = dados.get("fase", "Neutra")
        pct  = dados.get("pct_alta", 0)
        return f"{fase} ({pct:.0%} acima do alvo)"
    except Exception:
        return "—"

def _telegram_prob_str(alvo: float) -> str:
    """Extrai probabilidade estimada do agente de risco."""
    try:
        o = orquestradores.get(alvo or sessao.alvo_atual)
        v = o.risco.analisar(memoria)
        prob = v.dados.get("prob_recente", 0)
        return f"{prob:.2%}"
    except Exception:
        return "—"


async def _telegram_enviar_stats():
    s = sessao.to_dict()
    greens_diretos = s.get("greens_g0", s["greens"])  # greens sem gale se disponível
    gale_wins = [s["greens"] - greens_diretos] if s["greens"] > greens_diretos else []

    texto = (
        f"📊 *ESTATÍSTICAS*\n\n"
        f"Total de apostas: `{s['entradas']}`\n"
        f"Win Rate: `{s['win_rate']:.2f}%`\n\n"
        f"Vitórias diretas: `{greens_diretos}`\n"
    )
    # Linhas de gale (até MAX_GALE)
    for i in range(1, cfg.MAX_GALE + 1):
        n = s.get(f"greens_g{i}", 0)
        texto += f"Vitórias Martingale {i}: `{n}`\n"
    texto += f"Derrotas: `{s['stops']}`\n"
    if s["modo_recuperacao"]:
        texto += f"\n🔄 Em recuperação: `R${s['recuperado']:.2f}` / `R${s['meta_recuperar']:.2f}`"
    await _telegram_send(texto)


async def _telegram_notificar_acao(acao: dict, sinal=None, alvo: float = 1.5):
    tipo  = acao.get("tipo", "")
    tconf = estrategia_cfg.get("telegram_eventos", {})

    if tipo == "entrar" and tconf.get("entrar", True):
        tend  = _telegram_tendencia_str(alvo)
        prob  = _telegram_prob_str(alvo)
        ultimo = acao.get("resultado", 0)
        texto = (
            f"📊 *SINAL DE ANÁLISE*\n\n"
            f"Última vela: `{ultimo:.2f}x`\n"
            f"Probabilidade de acertar o alvo ({alvo}x): `{prob}`\n"
            f"Tendência: `{tend}`\n"
            f"🟢 *ENTRAR*\n"
        )
        await _telegram_send(texto)

    elif tipo == "resultado" and tconf.get("resultado", True):
        if acao.get("estado") == "GREEN":
            resultado_ultimo = acao.get("resultado", 1.5)
            gale_nivel = acao.get("gale_nivel", 0)
            await _telegram_send(
                f"✅ *WIN* — vela: `{resultado_ultimo:.2f}x` (G{gale_nivel})"
            )
        else:
            resultado_ultimo = acao.get("resultado", 1.0)
            gale_nivel = acao.get("gale_nivel", 0)
            await _telegram_send(
                f"❌ *STOP* — vela: `{resultado_ultimo:.2f}x` (G{gale_nivel} esgotado)"
            )

    elif tipo == "gale" and tconf.get("gale", True):
        resultado_ultimo = acao.get("resultado", 1.0)
        titulo = acao.get('titulo', '').split(" — ")[0]
        await _telegram_send(
            f"⚠️ *{titulo}*\n"
            f"Último resultado: `{resultado_ultimo:.2f}x`"
        )

    elif tipo == "atencao" and tconf.get("atencao", False):
        texto = (
            f"📊 *SINAL DE ANÁLISE*\n\n"
            f"⚠️ Aguardar próximo resultado\n"
        )
        await _telegram_send(texto)


# ── Backtest endpoint ──────────────────────────────────────────────────────────
async def backtest_handler(request):
    """Executa backtest nos dados históricos com filtros opcionais."""
    try:
        params = await request.json()
        filtro_dia    = params.get("dia", "")          # "SEG","TER",...
        filtro_bloco  = params.get("bloco", "")        # "14:20"
        filtro_hora_i = params.get("hora_inicio", "")  # "08:00"
        filtro_hora_f = params.get("hora_fim", "")     # "22:00"
        alvo          = float(params.get("alvo", 1.5))
        max_gale      = int(params.get("max_gale", 2))

        # Carrega histórico
        rodadas = carregar(CSV_PATH) if Path(CSV_PATH).exists() else []

        def hora_str(ts): return f"{ts.hour:02d}:{ts.minute:02d}"
        def dia_str(ts):
            return ["SEG","TER","QUA","QUI","SEX","SAB","DOM"][ts.weekday()]

        # Aplica filtros
        if filtro_dia:
            rodadas = [r for r in rodadas if dia_str(r.timestamp) == filtro_dia.upper()]
        if filtro_bloco:
            rodadas = [r for r in rodadas if getattr(r,"bloco_id","") == filtro_bloco]
        if filtro_hora_i and filtro_hora_f:
            h_i = filtro_hora_i.replace(":","")
            h_f = filtro_hora_f.replace(":","")
            rodadas = [r for r in rodadas if h_i <= hora_str(r.timestamp).replace(":","") <= h_f]

        if len(rodadas) < 10:
            return web.json_response({"ok": False, "erro": f"Dados insuficientes após filtros ({len(rodadas)} rodadas)"})

        # Simula estratégia gale
        total_ciclos = 0
        wins = 0
        pl   = 0.0
        i    = 0
        stake_base = cfg.STAKE_BASE
        max_streak_loss = 0
        streak_loss = 0

        while i < len(rodadas):
            acertou = False
            custo   = 0.0
            for g in range(max_gale + 1):
                if i + g >= len(rodadas):
                    break
                s = _stake_para_nivel(g, stake_base, max_gale)
                custo += s
                if rodadas[i + g].multiplicador >= alvo:
                    lucro = s * (alvo - 1)
                    pl += lucro - (custo - s)
                    acertou = True
                    i += g + 1
                    break
            if not acertou:
                pl -= custo
                i  += max_gale + 1
            total_ciclos += 1
            if acertou:
                wins += 1
                streak_loss = 0
            else:
                streak_loss += 1
                max_streak_loss = max(max_streak_loss, streak_loss)

        # Distribui resultado por hora
        por_hora: dict[str, dict] = {}
        i = 0
        while i < len(rodadas):
            h = rodadas[i].timestamp.hour
            k = f"{h:02d}h"
            if k not in por_hora:
                por_hora[k] = {"ciclos": 0, "wins": 0, "pl": 0.0}
            acertou = False
            custo   = 0.0
            for g in range(max_gale + 1):
                if i + g >= len(rodadas): break
                s = _stake_para_nivel(g, stake_base, max_gale)
                custo += s
                if rodadas[i + g].multiplicador >= alvo:
                    pl_c = s * (alvo - 1) - (custo - s)
                    por_hora[k]["pl"] += pl_c
                    acertou = True
                    i += g + 1
                    break
            if not acertou:
                por_hora[k]["pl"] -= custo
                i += max_gale + 1
            por_hora[k]["ciclos"] += 1
            if acertou:
                por_hora[k]["wins"] += 1

        return web.json_response({
            "ok": True,
            "n_rodadas":      len(rodadas),
            "n_ciclos":       total_ciclos,
            "wins":           wins,
            "taxa_acerto":    round(wins / total_ciclos * 100, 1) if total_ciclos else 0,
            "pl_total":       round(pl, 2),
            "max_loss_streak":max_streak_loss,
            "por_hora":       {k: {"ciclos": v["ciclos"], "wins": v["wins"],
                                    "taxa": round(v["wins"]/v["ciclos"]*100,1) if v["ciclos"] else 0,
                                    "pl": round(v["pl"], 2)}
                               for k, v in sorted(por_hora.items())},
            "filtros":        {"dia": filtro_dia, "bloco": filtro_bloco,
                               "hora_inicio": filtro_hora_i, "hora_fim": filtro_hora_f},
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        return web.json_response({"ok": False, "erro": str(e)}, status=500)


# ── Serve dashboard HTML ───────────────────────────────────────────────────────
async def index_handler(request):
    html_path = Path("dashboard.html")
    if not html_path.exists():
        return web.Response(text="dashboard.html nao encontrado", status=404)
    return web.Response(text=html_path.read_text(encoding="utf-8"), content_type="text/html")


# ── Upload XLSX para resincronização ──────────────────────────────────────────
async def upload_xlsx_handler(request):
    """Recebe um arquivo XLSX da Tipminer, importa rodadas novas (sem duplicatas)."""
    try:
        reader = await request.multipart()
        field  = await reader.next()
        if field is None or field.name != "file":
            return web.json_response({"ok": False, "erro": "Campo 'file' ausente"}, status=400)

        conteudo = await field.read()
        if not conteudo:
            return web.json_response({"ok": False, "erro": "Arquivo vazio"}, status=400)

        import io, openpyxl, sqlite3
        from config import DB_PATH
        from memoria import Rodada

        wb = openpyxl.load_workbook(io.BytesIO(conteudo))
        ws = wb.active

        # Timestamps já gravados (truncados em segundos para dedup)
        with sqlite3.connect(DB_PATH) as c:
            rows = c.execute("SELECT ts FROM rodadas").fetchall()
        existentes = {r[0][:19] for r in rows}

        inseridas = 0
        duplicadas = 0
        erros = 0

        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i < 2:  # pula cabeçalho e linha "tipminer.com"
                continue
            try:
                numero, cor, data_raw, hora_raw = row
                if not data_raw or not hora_raw:
                    continue
                if str(numero).lower() == "tipminer.com":
                    continue
                mult = float(numero)
                data_str = data_raw.strip() if isinstance(data_raw, str) else str(data_raw)
                hora_str = hora_raw.strip() if isinstance(hora_raw, str) else str(hora_raw)
                ts = datetime.strptime(f"{data_str} {hora_str}", "%d/%m/%Y %H:%M:%S")
                ts_key = ts.isoformat()[:19]
                if ts_key in existentes:
                    duplicadas += 1
                    continue
                rodada = Rodada(multiplicador=mult, timestamp=ts, temperatura=0)
                banco.gravar_rodada(rodada)
                existentes.add(ts_key)
                # Alimenta memória ao vivo também
                memoria.registrar(mult, ts, temperatura=0)
                inseridas += 1
            except Exception:
                erros += 1

        msg = f"Sincronizado: {inseridas} novas | {duplicadas} já existiam | {erros} erros"
        log.info(f"Upload XLSX: {msg}")
        await broadcast({"tipo": "status", "msg": msg})
        return web.json_response({"ok": True, "inseridas": inseridas,
                                   "duplicadas": duplicadas, "erros": erros, "msg": msg})

    except Exception as e:
        log.error(f"Erro upload XLSX: {e}")
        return web.json_response({"ok": False, "erro": str(e)}, status=500)


# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    banco.inicializar()

    # Garante sessão limpa a cada reinício do servidor (mas carrega o estado persistido das estatísticas)
    sessao.ativa      = False
    sessao.gale_nivel = 0
    sessao.carregar_estado()

    # Configura callbacks da IA para atualizar o dashboard quando as análises terminarem
    for orq in orquestradores.values():
        if hasattr(orq, "ia_gemini"):
            orq.ia_gemini.callback_update = atualizar_dashboard_ia

    _manut = banco.ler_config("modo_manutencao")
    if _manut:
        estrategia_cfg["modo_manutencao"] = (_manut.lower() == "true")
        log.info(f"Modo Manutencao carregado do banco: {estrategia_cfg['modo_manutencao']}")


    # ── Carrega credenciais persistidas no banco ─────────────────────────────
    _tg_token   = banco.ler_config("telegram_token")
    _tg_chat_id = banco.ler_config("telegram_chat_id")
    if _tg_token and _tg_chat_id:
        cfg.TELEGRAM_TOKEN   = _tg_token
        cfg.TELEGRAM_CHAT_ID = _tg_chat_id
        log.info(f"Telegram: credenciais restauradas do banco (chat_id={_tg_chat_id})")
    else:
        log.info("Telegram: sem credenciais salvas — configure via dashboard")

    _gemini_key = banco.ler_config("gemini_api_key")
    if _gemini_key:
        cfg.GEMINI_API_KEY = _gemini_key
        log.info("Gemini: API key restaurada do banco")
    elif cfg.GEMINI_API_KEY:
        log.info("Gemini: API key carregada da variável de ambiente")
    else:
        log.warning("Gemini: API key ausente — configure via dashboard (Configurações > IA)")

    # ── Calibração inicial: histórico CSV + rodadas ao vivo acumuladas ────────
    from retreinar import retreinar, _carregar_csv_ao_vivo

    rodadas_hist = []
    if Path(CSV_PATH).exists():
        rodadas_hist = carregar(CSV_PATH)
        log.info(f"Historico CSV: {len(rodadas_hist)} rodadas")

    # Tenta carregar rodadas ao vivo exportadas anteriormente
    csv_ao_vivo = "data/ao_vivo_retreino.csv"
    rodadas_ao_vivo = []
    if Path(csv_ao_vivo).exists():
        rodadas_ao_vivo = _carregar_csv_ao_vivo(csv_ao_vivo)
        log.info(f"Rodadas ao vivo (SQLite exportado): {len(rodadas_ao_vivo)}")

    todas = rodadas_hist + rodadas_ao_vivo
    todas.sort(key=lambda r: r.timestamp)
    corte = int(len(todas) * 0.80)
    treino = todas[:corte]

    _PADRAO_STATE = Path(__file__).parent / "padrao_state.json"
    for o in orquestradores.values():
        o.carregar_historico(treino)
        o._ultimo_retreino = __import__("datetime").datetime.now()
        # Restaura estado persistido do AgentePadrao (memória vetorial + LSTM)
        if _PADRAO_STATE.exists():
            try:
                import json as _json
                o.padrao.from_dict(_json.loads(_PADRAO_STATE.read_text("utf-8")))
                log.info(f"AgentePadrao: estado restaurado ({len(o.padrao._memoria)} entradas na memória)")
            except Exception as e:
                log.warning(f"AgentePadrao: falha ao restaurar estado — {e}")

    # Popula memória com as últimas 150 do treino para agentes reativos
    for r in treino[-150:]:
        memoria.registrar(r.multiplicador, r.timestamp, temperatura=getattr(r, "temperatura", 0))
    global _n_hist_treino, _n_vivo_treino
    _n_hist_treino = len(rodadas_hist)
    _n_vivo_treino = len(rodadas_ao_vivo)
    log.info(f"Agentes calibrados: {_n_hist_treino} hist + {_n_vivo_treino} ao vivo = {len(treino)} rodadas de treino")

    app = web.Application(client_max_size=20 * 1024 * 1024)  # 20 MB max upload
    app.router.add_get("/",          index_handler)
    app.router.add_get("/ws",        ws_handler)
    app.router.add_get("/historico", historico_handler)
    app.router.add_post("/upload",   upload_xlsx_handler)
    app.router.add_post("/backtest", backtest_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", PORT)
    await site.start()
    log.info(f"Dashboard: http://localhost:{PORT}")

    # Restaura estado do AgenteBloco
    _BLOCO_STATE = Path(__file__).parent / "bloco_state.json"
    for o in orquestradores.values():
        if _BLOCO_STATE.exists():
            try:
                import json as _j
                o.bloco.from_dict(_j.loads(_BLOCO_STATE.read_text("utf-8")))
                log.info(f"AgenteBloco: {len(o.bloco._hist)} blocos restaurados")
            except Exception as e:
                log.warning(f"AgenteBloco: falha ao restaurar — {e}")

    # Inicia loop de retreino diário em background
    asyncio.create_task(loop_retreino())
    # Inicia envio periódico de stats + insight Gemini ao Telegram
    asyncio.create_task(_tarefa_periodica())

    await sse_loop()


def _suprimir_erros_windows(loop, context):
    """Suprime ConnectionResetError cosmético do Proactor no Windows (WinError 10054)."""
    exc = context.get("exception")
    if isinstance(exc, ConnectionResetError):
        return
    loop.default_exception_handler(context)


if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        loop = asyncio.new_event_loop()
        loop.set_exception_handler(_suprimir_erros_windows)
        asyncio.set_event_loop(loop)
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("\nServidor encerrado.")
