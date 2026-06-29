from .base import AgenteBase, Veredito
import config


class AgenteRisco(AgenteBase):
    nome = "risco"

    def __init__(self, alvo: float):
        self.alvo = alvo
        self._banca = 0.0         # atualizado externamente
        self._perdas_ciclo = 0    # quantos gales usados no ciclo atual
        self._ciclos_perdidos = 0 # ciclos consecutivos com perda total
        self._prob_historica: float | None = None  # P(>= alvo) do treino 30k

    def atualizar_banca(self, banca: float):
        self._banca = banca

    def registrar_resultado(self, ganhou: bool):
        if ganhou:
            self._perdas_ciclo = 0
            self._ciclos_perdidos = 0
        else:
            self._ciclos_perdidos += 1

    def carregar_historico_csv(self, rodadas_historicas: list):
        """Calcula a probabilidade histórica real de >= ALVO no corpus inteiro."""
        if not rodadas_historicas:
            return
        acertos = sum(1 for r in rodadas_historicas if r.multiplicador >= self.alvo)
        self._prob_historica = acertos / len(rodadas_historicas)

    def _calcular_ev(self, prob_entrada: float) -> dict:
        """EV do ciclo com Martingale até config.MAX_GALE."""
        stakes = [config.STAKE_BASE * (2 ** i) for i in range(config.MAX_GALE + 1)]
        risco_total = sum(stakes)

        # Probabilidade de acertar em algum nível do gale
        p_falha_total = (1 - prob_entrada) ** (config.MAX_GALE + 1)
        p_acerto = 1 - p_falha_total

        lucro_se_ganhar = config.STAKE_BASE * (self.alvo - 1)
        ev = p_acerto * lucro_se_ganhar - p_falha_total * risco_total

        return {
            "ev": round(ev, 2),
            "risco_total": round(risco_total, 2),
            "p_acerto_ciclo": round(p_acerto, 4),
            "p_ruina_ciclo": round(p_falha_total, 4),
        }

    def analisar(self, memoria) -> Veredito:
        rodadas = memoria.snapshot()
        if len(rodadas) < 10:
            return Veredito(self.nome, 0.5, "AGUARDAR", "Dados insuficientes", {})

        # Usar TODAS as rodadas disponíveis na memória (150), não apenas 50
        acertos_mem = sum(1 for r in rodadas if r.multiplicador >= self.alvo)
        prob_mem = acertos_mem / len(rodadas)

        # Mescla com probabilidade histórica do treino (30k) se disponível
        # Peso 60% histórico + 40% ao vivo para estabilidade
        if self._prob_historica is not None:
            prob_est = self._prob_historica * 0.6 + prob_mem * 0.4
        else:
            prob_est = prob_mem

        ev_data = self._calcular_ev(prob_est)
        ev = ev_data["ev"]

        # Verificar risco de ruína acumulada (ciclos perdidos consecutivos)
        if self._ciclos_perdidos >= 3:
            return Veredito(
                self.nome, 0.0, "ABORTAR",
                f"3 ciclos consecutivos perdidos — stop loss ativado",
                {**ev_data, "prob_recente": round(prob_est, 4),
                 "prob_memoria": round(prob_mem, 4),
                 "prob_historica": round(self._prob_historica or 0, 4),
                 "n_amostras": len(rodadas),
                 "ciclos_perdidos": self._ciclos_perdidos},
            )

        # Verificar banca mínima
        stakes = [config.STAKE_BASE * (2 ** i) for i in range(config.MAX_GALE + 1)]
        banca_minima = sum(stakes) * 5   # 5 ciclos de reserva
        if self._banca > 0 and self._banca < banca_minima:
            return Veredito(
                self.nome, 0.20, "AGUARDAR",
                f"Banca R${self._banca:.0f} abaixo do mínimo recomendado R${banca_minima:.0f}",
                {**ev_data, "prob_recente": round(prob_est, 4),
                 "prob_memoria": round(prob_mem, 4),
                 "prob_historica": round(self._prob_historica or 0, 4),
                 "n_amostras": len(rodadas)},
            )

        # Score baseado em EV positivo e probabilidade estimada
        hist_str = f"{self._prob_historica:.1%}" if self._prob_historica is not None else "n/a"
        if ev > 0 and prob_est >= 0.55:
            score = 0.80
            estado = "ENTRAR"
            motivo = (f"EV +R${ev:.2f} | P(ciclo)={ev_data['p_acerto_ciclo']:.1%} | "
                      f"WR hist={hist_str} ao vivo={prob_mem:.1%}")
        elif prob_est >= 0.50:
            score = 0.60
            estado = "ATENCAO"
            motivo = (f"EV R${ev:.2f} | P(ciclo)={ev_data['p_acerto_ciclo']:.1%} | "
                      f"WR hist={hist_str} ao vivo={prob_mem:.1%} (sustentavel)")
        elif prob_est >= 0.40:
            score = 0.40
            estado = "AGUARDAR"
            motivo = (f"EV R${ev:.2f} | WR combinado={prob_est:.1%} | "
                      f"Risco moderado, aguardar melhora")
        else:
            score = 0.15
            estado = "AGUARDAR"
            motivo = (f"EV R${ev:.2f} | WR combinado={prob_est:.1%} | "
                      f"Risco elevado, mercado desfavoravel")

        return Veredito(
            agente=self.nome,
            score=round(score, 3),
            estado=estado,
            motivo=motivo,
            dados={**ev_data,
                   "prob_recente": round(prob_est, 4),
                   "prob_memoria": round(prob_mem, 4),
                   "prob_historica": round(self._prob_historica or 0, 4),
                   "n_amostras": len(rodadas)},
        )
