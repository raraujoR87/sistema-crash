import logging
from collections import defaultdict
from agents.base import AgenteBase, Veredito

log = logging.getLogger("AgenteEstrategia")

class AgenteEstrategia(AgenteBase):
    nome = "estrategia"
    
    def __init__(self, alvo: float):
        self.alvo = alvo
        self.padroes_fortes = {}  # tuple(sequencia) -> win_rate
        self.tamanhos = [3, 4, 5, 6]

    def _converter_outcome(self, multiplicador: float) -> str:
        return "G" if multiplicador >= self.alvo else "R"

    def carregar_historico_csv(self, rodadas: list):
        """
        Treina o agente varrendo o histórico.
        Para cada tamanho (3 a 6), pega janelas e olha o resultado *seguinte*.
        """
        if len(rodadas) < 100:
            return

        ocorrencias = defaultdict(lambda: {"wins": 0, "total": 0})
        outcomes = [self._converter_outcome(r.multiplicador) for r in rodadas]

        # Mapeia todas as sequências e seus desfechos (rodada seguinte)
        for tam in self.tamanhos:
            for i in range(len(outcomes) - tam):
                # Extrai a janela de tamanho 'tam'
                janela = tuple(outcomes[i : i+tam])
                # O resultado seguinte
                resultado_seguinte = outcomes[i+tam]
                
                ocorrencias[janela]["total"] += 1
                if resultado_seguinte == "G":
                    ocorrencias[janela]["wins"] += 1

        self.padroes_fortes.clear()
        
        # Filtra os padrões com amostragem decente (min 5 ocorrencias) e >= 85% WR
        for seq, stats in ocorrencias.items():
            if stats["total"] >= 5:  # Evita anomalias estatísticas de sequências muito raras
                wr = stats["wins"] / stats["total"]
                if wr >= 0.85:
                    self.padroes_fortes[seq] = wr

        log.info(f"[Estrategia] Treinado com {len(rodadas)} rodadas. Padrões catalogados (>85% WR): {len(self.padroes_fortes)}")

    def analisar(self, memoria) -> Veredito:
        if memoria.total() < max(self.tamanhos):
            return Veredito(
                agente=self.nome,
                score=0.0,
                estado="AGUARDAR",
                motivo="Sem histórico suficiente.",
                dados={}
            )

        # Pega as últimas rodadas (limite máximo de 6)
        recentes = []
        for rd in memoria.ultimas(6):
            recentes.append(self._converter_outcome(rd.multiplicador))

        # Analisa do maior padrão para o menor para priorizar o mais específico
        padrao_encontrado = None
        wr_encontrado = 0.0

        for tam in sorted(self.tamanhos, reverse=True):
            janela_atual = tuple(recentes[-tam:])
            if janela_atual in self.padroes_fortes:
                padrao_encontrado = janela_atual
                wr_encontrado = self.padroes_fortes[janela_atual]
                break

        if padrao_encontrado:
            # Score baseia-se diretamente na assertividade (0.85 a 1.0)
            # Retorna ENTRAR pois encontramos uma mina de ouro estatística
            score = wr_encontrado
            return Veredito(
                agente=self.nome,
                score=score,
                estado="ENTRAR",
                motivo=f"Padrão forte detectado: {'-'.join(padrao_encontrado)} (WR: {wr_encontrado*100:.1f}%)",
                dados={"padrao": padrao_encontrado, "wr": wr_encontrado}
            )

        # Se não bateu com nada mágico, é neutro
        return Veredito(
            agente=self.nome,
            score=0.0,
            estado="AGUARDAR",
            motivo="Nenhum padrão forte detectado na janela recente.",
            dados={}
        )
