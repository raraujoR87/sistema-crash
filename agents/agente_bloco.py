# agente_bloco.py — Análise de performance por período do dia.
#
# Divide o dia em blocos de N minutos (padrão 15min).
# Para cada bloco armazena: n_wins, n_losses, vitórias por nível de gale.
#
# Insights gerados:
#   • Bloco com WR < 35% → ABORTAR (não operar nesse horário)
#   • Bloco onde vitórias concentradas em G2+  → "aguardar gale"
#   • Melhor/pior blocos do histórico
#
# Persistência: to_dict / from_dict (salvo junto com padrao_state.json)

import math
from datetime import datetime
from collections import defaultdict
from .base import AgenteBase, Veredito

_MIN_AMOSTRAS = 8   # mínimo para considerar o bloco confiável


class AgenteBloco(AgenteBase):
    nome = "bloco"

    def __init__(self, alvo: float, granularidade_min: int = 15):
        self.alvo  = alvo
        self.gran  = granularidade_min

        # hist[bloco_key] = {"wins":0,"losses":0,"g0":0,"g1":0,"g2":0,"g3":0}
        self._hist: dict = defaultdict(lambda: {"wins":0,"losses":0,"g0":0,"g1":0,"g2":0,"g3":0})

        # Entrada em aberto aguardando resultado
        self._pendente: dict | None = None

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _key(self, ts: datetime) -> str:
        """Arredonda para o início do bloco: ex. 14:23 → '14:15'."""
        m = (ts.minute // self.gran) * self.gran
        return f"{ts.hour:02d}:{m:02d}"

    def _stats(self, key: str) -> dict:
        b = self._hist.get(key, {})
        total  = b.get("wins", 0) + b.get("losses", 0)
        if total == 0:
            return {"total": 0, "wr": None, "gale_medio": 0, "bloco": key}
        wr    = b["wins"] / total
        # Média ponderada de gale nas vitórias
        n_win = b.get("wins", 1) or 1
        gm    = sum(i * b.get(f"g{i}", 0) for i in range(4)) / n_win
        return {
            "bloco":       key,
            "wins":        b["wins"],
            "losses":      b["losses"],
            "total":       total,
            "wr":          round(wr, 3),
            "gale_medio":  round(gm, 2),
            "g0":          b.get("g0", 0),
            "g1":          b.get("g1", 0),
            "g2":          b.get("g2", 0),
            "g3":          b.get("g3", 0),
        }

    # ── Feedback externo ──────────────────────────────────────────────────────

    def registrar_entrada(self, ts: datetime):
        self._pendente = {"bloco": self._key(ts), "ts": ts.isoformat()}

    def registrar_resultado(self, acertou: bool, gale_nivel: int):
        if not self._pendente:
            return
        key = self._pendente["bloco"]
        b   = self._hist[key]
        if acertou:
            b["wins"] += 1
            gk = f"g{min(gale_nivel, 3)}"
            b[gk] = b.get(gk, 0) + 1
        else:
            b["losses"] += 1
        self._pendente = None

    # ── Análise ───────────────────────────────────────────────────────────────

    def analisar(self, memoria) -> Veredito:
        rodadas = memoria.snapshot()
        if not rodadas:
            return Veredito(self.nome, 0.5, "AGUARDAR", "Sem dados", {})

        ts    = rodadas[-1].timestamp
        key   = self._key(ts)
        stats = self._stats(key)

        if stats["total"] < _MIN_AMOSTRAS:
            return Veredito(
                self.nome, 0.55, "AGUARDAR",
                f"Bloco {key}: sem histórico suficiente ({stats['total']}/{_MIN_AMOSTRAS} amostras)",
                stats,
            )

        wr  = stats["wr"]
        gm  = stats["gale_medio"]

        # Distribuição de gale nas vitórias
        n_win = stats["wins"] or 1
        pct_g0 = stats["g0"] / n_win
        pct_g2p = (stats["g2"] + stats["g3"]) / n_win

        if wr >= 0.65:
            score  = 0.85
            estado = "ENTRAR"
            motivo = (f"Bloco {key} EXCELENTE: WR={wr:.0%} | "
                      f"{stats['total']} entradas | gale médio G{gm:.1f}")
        elif wr >= 0.52:
            score  = 0.65
            estado = "ATENCAO"
            if pct_g2p > 0.50:
                motivo = (f"Bloco {key}: WR={wr:.0%} — atenção: "
                          f"{pct_g2p:.0%} das vitórias ocorreram em G2+, aguardar gale")
            else:
                motivo = f"Bloco {key}: período razoável WR={wr:.0%} | G0={pct_g0:.0%}"
        elif wr >= 0.38:
            score  = 0.30
            estado = "AGUARDAR"
            if pct_g2p > 0.60:
                motivo = (f"Bloco {key}: fraco direto ({wr:.0%}) "
                          f"— {pct_g2p:.0%} em G2+, considere entrar só após G1")
            else:
                motivo = f"Bloco {key}: período fraco WR={wr:.0%}, aguardar melhora"
        else:
            score  = 0.02
            estado = "ABORTAR"
            motivo = (f"BLOQUEADO {key}: histórico muito fraco "
                      f"WR={wr:.0%} ({stats['total']} entradas) — não operar")

        return Veredito(self.nome, round(score, 3), estado, motivo, stats)

    # ── Relatórios ────────────────────────────────────────────────────────────

    def relatorio_blocos(self) -> list[dict]:
        """Lista todos os blocos com amostras, ordenados por horário."""
        rows = []
        for key in sorted(self._hist.keys()):
            s = self._stats(key)
            if s["total"] >= 3:
                rows.append(s)
        return rows

    def melhores_blocos(self, top: int = 3) -> list[dict]:
        r = [s for s in self.relatorio_blocos() if s["total"] >= _MIN_AMOSTRAS]
        return sorted(r, key=lambda x: -x["wr"])[:top]

    def piores_blocos(self, top: int = 3) -> list[dict]:
        r = [s for s in self.relatorio_blocos() if s["total"] >= _MIN_AMOSTRAS]
        return sorted(r, key=lambda x: x["wr"])[:top]

    def insight_str(self) -> str:
        """String compacta de insights para Telegram/Gemini."""
        mb = self.melhores_blocos(3)
        pb = self.piores_blocos(2)
        lines = []
        if mb:
            m_str = ", ".join(f"{b['bloco']} ({b['wr']:.0%})" for b in mb)
            lines.append(f"✅ Melhores horários: {m_str}")
        if pb:
            p_str = ", ".join(f"{b['bloco']} ({b['wr']:.0%})" for b in pb)
            lines.append(f"⚠️ Evitar: {p_str}")
        # Destaca blocos com padrão de entrada tardia
        for b in self.relatorio_blocos():
            if b["total"] >= _MIN_AMOSTRAS and b["wins"] > 0:
                n_win = b["wins"]
                pct_g2p = (b["g2"] + b["g3"]) / n_win
                if pct_g2p > 0.60:
                    lines.append(f"💡 {b['bloco']}: {pct_g2p:.0%} vitórias em G2+ — entrar no gale")
        return "\n".join(lines) if lines else "Sem padrões claros ainda"

    # ── Persistência ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {"hist": {k: dict(v) for k, v in self._hist.items()}}

    def from_dict(self, d: dict):
        for k, v in d.get("hist", {}).items():
            self._hist[k] = v
