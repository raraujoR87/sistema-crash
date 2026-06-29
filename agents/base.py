# agents/base.py — Contrato comum de todos os agentes.

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Veredito:
    agente: str
    score: float          # 0.0 a 1.0 — confiança de que AGORA é propício para entrar
    estado: str           # AGUARDAR | ATENCAO | ENTRAR | ABORTAR
    motivo: str           # Frase curta explicando o raciocínio
    dados: dict           # Métricas brutas para o orquestrador/log


class AgenteBase(ABC):
    nome: str = "base"

    @abstractmethod
    def analisar(self, memoria) -> Veredito:
        """Recebe o objeto Memoria e devolve um Veredito."""
        ...
