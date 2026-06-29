# memoria.py — Buffer deslizante compartilhado entre todos os agentes.
# Centraliza o estado do sistema em memória RAM.

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import threading

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import threading
import config

DIAS_SEMANA = ["SEG", "TER", "QUA", "QUI", "SEX", "SAB", "DOM"]


@dataclass
class Rodada:
    multiplicador: float
    timestamp: datetime
    temperatura: int = 0          # 1-5 do SSE tipminer
    bloco_id: str = ""            # ex: "14:20"
    bloco_dia: str = ""           # ex: "SEG_14:20" (dia semana + bloco)

    def __post_init__(self):
        self.bloco_id  = _bloco(self.timestamp)
        self.bloco_dia = _bloco_dia(self.timestamp)


def _bloco(ts: datetime) -> str:
    minuto_arredondado = (ts.minute // 10) * 10
    return f"{ts.hour:02d}:{minuto_arredondado:02d}"


def _bloco_dia(ts: datetime) -> str:
    dia = DIAS_SEMANA[ts.weekday()]
    minuto_arredondado = (ts.minute // 10) * 10
    return f"{dia}_{ts.hour:02d}:{minuto_arredondado:02d}"


class Memoria:
    """Thread-safe. Todos os agentes leem daqui."""

    def __init__(self):
        self._lock  = threading.RLock()
        self._buffer: deque[Rodada] = deque(maxlen=config.JANELA_MEMORIA)

    def registrar(self, multiplicador: float, timestamp: Optional[datetime] = None,
                  temperatura: int = 0) -> Rodada:
        rodada = Rodada(
            multiplicador=multiplicador,
            timestamp=timestamp or datetime.now(),
            temperatura=temperatura,
        )
        with self._lock:
            self._buffer.append(rodada)
        return rodada

    def snapshot(self) -> list[Rodada]:
        with self._lock:
            return list(self._buffer)

    def ultimas(self, n: int) -> list[Rodada]:
        with self._lock:
            buf = list(self._buffer)
        return buf[-n:] if len(buf) >= n else buf

    def total(self) -> int:
        with self._lock:
            return len(self._buffer)

    def bloco_atual(self) -> str:
        with self._lock:
            if not self._buffer:
                return ""
            return self._buffer[-1].bloco_id
