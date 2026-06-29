from collections import defaultdict
from .base import AgenteBase, Veredito
import config


JANELA_PADRAO = 3   # Tamanho da sequência de categorias analisada


def _label(cat: str) -> str:
    """Converte nome interno da categoria para label legível."""
    return config.CATEGORIAS_LABEL.get(cat, cat)


class AgenteCovariancia(AgenteBase):
    nome = "covariancia"

    def __init__(self, alvo: float):
        self.alvo = alvo
        # padrão_str -> {"total": int, "acertos": int}
        self._matriz: dict[str, dict] = defaultdict(lambda: {"total": 0, "acertos": 0})

    def carregar_historico_csv(self, rodadas_historicas: list):
        """Popula a matriz de transição a partir dos dados históricos."""
        self._matriz.clear()
        cats = [config.classificar_rodada(r.multiplicador, self.alvo) for r in rodadas_historicas]
        mults = [r.multiplicador for r in rodadas_historicas]
        for i in range(JANELA_PADRAO, len(cats)):
            padrao = ">".join(cats[i - JANELA_PADRAO: i])
            self._matriz[padrao]["total"] += 1
            if mults[i] >= self.alvo:
                self._matriz[padrao]["acertos"] += 1

    def analisar(self, memoria) -> Veredito:
        rodadas = memoria.snapshot()
        if len(rodadas) < JANELA_PADRAO + 1:
            return Veredito(self.nome, 0.5, "AGUARDAR", "Dados insuficientes", {})

        # Padrão das últimas N categorias
        ultimas_cats = [config.classificar_rodada(r.multiplicador, self.alvo) for r in rodadas[-JANELA_PADRAO:]]
        padrao_chave = ">".join(ultimas_cats)
        padrao_legivel = " → ".join(_label(c) for c in ultimas_cats)

        h = self._matriz.get(padrao_chave, {"total": 0, "acertos": 0})
        total   = h["total"]
        acertos = h["acertos"]

        if total < 20:
            return Veredito(
                self.nome, 0.50, "AGUARDAR",
                f"Padrão '{padrao_legivel}' raro (n={total})",
                {"padrao": padrao_legivel, "n": total, "prob_condicional": None},
            )

        prob = acertos / total

        if prob >= 0.70:
            score = 0.90
            estado = "ENTRAR"
        elif prob >= 0.58:
            score = 0.68
            estado = "ATENCAO"
        elif prob >= 0.45:
            score = 0.48
            estado = "AGUARDAR"
        else:
            score = 0.20
            estado = "AGUARDAR"

        motivo = f"Sequência {padrao_legivel} | Prob próx >= {self.alvo}x: {prob:.1%} (n={total})"

        return Veredito(
            agente=self.nome,
            score=round(score, 3),
            estado=estado,
            motivo=motivo,
            dados={"padrao": padrao_legivel, "n": total, "prob_condicional": round(prob, 4)},
        )

