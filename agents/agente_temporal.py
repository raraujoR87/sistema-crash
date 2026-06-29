from collections import defaultdict
from .base import AgenteBase, Veredito
import config


class AgenteTemporal(AgenteBase):
    nome = "temporal"

    def __init__(self, alvo: float):
        self.alvo = alvo
        # bloco_id (HH:MM) -> {total, acertos}
        self._hist_bloco: dict[str, dict] = defaultdict(lambda: {"total": 0, "acertos": 0})
        # bloco_dia (DIA_HH:MM) -> {total, acertos}
        self._hist_dia:   dict[str, dict] = defaultdict(lambda: {"total": 0, "acertos": 0})
        # bloco_id -> {temperatura -> {total, acertos}}  (correlação temperatura × bloco)
        self._hist_temp:  dict[str, dict] = defaultdict(lambda: defaultdict(lambda: {"total": 0, "acertos": 0}))

    def carregar_historico_csv(self, rodadas_historicas: list):
        """Alimentar com dados do CSV ou SQLite antes de iniciar o loop ao vivo."""
        self._hist_dia.clear()
        self._hist_temp.clear()
        for r in rodadas_historicas:
            bloco = getattr(r, "bloco_id", "")
            bloco_dia = getattr(r, "bloco_dia", "") or bloco
            temp = getattr(r, "temperatura", 0)

            self._hist_bloco[bloco]["total"] += 1
            if r.multiplicador >= self.alvo:
                self._hist_bloco[bloco]["acertos"] += 1

            if bloco_dia:
                self._hist_dia[bloco_dia]["total"] += 1
                if r.multiplicador >= self.alvo:
                    self._hist_dia[bloco_dia]["acertos"] += 1

            if temp and bloco:
                self._hist_temp[bloco][temp]["total"] += 1
                if r.multiplicador >= self.alvo:
                    self._hist_temp[bloco][temp]["acertos"] += 1

    def analisar(self, memoria) -> Veredito:
        ultimas = memoria.snapshot()
        bloco_id  = memoria.bloco_atual()
        if not bloco_id or not ultimas:
            return Veredito(self.nome, 0.5, "AGUARDAR", "Bloco não identificado", {})

        # Tenta usar bloco_dia (mais específico) se tiver dados
        bloco_dia = getattr(ultimas[-1], "bloco_dia", "") if ultimas else ""
        temp_atual = getattr(ultimas[-1], "temperatura", 0) if ultimas else 0

        # ── 1. Frequência por bloco genérico (HH:MM) ─────────────────────
        h_bloco = self._hist_bloco.get(bloco_id, {"total": 0, "acertos": 0})
        n_bloco, ok_bloco = h_bloco["total"], h_bloco["acertos"]
        taxa_bloco = ok_bloco / n_bloco if n_bloco >= 30 else None

        # ── 2. Frequência por dia da semana + bloco (mais fino) ───────────
        h_dia = self._hist_dia.get(bloco_dia, {"total": 0, "acertos": 0})
        n_dia, ok_dia = h_dia["total"], h_dia["acertos"]
        taxa_dia = ok_dia / n_dia if n_dia >= 15 else None

        # ── 3. Correlação temperatura × bloco ────────────────────────────
        taxa_temp = None
        n_temp = 0
        if temp_atual and bloco_id in self._hist_temp:
            ht = self._hist_temp[bloco_id].get(temp_atual, {"total": 0, "acertos": 0})
            n_temp = ht["total"]
            if n_temp >= 10:
                taxa_temp = ht["acertos"] / n_temp

        # ── Score composto ────────────────────────────────────────────────
        # Prioridade: dia_semana > bloco_genérico > neutro
        # Temperatura entra como ajuste de ±0.05 sobre a taxa base
        taxa_base = taxa_dia if taxa_dia is not None else taxa_bloco
        fonte = "dia_semana" if taxa_dia is not None else ("bloco" if taxa_bloco is not None else None)
        n_base  = n_dia if taxa_dia is not None else n_bloco

        if taxa_base is None:
            return Veredito(
                self.nome, 0.50, "AGUARDAR",
                f"Bloco {bloco_id} sem histórico suficiente (n={n_bloco})",
                {"bloco": bloco_id, "bloco_dia": bloco_dia, "n": n_bloco,
                 "taxa_historica": None, "fonte": "insuficiente"},
            )

        # Ajuste por temperatura (se temos dados)
        ajuste_temp = 0.0
        if taxa_temp is not None:
            delta = taxa_temp - taxa_base
            ajuste_temp = round(delta * 0.5, 3)   # peso 50% do delta

        taxa_efetiva = min(max(taxa_base + ajuste_temp * 0.5, 0.0), 1.0)

        # Limiar de blocos ruins: veto se taxa < 44%
        if taxa_efetiva < 0.44:
            score  = 0.20
            estado = "AGUARDAR"
            motivo = (f"Bloco {bloco_id} desfavorável: {taxa_efetiva:.1%} "
                      f"[{fonte}, n={n_base}]")
        elif taxa_efetiva < 0.52:
            score  = 0.45
            estado = "AGUARDAR"
            motivo = (f"Bloco {bloco_id}: {taxa_efetiva:.1%} neutro "
                      f"[{fonte}, n={n_base}]")
        elif taxa_efetiva < 0.60:
            score  = 0.65
            estado = "ATENCAO"
            motivo = (f"Bloco {bloco_id}: {taxa_efetiva:.1%} favorável "
                      f"[{fonte}, n={n_base}]")
        else:
            score  = 0.85
            estado = "ENTRAR"
            motivo = (f"Bloco {bloco_id}: {taxa_efetiva:.1%} histórico de acerto "
                      f"[{fonte}, n={n_base}]")

        if ajuste_temp != 0.0:
            sinal_temp = "+" if ajuste_temp > 0 else ""
            motivo += f" | temp={temp_atual} ajuste={sinal_temp}{ajuste_temp:.3f}"

        return Veredito(
            agente=self.nome,
            score=round(score, 3),
            estado=estado,
            motivo=motivo,
            dados={
                "bloco": bloco_id,
                "bloco_dia": bloco_dia,
                "n": n_base,
                "taxa_historica": round(taxa_base, 4) if taxa_base else None,
                "taxa_efetiva":   round(taxa_efetiva, 4),
                "taxa_temp":      round(taxa_temp, 4) if taxa_temp else None,
                "n_temp":         n_temp,
                "fonte":          fonte,
                "ajuste_temp":    ajuste_temp,
            },
        )
