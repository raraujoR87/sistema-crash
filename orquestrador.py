# orquestrador.py — Combina 6 agentes em um sinal único via Grafo de Orquestração (LangGraph Style).
# Cada nó representa um estágio de análise do fluxo, permitindo roteamento condicional e vetos antecipados.

from dataclasses import dataclass
from datetime import datetime
import config
from agents.base import Veredito
from agents.agente_streak      import AgenteStreak
from agents.agente_margem      import AgenteMargem
from agents.agente_temporal    import AgenteTemporal
from agents.agente_covariancia import AgenteCovariancia
from agents.agente_temperatura import AgenteTemperatura
from agents.agente_risco       import AgenteRisco
from agents.agente_ia          import AgenteIA
from agents.agente_padrao      import AgentePadrao
from agents.agente_tendencia   import AgenteTendencia
from agents.agente_bloco       import AgenteBloco
from agents.agente_rtp         import AgenteRTP
from agents.agente_estrategia  import AgenteEstrategia
import json
import banco
import random


@dataclass
class Sinal:
    score_final: float
    estado: str          # AGUARDAR | ATENCAO | ENTRAR | ABORTAR
    mensagem: str
    vereditos: list[Veredito]
    dados_agentes: dict


class StateGraph:
    """Mecanismo leve de fluxo em grafos de estados (LangGraph-like)."""
    def __init__(self):
        self.nodes = {}
        self.edges = {}
        self.conditional_edges = {}

    def add_node(self, name: str, func):
        self.nodes[name] = func

    def add_edge(self, start: str, end: str):
        self.edges[start] = end

    def add_conditional_edges(self, start: str, router_func, path_map: dict):
        self.conditional_edges[start] = (router_func, path_map)

    def execute(self, initial_state: dict) -> dict:
        state = initial_state
        current = "START"
        state["passos"] = [current]

        # Inicia a travessia a partir da transição de START
        current = self.edges.get("START")
        while current and current != "END":
            state["passos"].append(current)
            # Executa o processamento do nó
            state = self.nodes[current](state)
            
            # Decisão da próxima transição
            if current in self.conditional_edges:
                router, path_map = self.conditional_edges[current]
                decision = router(state)
                next_node = path_map.get(decision, "END")
            else:
                next_node = self.edges.get(current, "END")
            current = next_node

        state["passos"].append("END")
        return state


class Orquestrador:
    def __init__(self, alvo: float):
        self.alvo        = alvo
        self.streak      = AgenteStreak(alvo)
        self.margem      = AgenteMargem(alvo)
        self.temporal    = AgenteTemporal(alvo)
        self.covariancia = AgenteCovariancia(alvo)
        self.temperatura = AgenteTemperatura(alvo)
        self.risco       = AgenteRisco(alvo)
        self.ia_gemini   = AgenteIA(alvo)
        self.padrao      = AgentePadrao(alvo)
        self.tendencia   = AgenteTendencia(alvo)
        self.bloco       = AgenteBloco(alvo)
        self.rtp_agente  = AgenteRTP(alvo)
        self.estrategia  = AgenteEstrategia(alvo)

        # Configuração do Grafo de Orquestração (Workflow)
        self.workflow = StateGraph()
        self._setup_workflow()

        # Rastreamento de acurácia para auto-calibração
        self._historico_resultados: list[dict] = []   # {score, acertou}
        self._falsos_positivos_consecutivos = 0
        self._rounds_de_pausa_pos_red = 0
        self._ultimo_retreino: datetime | None = None

        # Aquecimento: conta rounds ao vivo processados (não conta backtest)
        self._n_rounds_ao_vivo = 0
        self._WARMUP_MINIMO = 20  # rounds mínimos ao vivo antes de habilitar ENTRAR
        self._em_backtest = False  # flag para suprimir warmup durante carregar_historico

    def _setup_workflow(self):
        # 1. Registro dos nós
        self.workflow.add_node("risco_node", self._risco_node)
        self.workflow.add_node("veto_risco_node", self._veto_risco_node)
        self.workflow.add_node("temporal_node", self._temporal_node)
        self.workflow.add_node("veto_temporal_node", self._veto_temporal_node)
        self.workflow.add_node("analise_node", self._analise_node)
        self.workflow.add_node("consolidacao_node", self._consolidacao_node)

        # 2. Definição das conexões
        self.workflow.add_edge("START", "risco_node")
        
        # Roteamento condicional no nó de risco
        self.workflow.add_conditional_edges(
            "risco_node",
            lambda state: "veto" if state["vetado"] else "continue",
            {"veto": "veto_risco_node", "continue": "temporal_node"}
        )
        self.workflow.add_edge("veto_risco_node", "consolidacao_node")
        
        # Roteamento condicional no nó temporal
        self.workflow.add_conditional_edges(
            "temporal_node",
            lambda state: "veto" if state["vetado"] else "continue",
            {"veto": "veto_temporal_node", "continue": "analise_node"}
        )
        self.workflow.add_edge("veto_temporal_node", "consolidacao_node")
        
        self.workflow.add_edge("analise_node", "consolidacao_node")
        self.workflow.add_edge("consolidacao_node", "END")

    @property
    def _agentes_pesos(self):
        """Mantém compatibilidade de assinatura externa com o payload do server.py."""
        return [
            (self.streak,      config.PESO_AGENTE_STREAK),
            (self.margem,      config.PESO_AGENTE_MARGEM),
            (self.temporal,    config.PESO_AGENTE_TEMPORAL),
            (self.covariancia, config.PESO_AGENTE_COVARIANCA),
            (self.temperatura, config.PESO_AGENTE_TEMPERATURA),
            (self.risco,       config.PESO_AGENTE_RISCO),
            (self.ia_gemini,   config.PESO_AGENTE_IA),
            (self.rtp_agente,  config.PESO_AGENTE_RTP),
        ]

    def _risco_node(self, state: dict) -> dict:
        v = self.risco.analisar(state["memoria"])
        state["vereditos"].append(v)
        state["dados_agentes"]["risco"] = v.dados
        if v.estado == "ABORTAR":
            state["vetado"] = True
        return state

    def _veto_risco_node(self, state: dict) -> dict:
        agentes = ["temporal", "streak", "margem", "covariancia", "temperatura", "ia_gemini", "tendencia", "bloco", "padrao", "rtp", "estrategia"]
        motivo_risco = next((v.motivo for v in state["vereditos"] if v.agente == "risco"), "")
        for nome in agentes:
            state["vereditos"].append(Veredito(
                agente=nome,
                score=0.0,
                estado="ABORTAR",
                motivo=f"Bypassed: Veto do Agente de Risco ({motivo_risco})",
                dados={}
            ))
            state["dados_agentes"][nome] = {}
        return state

    def _temporal_node(self, state: dict) -> dict:
        v = self.temporal.analisar(state["memoria"])
        state["vereditos"].append(v)
        state["dados_agentes"]["temporal"] = v.dados
        if v.score < 0.30:
            state["vetado"] = True
        return state

    def _veto_temporal_node(self, state: dict) -> dict:
        agentes = ["streak", "margem", "covariancia", "temperatura", "ia_gemini", "tendencia", "bloco", "padrao", "rtp", "estrategia"]
        motivo_temp = next((v.motivo for v in state["vereditos"] if v.agente == "temporal"), "")
        for nome in agentes:
            state["vereditos"].append(Veredito(
                agente=nome,
                score=0.0,
                estado="AGUARDAR",
                motivo=f"Bypassed: Veto por Bloco Temporal ({motivo_temp})",
                dados={}
            ))
            state["dados_agentes"][nome] = {}
        return state

    def _analise_node(self, state: dict) -> dict:
        # Executa os agentes analíticos e estatísticos
        for agente in [self.streak, self.margem, self.covariancia, self.temperatura, self.ia_gemini, self.rtp_agente, self.estrategia]:
            v = agente.analisar(state["memoria"])
            state["vereditos"].append(v)
            state["dados_agentes"][agente.nome] = v.dados

        # Tendência de mercado (ALTA/BAIXA/NEUTRA) — veto em BAIXA severa
        v_tend = self.tendencia.analisar(state["memoria"])
        state["vereditos"].append(v_tend)
        state["dados_agentes"]["tendencia"] = v_tend.dados
        if v_tend.estado == "ABORTAR":
            state["vetado"] = True

        # Análise de bloco histórico — veta períodos com histórico muito ruim
        v_bloco = self.bloco.analisar(state["memoria"])
        state["vereditos"].append(v_bloco)
        state["dados_agentes"]["bloco"] = v_bloco.dados
        if v_bloco.estado == "ABORTAR":
            state["vetado"] = True

        # Agente de padrão/feedback — ABORTAR tem poder de veto antecipado
        v_padrao = self.padrao.analisar(state["memoria"])
        state["vereditos"].append(v_padrao)
        state["dados_agentes"]["padrao"] = v_padrao.dados
        if v_padrao.estado == "ABORTAR":
            state["vetado"] = True
        return state

    def _consolidacao_node(self, state: dict) -> dict:
        vereditos = state["vereditos"]
        dados_agentes = state["dados_agentes"]

        # Mapeia vereditos por nome para aplicar os pesos corretos
        pesos_base = {
            "streak":      config.PESO_AGENTE_STREAK,
            "margem":      config.PESO_AGENTE_MARGEM,
            "temporal":    config.PESO_AGENTE_TEMPORAL,
            "covariancia": config.PESO_AGENTE_COVARIANCA,
            "temperatura": config.PESO_AGENTE_TEMPERATURA,
            "risco":       config.PESO_AGENTE_RISCO,
            "ia_gemini":   config.PESO_AGENTE_IA,
            "rtp":         config.PESO_AGENTE_RTP,
            "estrategia":  0.15,
            "tendencia":   0.10,
            "bloco":       0.12,   # peso do histórico de bloco/período
            "padrao":      0.13,
        }

        # Aplica blame weights — penaliza agentes com alto índice de stops
        blame_mod = self.padrao.blame_weights()
        pesos_map = {k: v * blame_mod.get(k, 1.0) for k, v in pesos_base.items()}

        # Normaliza pesos para somar 1
        total_peso = sum(pesos_map.values())
        pesos_map  = {k: v/total_peso for k, v in pesos_map.items()}

        # Calcula a soma ponderada (ignora agentes não presentes)
        score = sum(v.score * pesos_map.get(v.agente, 0) for v in vereditos)

        # Determina o estado baseado em vetos ou regras de consenso
        veredito_risco = next((v for v in vereditos if v.agente == "risco"), None)
        veredito_temporal = next((v for v in vereditos if v.agente == "temporal"), None)

        if veredito_risco and veredito_risco.estado == "ABORTAR":
            estado = "ABORTAR"
            score = 0.0
            mensagem = f"[ABORTAR] — {veredito_risco.motivo}"
        elif veredito_temporal and veredito_temporal.score < 0.30:
            estado = "AGUARDAR"
            score = round(veredito_temporal.score, 3)
            mensagem = f"[AGUARDAR] Bloco vetado — {veredito_temporal.motivo}"
        else:
            # Penalidade adaptativa por falsos positivos recentes
            penalidade = self._penalidade_falsos_positivos()
            limiar_efetivo = config.SCORE_MINIMO_ENTRADA + penalidade

            votos_entrar  = sum(1 for v in vereditos if v.estado == "ENTRAR")
            votos_atencao = sum(1 for v in vereditos if v.estado in ("ENTRAR", "ATENCAO"))
            votos_abortar = sum(1 for v in vereditos if v.estado == "ABORTAR")

            # Consenso rigoroso: 4+ votos ENTRAR, score acima do limiar, nenhum ABORTAR
            if score >= limiar_efetivo and votos_entrar >= 4 and votos_abortar == 0:
                estado = "ENTRAR"
            elif score >= config.SCORE_ALERTA_ATENCAO and votos_atencao >= 4:
                estado = "ATENCAO"
            else:
                estado = "AGUARDAR"

            resumo = " | ".join(
                f"{v.agente[:4].upper()}={v.score:.2f}({v.estado[0]})"
                for v in vereditos
            )
            mensagem = _formatar_mensagem(estado, score, vereditos, resumo, penalidade)

        dados_agentes["_score_final"]     = round(score, 4)
        dados_agentes["_votos_entrar"]    = sum(1 for v in vereditos if v.estado == "ENTRAR")
        dados_agentes["_limiar_efetivo"]  = round(config.SCORE_MINIMO_ENTRADA + self._penalidade_falsos_positivos(), 3)
        dados_agentes["_falsos_pos"]      = self._falsos_positivos_consecutivos
        dados_agentes["streak_atual"]     = dados_agentes.get("streak", {}).get("streak", 0)
        dados_agentes["ema_rapida"]       = dados_agentes.get("margem", {}).get("ema_rapida")

        # Ordena os vereditos de acordo com a ordem padrão para consistência no frontend
        ordem_agentes = ["rtp", "streak", "margem", "temporal", "covariancia", "temperatura", "risco", "ia_gemini"]
        ordem_agentes = ["rtp", "streak", "margem", "temporal", "covariancia", "temperatura", "risco", "ia_gemini", "estrategia"]
        vereditos_ordenados = sorted(vereditos, key=lambda v: ordem_agentes.index(v.agente) if v.agente in ordem_agentes else 99)

        state["sinal"] = Sinal(
            score_final=round(score, 3),
            estado=estado,
            mensagem=mensagem,
            vereditos=vereditos_ordenados,
            dados_agentes=dados_agentes,
        )
        return state

    def processar(self, memoria) -> Sinal:
        # Estado inicial do grafo
        initial_state = {
            "memoria":       memoria,
            "vereditos":     [],
            "dados_agentes": {},
            "vetado":        False,
            "sinal":         None,
            "passos":        []
        }
        
        final_state = self.workflow.execute(initial_state)
        sinal = final_state["sinal"]

        # ── Pausa Pós-Indicação (Sniper Cooldown) ──
        if self._rounds_de_pausa_pos_red > 0:
            if not self._em_backtest:
                self._rounds_de_pausa_pos_red -= 1
            else:
                self._rounds_de_pausa_pos_red -= 1
            
            sinal.estado = "ABORTAR"
            sinal.mensagem = f"[SISTEMA] Pausa Estratégica (Sniper): Faltam {self._rounds_de_pausa_pos_red} rodadas."

        # ── Período de aquecimento (só conta rounds ao vivo, não backtest) ──
        if not self._em_backtest:
            self._n_rounds_ao_vivo += 1
            faltam = self._WARMUP_MINIMO - self._n_rounds_ao_vivo
            if faltam > 0:
                sinal.dados_agentes["_warmup_faltam"] = faltam
                if sinal.estado == "ENTRAR":
                    sinal = Sinal(
                        score_final=sinal.score_final,
                        estado="ATENCAO",
                        mensagem=f"[AQUECENDO] {faltam} rounds ao vivo para habilitar entradas — {sinal.mensagem}",
                        vereditos=sinal.vereditos,
                        dados_agentes=sinal.dados_agentes,
                    )
        # ────────────────────────────────────────────────────────────────────

        # Anexa o histórico de execução do grafo nos dados extras
        sinal.dados_agentes["_grafo_passos"] = final_state["passos"]
        sinal.dados_agentes["_vetado"]       = final_state["vetado"]
        return sinal

    def carregar_historico(self, rodadas_historicas: list):
        """Alimenta agentes que precisam de histórico (CSV ou SQLite ao vivo)."""
        self.carregar_modelo() # Tenta carregar do banco primeiro
        
        self.temporal.carregar_historico_csv(rodadas_historicas)
        self.covariancia.carregar_historico_csv(rodadas_historicas)
        self.temperatura.carregar_historico_csv(rodadas_historicas)
        self.risco.carregar_historico_csv(rodadas_historicas)
        self.estrategia.carregar_historico_csv(rodadas_historicas)

        # Pre-popula a calibração com um backtest rápido das últimas 300 rodadas do histórico
        self._historico_resultados.clear()
        if len(rodadas_historicas) > 150:
            amostra_teste = rodadas_historicas[-300:]
            from memoria import Memoria
            mem_sim = Memoria()
            hist_previo = rodadas_historicas[:-300]
            for r in hist_previo[-150:]:
                mem_sim.registrar(r.multiplicador, r.timestamp, r.temperatura)

            self._em_backtest = True  # não conta rounds ao vivo durante backtest
            for i, r in enumerate(amostra_teste):
                fp_antigo = self._falsos_positivos_consecutivos
                sinal = self.processar(mem_sim)
                self._falsos_positivos_consecutivos = fp_antigo
                
                if sinal.estado == "ENTRAR":
                    acertou = False
                    for gale in range(config.MAX_GALE + 1):
                        if i + gale < len(amostra_teste):
                            if amostra_teste[i + gale].multiplicador >= self.alvo:
                                acertou = True
                                break
                    self._historico_resultados.append({"score": sinal.score_final, "acertou": acertou})

                mem_sim.registrar(r.multiplicador, r.timestamp, r.temperatura)

            self._em_backtest = False  # backtest concluído

    def registrar_resultado(self, score_entrada: float, acertou: bool, mult_real: float = 0.0, gale_nivel: int = 0):
        """Chamado após cada ciclo para calibração contínua e feedback ao AgentePadrao."""
        self._historico_resultados.append({"score": score_entrada, "acertou": acertou})
        if len(self._historico_resultados) > 500:
            self._historico_resultados.pop(0)

        # Após qualquer indicação finalizada, aplicamos o cooldown de 5 a 8 minutos (15 a 24 rodadas)
        self._rounds_de_pausa_pos_red = random.randint(15, 24)
        
        if not acertou:
            self._falsos_positivos_consecutivos += 1
        else:
            self._falsos_positivos_consecutivos = 0

        # Propaga feedback para os agentes (Padrao e Bloco)
        self.padrao.registrar_resultado(acertou, mult_real)
        self.bloco.registrar_resultado(acertou, gale_nivel=gale_nivel)

    def _fator_tempo(self) -> float:
        # Entre 22h e 04h = penalidade extra
        hora = datetime.now().hour
        if hora >= 22 or hora <= 4:
            return 0.08
        return 0.0

    def salvar_modelo(self):
        """Serializa o estado dos agentes em JSON e salva no banco."""
        dados = {
            "covariancia": {k: dict(v) for k, v in self.covariancia._matriz.items()},
            "temperatura": {k: dict(v) for k, v in self.temperatura._hist_temp.items()},
            "temporal_blocos": {k: dict(v) for k, v in self.temporal._hist_bloco.items()},
            "temporal_dia": {k: dict(v) for k, v in self.temporal._hist_dia.items()},
            "risco_prob": self.risco._prob_historica
        }
        banco.salvar_modelo(self.alvo, json.dumps(dados))

    def carregar_modelo(self):
        """Restaura o estado dos agentes do JSON do banco, se existir."""
        json_str = banco.carregar_modelo(self.alvo)
        if not json_str:
            return
        
        try:
            dados = json.loads(json_str)
            from collections import defaultdict
            
            # Covariancia
            if "covariancia" in dados:
                self.covariancia._matriz.clear()
                for k, v in dados["covariancia"].items():
                    self.covariancia._matriz[k] = {"total": v["total"], "acertos": v["acertos"]}
            
            # Temperatura
            if "temperatura" in dados:
                self.temperatura._hist_temp.clear()
                for k, v in dados["temperatura"].items():
                    self.temperatura._hist_temp[int(k)] = {"total": v["total"], "acertos": v["acertos"]}
            
            # Temporal
            if "temporal_blocos" in dados:
                self.temporal._hist_bloco.clear()
                for k, v in dados["temporal_blocos"].items():
                    self.temporal._hist_bloco[k] = {"total": v["total"], "acertos": v["acertos"]}
            
            if "temporal_dia" in dados:
                self.temporal._hist_dia.clear()
                for k, v in dados["temporal_dia"].items():
                    self.temporal._hist_dia[k] = {"total": v["total"], "acertos": v["acertos"]}
            
            # Risco
            if "risco_prob" in dados:
                self.risco._prob_historica = dados["risco_prob"]
                
        except Exception as e:
            import logging
            logging.getLogger("orquestrador").error(f"Erro ao carregar modelo alvo {self.alvo}: {e}")

    def _penalidade_falsos_positivos(self) -> float:
        fp = self._falsos_positivos_consecutivos
        if fp >= 3: return 0.08
        if fp >= 2: return 0.04
        if fp >= 1: return 0.02
        return 0.0

    def estatisticas_calibracao(self) -> dict:
        """Retorna métricas de acurácia do sistema para exibição no dashboard."""
        if not self._historico_resultados:
            return {}
        total = len(self._historico_resultados)
        acertos = sum(1 for r in self._historico_resultados if r["acertou"])
        scores_entrada = [r["score"] for r in self._historico_resultados]
        return {
            "total_sinais": total,
            "acertos": acertos,
            "taxa_acerto": round(acertos / total, 4) if total else 0,
            "score_medio": round(sum(scores_entrada) / total, 3) if total else 0,
            "falsos_positivos_consecutivos": self._falsos_positivos_consecutivos,
        }

    def obter_dados_inteligencia_global(self) -> dict:
        """Extrai insights e estatísticas de maior assertividade do corpus de 30k e dados SQLite."""
        # Top 3 blocos com maior assertividade histórica
        blocos_validos = [
            (b, h["total"], h["acertos"] / h["total"])
            for b, h in self.temporal._hist_bloco.items()
            if h["total"] >= 30
        ]
        blocos_validos.sort(key=lambda x: x[2], reverse=True)
        top_blocos = [
            {"bloco": b, "total": tot, "taxa": round(tx, 3)}
            for b, tot, tx in blocos_validos[:3]
        ]

        # Top 3 padrões de covariância com maior assertividade histórica
        pads_validos = [
            (p, h["total"], h["acertos"] / h["total"])
            for p, h in self.covariancia._matriz.items()
            if h["total"] >= 20
        ]
        pads_validos.sort(key=lambda x: x[2], reverse=True)
        top_padroes = [
            {"padrao": p, "total": tot, "taxa": round(tx, 3)}
            for p, tot, tx in pads_validos[:3]
        ]

        return {
            "top_blocos": top_blocos,
            "top_padroes": top_padroes
        }


def _formatar_mensagem(estado, score, vereditos, resumo, penalidade) -> str:
    icone = {"ENTRAR":"✅","ATENCAO":"⚠️","AGUARDAR":"⏳","ABORTAR":"🛑"}.get(estado,"")

    favor    = [v for v in vereditos if v.estado in ("ENTRAR", "ATENCAO")]
    contra   = [v for v in vereditos if v.estado == "AGUARDAR"]
    bloqueio = [v for v in vereditos if v.estado == "ABORTAR"]

    linhas = [f"{icone} Score={score:.3f}" + (f" | penalidade +{penalidade:.0%}" if penalidade > 0 else "")]

    if favor:
        linhas.append("A FAVOR: " + " | ".join(f"{v.agente.upper()}({v.score:.2f}) {v.motivo.split('|')[0].strip()}" for v in favor))

    if bloqueio:
        linhas.append("BLOQUEIO: " + " | ".join(f"{v.agente.upper()} {v.motivo}" for v in bloqueio))
    elif contra:
        # Mostrar o principal motivo de espera
        principal = max(contra, key=lambda v: v.score)
        outros = [v.agente.upper() for v in contra if v != principal]
        linhas.append(f"AGUARDAR: {principal.agente.upper()} {principal.motivo}" +
                      (f" | também: {', '.join(outros)}" if outros else ""))

    if estado == "AGUARDAR" and len(favor) > 0:
        linhas.append(f"Pontos positivos detectados ({len(favor)}/6) — aguardar confirmação de streak/compressão")
    elif estado == "ENTRAR":
        linhas.append(f"CONSENSO: {len(favor)}/6 agentes favoráveis — entrada recomendada")
    elif estado == "ATENCAO":
        linhas.append(f"PARCIAL: {len(favor)}/6 favoráveis — preparar entrada no próximo sinal de compressão")

    return "\n".join(linhas)
