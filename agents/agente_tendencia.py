# agente_tendencia.py — Detecta fase do mercado (ALTA/BAIXA/NEUTRA) em janela deslizante.
#
# Lógica:
#   1. Janela principal (15 rounds): % acima do alvo → define fase atual
#   2. Momentum (últimos 5 vs 5 anteriores): aceleração da fase
#   3. Micro-estrutura: sequência atual de baixas (streak) peso extra
#   4. Filtra ruído com EWM (média móvel exponencial simples)

import math
from .base import AgenteBase, Veredito


class AgenteTendencia(AgenteBase):
    nome = "tendencia"

    def __init__(self, alvo: float, janela: int = 18):
        self.alvo   = alvo
        self.janela = janela

    def analisar(self, memoria) -> Veredito:
        rodadas = memoria.snapshot()
        if len(rodadas) < 8:
            return Veredito(self.nome, 0.5, "AGUARDAR", "Dados insuficientes para tendência", {})

        # Janela principal
        jan  = list(rodadas)[-self.janela:]
        mults = [r.multiplicador for r in jan]

        pct_alta   = sum(1 for m in mults if m >= self.alvo) / len(mults)
        pct_baixa  = 1.0 - pct_alta
        media      = sum(mults) / len(mults)
        variancia  = sum((m - media)**2 for m in mults) / len(mults)
        volatilidade = math.sqrt(variancia)

        # EWM simples para suavizar pct_alta
        alpha = 0.3
        ewm   = mults[0]
        for m in mults[1:]:
            ewm = alpha * m + (1 - alpha) * ewm
        ewm_norm = min(ewm / max(self.alvo, 1.01), 2.0)  # > 1 = acima do alvo

        # Momentum: últimos 5 vs 5 anteriores
        if len(mults) >= 10:
            rec   = mults[-5:]
            ant   = mults[-10:-5]
            p_rec = sum(1 for m in rec if m >= self.alvo) / 5
            p_ant = sum(1 for m in ant if m >= self.alvo) / 5
            momentum = p_rec - p_ant   # positivo = melhorando, negativo = piorando
        else:
            momentum = 0.0

        # Streak de baixas atuais
        streak_baixas = 0
        for r in reversed(list(rodadas)):
            if r.multiplicador < self.alvo:
                streak_baixas += 1
            else:
                break

        # Classificação de fase
        if pct_alta >= 0.60:
            fase        = "ALTA"
            confianca   = min((pct_alta - 0.50) * 2.5, 1.0)
            descricao   = f"Mercado FORTE: {pct_alta:.0%} rounds acima de {self.alvo}x"
        elif pct_alta >= 0.48:
            fase        = "NEUTRA"
            confianca   = 0.50
            descricao   = f"Mercado EQUILIBRADO: {pct_alta:.0%} acima do alvo (±)"
        else:
            fase        = "BAIXA"
            confianca   = min((0.50 - pct_alta) * 2.5, 1.0)
            descricao   = f"Mercado FRACO: só {pct_alta:.0%} rounds acima de {self.alvo}x"

        # Ajuste de momentum
        if momentum > 0.20 and fase != "ALTA":
            descricao += f" | ↗ Momentum +{momentum:.0%} (virando)"
        elif momentum < -0.20 and fase != "BAIXA":
            descricao += f" | ↘ Momentum {momentum:.0%} (revertendo)"

        # Score para o orquestrador:
        #   ALTA → score alto (favorece entrada)
        #   BAIXA → score baixo (veta ou reduz)
        #   NEUTRA → score médio
        if fase == "ALTA":
            score  = 0.65 + confianca * 0.25
            estado = "ENTRAR" if score >= 0.80 else "ATENCAO"
        elif fase == "NEUTRA":
            score  = 0.50
            estado = "AGUARDAR"
        else:  # BAIXA
            score  = max(0.10, 0.40 - confianca * 0.30)
            # Streak longo de baixas é veto direto
            if streak_baixas >= 6:
                score  = 0.05
                estado = "ABORTAR"
                descricao = f"VETO: {streak_baixas} rounds consecutivos abaixo de {self.alvo}x"
            else:
                estado = "AGUARDAR"

        dados = {
            "fase":          fase,
            "pct_alta":      round(pct_alta, 3),
            "pct_baixa":     round(pct_baixa, 3),
            "media_janela":  round(media, 3),
            "volatilidade":  round(volatilidade, 3),
            "momentum":      round(momentum, 3),
            "ewm":           round(ewm, 3),
            "streak_baixas": streak_baixas,
            "janela_n":      len(jan),
        }

        return Veredito(self.nome, round(score, 3), estado, descricao, dados)
