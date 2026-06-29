# agents/agente_streak.py
# Analisa sequências consecutivas abaixo do alvo.
# Considera a PROFUNDIDADE da compressão, não apenas a contagem.

from .base import AgenteBase, Veredito
import config


class AgenteStreak(AgenteBase):
    nome = "streak"

    def __init__(self, alvo: float):
        self.alvo = alvo

    def analisar(self, memoria) -> Veredito:
        rodadas = memoria.snapshot()
        if len(rodadas) < 5:
            return Veredito(self.nome, 0.0, "AGUARDAR", "Dados insuficientes", {})

        # Contagem de reds consecutivos
        streak = 0
        soma_reds = 0.0
        for r in reversed(rodadas):
            if r.multiplicador < self.alvo:
                streak += 1
                soma_reds += r.multiplicador
            else:
                break

        media_reds = soma_reds / streak if streak > 0 else 0.0

        # Profundidade: quão baixos foram esses reds
        if streak == 0:
            score = 0.50
            estado = "AGUARDAR"
            motivo = "Sem sequência de reds (neutro)"
        else:
            # Determinar gatilho baseado na profundidade da compressão
            if media_reds < config.LIMIAR_RETENCAO_SEVERA:
                gatilho = config.GATILHO_SEVERO
                pressao = "severa"
            elif media_reds < 1.35:
                gatilho = config.GATILHO_EXPANDIDO
                pressao = "moderada"
            elif media_reds > config.LIMIAR_MERCADO_SOLTO:
                gatilho = config.GATILHO_CONTRAIDO
                pressao = "suave"
            else:
                gatilho = config.GATILHO_BASE
                pressao = "normal"

            progresso = min(streak / gatilho, 1.0)

            if streak >= gatilho:
                score = min(0.85 + (streak - gatilho) * 0.03, 0.97)
                estado = "ENTRAR"
                motivo = f"{streak} reds consecutivos (gatilho={gatilho}, pressão={pressao})"
            elif progresso >= 0.75:
                score = 0.55 + (progresso - 0.75) * 0.8
                estado = "ATENCAO"
                motivo = f"{streak}/{gatilho} reds — aproximando gatilho (pressão={pressao})"
            else:
                score = 0.30 + progresso * 0.25
                estado = "AGUARDAR"
                motivo = f"{streak}/{gatilho} reds — aguardando compressão (pressão={pressao})"

        return Veredito(
            agente=self.nome,
            score=round(score, 3),
            estado=estado,
            motivo=motivo,
            dados={
                "streak": streak,
                "media_reds": round(media_reds, 3),
                "gatilho_dinamico": gatilho if streak > 0 else config.GATILHO_BASE,
            },
        )
