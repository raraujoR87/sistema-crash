# agents/agente_margem.py
# Calcula a Margem de Baixa via EMA dupla (curta + longa).
# Detecta retenção agressiva e mede "velocidade" de queda.

from .base import AgenteBase, Veredito
import config


class AgenteMargem(AgenteBase):
    nome = "margem"

    def __init__(self, alvo: float):
        self.alvo = alvo
        self._ema_rapida = None   # alpha = EMA_ALPHA (reativa)
        self._ema_lenta  = None   # alpha = EMA_ALPHA/3 (tendência)
        self._ultima_rodada_processada = None

    def _atualizar_emas(self, rodadas):
        if not rodadas:
            return

        # Encontra se a última rodada processada ainda está no buffer
        idx = -1
        if self._ultima_rodada_processada is not None:
            for i, r in enumerate(rodadas):
                if r is self._ultima_rodada_processada:
                    idx = i
                    break

        if idx != -1:
            novas = rodadas[idx + 1:]
        else:
            novas = rodadas

        alpha_r = config.EMA_ALPHA
        alpha_l = config.EMA_ALPHA / 3
        for r in novas:
            v = r.multiplicador
            self._ema_rapida = v if self._ema_rapida is None else alpha_r * v + (1 - alpha_r) * self._ema_rapida
            self._ema_lenta  = v if self._ema_lenta  is None else alpha_l * v + (1 - alpha_l) * self._ema_lenta

        self._ultima_rodada_processada = rodadas[-1]

    def analisar(self, memoria) -> Veredito:
        rodadas = memoria.snapshot()
        if len(rodadas) < 10:
            return Veredito(self.nome, 0.0, "AGUARDAR", "Dados insuficientes", {})

        self._atualizar_emas(rodadas)
        ema_r = self._ema_rapida
        ema_l = self._ema_lenta
        divergencia = ema_r - ema_l   # negativo = queda acelerando

        # Classificar estado do mercado
        if ema_r < config.LIMIAR_RETENCAO_SEVERA:
            estado_mercado = "RETENCAO_SEVERA"
            # Em retenção severa, score sobe pois anomalia se aprofunda
            score = min(0.70 + abs(divergencia) * 0.1, 0.90)
            estado = "ATENCAO"
            motivo = f"EMA rápida={ema_r:.2f} — retenção severa (diverg={divergencia:.2f})"

        elif ema_r < config.LIMIAR_RETENCAO_MODERADA:
            estado_mercado = "RETENCAO_MODERADA"
            score = 0.55
            estado = "ATENCAO"
            motivo = f"EMA rápida={ema_r:.2f} — retenção moderada"

        elif ema_r > config.LIMIAR_MERCADO_SOLTO:
            estado_mercado = "MERCADO_SOLTO"
            # Mercado pagando bem: menos urgência para entrada defensiva
            score = 0.35
            estado = "AGUARDAR"
            motivo = f"EMA rápida={ema_r:.2f} — mercado solto, risco de entrada menor"

        else:
            estado_mercado = "DISTRIBUICAO_NORMAL"
            # Dentro do esperado: score neutro
            score = 0.50
            estado = "AGUARDAR"
            motivo = f"EMA rápida={ema_r:.2f} — distribuição normal"

        # Bonus: divergência EMAs indica aceleração de queda (exaustão próxima)
        if divergencia < -0.15 and estado_mercado in ("RETENCAO_SEVERA", "RETENCAO_MODERADA"):
            score = min(score + 0.08, 0.95)
            estado = "ENTRAR"
            motivo += " | Aceleração de queda detectada (divergência EMA)"

        return Veredito(
            agente=self.nome,
            score=round(score, 3),
            estado=estado,
            motivo=motivo,
            dados={
                "ema_rapida": round(ema_r, 3),
                "ema_lenta":  round(ema_l, 3),
                "divergencia": round(divergencia, 3),
                "estado_mercado": estado_mercado,
            },
        )
