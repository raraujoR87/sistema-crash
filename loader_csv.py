# loader_csv.py — Lê o CSV da Tipminer e devolve lista de Rodada para calibrar os agentes.

import csv
import re
from datetime import datetime
from pathlib import Path
from memoria import Rodada


FORMATOS_DATA = [
    "%d/%m/%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%Y-%m-%dT%H:%M:%S",
]


def _parse_dt(data_str: str, hora_str: str = "") -> datetime:
    texto = f"{data_str} {hora_str}".strip()
    for fmt in FORMATOS_DATA:
        try:
            return datetime.strptime(texto, fmt)
        except ValueError:
            continue
    # Fallback: usa agora (não ideal mas não trava a carga)
    return datetime.now()


def _parse_mult(valor: str) -> float:
    # Remove caracteres não numéricos (exceto ponto/vírgula)
    limpo = re.sub(r"[^\d.,]", "", valor).replace(",", ".")
    try:
        return float(limpo)
    except ValueError:
        return 0.0


def carregar(caminho: str) -> list[Rodada]:
    """
    Suporta CSVs da Tipminer com colunas: Número, Cor, Data, Horário
    ou qualquer CSV com coluna de multiplicador + timestamp detectável.
    """
    path = Path(caminho)
    if not path.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {caminho}")

    rodadas: list[Rodada] = []

    with open(path, encoding="utf-8-sig", newline="") as f:
        # Detectar delimitador
        amostra = f.read(2048)
        f.seek(0)
        delimitador = ";" if amostra.count(";") > amostra.count(",") else ","
        reader = csv.DictReader(f, delimiter=delimitador)

        for linha in reader:
            # Normalizar nomes de colunas
            cols = {k.strip().lower(): v.strip() for k, v in linha.items() if k}

            # Detectar coluna de multiplicador
            mult_raw = (cols.get("número") or cols.get("numero")
                        or cols.get("mult") or cols.get("multiplicador")
                        or cols.get("value") or "")
            if not mult_raw or "tipminer" in mult_raw.lower():
                continue

            mult = _parse_mult(mult_raw)
            if mult <= 0:
                continue

            # Detectar colunas de data/hora
            data_raw = cols.get("data") or cols.get("date") or ""
            hora_raw = cols.get("horário") or cols.get("horario") or cols.get("hora") or cols.get("time") or ""
            ts = _parse_dt(data_raw, hora_raw)

            rodadas.append(Rodada(multiplicador=mult, timestamp=ts))

    print(f"[loader] {len(rodadas)} rodadas carregadas de '{path.name}'")
    return rodadas
