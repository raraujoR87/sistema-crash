from collections import defaultdict
from .base import AgenteBase, Veredito
import config


class AgenteTemperatura(AgenteBase):
    nome = "temperatura"

    def __init__(self, alvo: float):
        self.alvo = alvo
        # temp(1 a 5) -> {"total": int, "acertos": int}
        self._estatisticas: dict[int, dict] = defaultdict(lambda: {"total": 0, "acertos": 0})
        # temperatura (int 1-5) -> {total, acertos} — carregado do histórico
        self._hist_temp: dict[int, dict] = defaultdict(lambda: {"total": 0, "acertos": 0})

    def carregar_historico_csv(self, rodadas_historicas: list):
        """Aprende P(mult >= self.alvo | temperatura = T) do histórico."""
        self._hist_temp.clear()
        for r in rodadas_historicas:
            temp = getattr(r, "temperatura", 0)
            if not temp:
                continue
            self._hist_temp[temp]["total"] += 1
            if r.multiplicador >= self.alvo:
                self._hist_temp[temp]["acertos"] += 1

    def analisar(self, memoria) -> Veredito:
        rodadas = memoria.snapshot()
        if len(rodadas) < 5:
            return Veredito(self.nome, 0.5, "AGUARDAR", "Dados insuficientes", {})

        # Extrai dinamicamente as últimas temperaturas válidas (> 0) da memória
        temp_janela = [r.temperatura for r in rodadas if getattr(r, "temperatura", 0) > 0]
        temp_janela = temp_janela[-20:] # limite de 20 para tendência

        if not temp_janela:
            return Veredito(self.nome, 0.5, "AGUARDAR", "Temperatura não disponível", {})

        temp_atual = temp_janela[-1]
        temp_media = sum(temp_janela) / len(temp_janela)

        # ── Tendência de temperatura ─────────────────────────────────────
        # Compara primeira metade vs segunda metade da janela
        meio = len(temp_janela) // 2
        if meio > 0:
            media_velha = sum(temp_janela[:meio]) / meio
            media_nova  = sum(temp_janela[meio:]) / (len(temp_janela) - meio)
            tendencia = media_nova - media_velha  # + = subindo, - = caindo
        else:
            tendencia = 0.0

        # ── P histórica para temperatura atual ───────────────────────────
        h = self._hist_temp.get(temp_atual, {"total": 0, "acertos": 0})
        n_hist = h["total"]
        taxa_hist = (h["acertos"] / n_hist) if n_hist >= 20 else None

        # ── Transição: temp subindo após reds (liberação esperada) ───────
        reds_recentes = sum(1 for r in rodadas[-10:] if r.multiplicador < self.alvo)
        transicao_positiva = (tendencia > 0.3 and reds_recentes >= 3)

        # ── Cálculo de score ─────────────────────────────────────────────
        if temp_atual >= 4:
            # Temperatura alta: jogo em distribuição
            score_base = 0.70
            estado = "ENTRAR"
            motivo = f"Temp={temp_atual} (alta) — jogo em distribuição active"
        elif temp_atual >= 3:
            score_base = 0.55
            estado = "ATENCAO"
            motivo = f"Temp={temp_atual} (média) — zona de transição"
        elif temp_atual == 2:
            score_base = 0.35
            estado = "AGUARDAR"
            motivo = f"Temp={temp_atual} (baixa) — retenção moderada"
        else:  # temp == 1
            score_base = 0.20
            estado = "AGUARDAR"
            motivo = f"Temp={temp_atual} (mínima) — retenção severa"

        # Bônus por transição positiva (subindo após reds)
        if transicao_positiva:
            score_base = min(score_base + 0.15, 0.90)
            if estado == "AGUARDAR":
                estado = "ATENCAO"
            motivo += f" | Transicao: temp subindo (delta={tendencia:+.1f}) com {reds_recentes} reds"

        # Bônus/penalidade por taxa histórica
        if taxa_hist is not None:
            delta_hist = (taxa_hist - 0.65) * 0.3   # normalizado em torno do alvo
            score_base = min(max(score_base + delta_hist, 0.05), 0.95)
            motivo += f" | P_hist(temp={temp_atual})={taxa_hist:.1%}(n={n_hist})"

        return Veredito(
            agente=self.nome,
            score=round(score_base, 3),
            estado=estado,
            motivo=motivo,
            dados={
                "temp_atual":   temp_atual,
                "temp_media":   round(temp_media, 2),
                "tendencia":    round(tendencia, 3),
                "taxa_hist":    round(taxa_hist, 4) if taxa_hist else None,
                "n_hist":       n_hist,
                "reds_recentes":reds_recentes,
                "transicao":    transicao_positiva,
            },
        )
