import math
from agents.base import Veredito
import config
from memoria import Memoria

class AgenteRTP:
    """
    Agente matemático que calcula a divergência entre a saída real de multiplicadores
    e a saída teórica baseada no RTP, fatiando as análises em regiões de risco.
    """
    def __init__(self, alvo: float):
        self.nome = "rtp"
        self.alvo = alvo
        self.rtp = getattr(config, 'RTP_JOGO', 0.95)
        
        # Probabilidades Teóricas Cumulativas P(X < x) = 1 - (RTP / x)
        # 1.00 exato (House Edge)
        self.p_instant = 1.0 - self.rtp
        # < 1.20
        self.p_below_120 = 1.0 - (self.rtp / 1.20)
        # < 1.40
        self.p_below_140 = 1.0 - (self.rtp / 1.40)
        # < alvo (ex: 1.50)
        self.p_below_alvo = 1.0 - (self.rtp / self.alvo)

        # Regiões exatas
        self.prob_regioes = {
            "instant": self.p_instant,                                      # 1.00
            "baixo":   self.p_below_120 - self.p_instant,                   # 1.01 - 1.19
            "medio":   self.p_below_140 - self.p_below_120,                 # 1.20 - 1.39
            "alerta":  self.p_below_alvo - self.p_below_140,                # 1.40 - 1.49
            "green":   1.0 - self.p_below_alvo                              # >= 1.50
        }

    def analisar(self, memoria: Memoria) -> Veredito:
        # Analisa até as últimas 150 rodadas (~1 hora)
        rodadas = memoria.ultimas(150)
        total = len(rodadas)

        if total < 30:
            return Veredito(self.nome, 0.5, "AGUARDAR", "RTP: Aquecimento (<30 rodadas)", {})

        # Contagem real
        counts = {"instant": 0, "baixo": 0, "medio": 0, "alerta": 0, "green": 0}
        
        for r in rodadas:
            if r.multiplicador == 1.00:
                counts["instant"] += 1
            elif r.multiplicador < 1.20:
                counts["baixo"] += 1
            elif r.multiplicador < 1.40:
                counts["medio"] += 1
            elif r.multiplicador < self.alvo:
                counts["alerta"] += 1
            else:
                counts["green"] += 1

        # Calcula o delta (real - teórico)
        deltas = {}
        dados_detalhados = {}
        for reg, teo in self.prob_regioes.items():
            real = counts[reg] / total
            delta = real - teo
            deltas[reg] = delta
            dados_detalhados[f"{reg}_real"] = round(real * 100, 1)
            dados_detalhados[f"{reg}_teo"] = round(teo * 100, 1)
            dados_detalhados[f"{reg}_delta"] = round(delta * 100, 1)

        dados_detalhados["amostra"] = total

        # LÓGICA DE DÉFICIT / SUPERÁVIT DA ZONA DE GREEN (ALVO)
        # delta_green < 0 = Déficit de pagamentos (Casino deve pagar greens em breve)
        # delta_green > 0 = Superávit de pagamentos (Casino pagou demais, deve recolher)
        delta_green = deltas["green"]
        
        # Déficit nas piores regiões vermelhas
        deficit_red_severo = (deltas["baixo"] <= -0.06 or deltas["instant"] <= -0.03)

        # 1. VETO DE ALTO RISCO (Casino pagou demais E as piores zonas estão devendo muito)
        if delta_green >= 0.08 and deficit_red_severo:
            motivo = f"Risco Extremo: Mercado pagou muitos greens (Superávit: +{delta_green*100:.1f}%) e deve quebras baixas. Correção iminente."
            return Veredito(self.nome, 0.05, "ABORTAR", motivo, dados_detalhados)
            
        # 2. VETO MODERADO (Casino pagou MUITOS greens, tendência forte de correção)
        elif delta_green >= 0.10:
            motivo = f"Correção de RTP: Mercado com muito superávit de greens (+{delta_green*100:.1f}%). Casino deve recolher."
            return Veredito(self.nome, 0.15, "ABORTAR", motivo, dados_detalhados)

        # 3. OPORTUNIDADE EXTREMA (Casino está devendo muito green)
        elif delta_green <= -0.05:
            motivo = f"Janela de Pagamento Forte: Déficit agudo de greens ({delta_green*100:.1f}%). O RTP forçará altas."
            return Veredito(self.nome, 0.95, "ENTRAR", motivo, dados_detalhados)
            
        # 4. OPORTUNIDADE FORTE
        elif delta_green <= -0.02:
            motivo = f"Inclinação a pagar: Déficit de greens em {delta_green*100:.1f}%."
            return Veredito(self.nome, 0.85, "ENTRAR", motivo, dados_detalhados)

        # 5. OPORTUNIDADE MODERADA
        elif delta_green <= 0.0:
            motivo = f"Mercado matematicamente favorável (Delta Green: {delta_green*100:.1f}%)."
            return Veredito(self.nome, 0.75, "ENTRAR", motivo, dados_detalhados)

        # 6. SUPORTE LEVE (Mesmo com leve superávit, permite que os outros agentes achem entradas)
        elif delta_green <= 0.04:
            motivo = f"RTP Neutro/Leve Superávit (Delta Green: {delta_green*100:.1f}%). Caminho livre para padrões."
            return Veredito(self.nome, 0.65, "ENTRAR", motivo, dados_detalhados)

        # Se houver déficit apenas na zona baixa, alertar
        elif deltas["baixo"] <= -0.07:
            motivo = f"Atenção: Mercado devendo quebras baixas. (Déficit red baixo: {deltas['baixo']*100:.1f}%)."
            return Veredito(self.nome, 0.40, "AGUARDAR", motivo, dados_detalhados)

        # Mercado com superávit moderado (Aguarda)
        else:
            motivo = f"RTP com superávit de greens (Delta Green: {delta_green*100:.1f}%). Risco Moderado."
            return Veredito(self.nome, 0.50, "AGUARDAR", motivo, dados_detalhados)
