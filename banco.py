# banco.py — Persistência SQLite. Grava rodadas ao vivo + sinais + resultados.
# Os dados ao vivo são a base para o retreino diário dos agentes.

import sqlite3, json, csv
from datetime import datetime
from pathlib import Path
from config import DB_PATH


def _conn():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def inicializar():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS rodadas (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                bloco       TEXT NOT NULL,
                bloco_dia   TEXT NOT NULL DEFAULT '',
                mult        REAL NOT NULL,
                categoria   TEXT NOT NULL,
                temperatura INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Migração: adiciona colunas novas se já existir tabela antiga
        _migrar(c, "rodadas", [
            ("bloco_dia",   "TEXT NOT NULL DEFAULT ''"),
            ("temperatura", "INTEGER NOT NULL DEFAULT 0"),
        ])
        c.execute("""
            CREATE TABLE IF NOT EXISTS sinais (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         TEXT NOT NULL,
                bloco      TEXT NOT NULL,
                estado     TEXT NOT NULL,
                score      REAL NOT NULL,
                mensagem   TEXT NOT NULL,
                dados_json TEXT NOT NULL,
                rodada_ts  TEXT DEFAULT ''
            )
        """)
        _migrar(c, "sinais", [("rodada_ts", "TEXT DEFAULT ''")])
        c.execute("""
            CREATE TABLE IF NOT EXISTS resultados (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                sinal_id   INTEGER,
                ts         TEXT NOT NULL,
                ganhou     INTEGER NOT NULL,
                mult_real  REAL NOT NULL,
                gale_usado INTEGER NOT NULL DEFAULT 0
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS modelos_treinados (
                alvo       REAL PRIMARY KEY,
                ts_treino  TEXT NOT NULL,
                dados_json TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS feedback_ia (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         TEXT NOT NULL,
                bloco      TEXT NOT NULL,
                prompt     TEXT NOT NULL,
                resposta   TEXT NOT NULL,
                score      REAL NOT NULL,
                mult_real  REAL,
                acertou    INTEGER
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS configuracoes (
                chave  TEXT PRIMARY KEY,
                valor  TEXT NOT NULL,
                ts     TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)


def _migrar(conn, tabela: str, colunas: list[tuple[str, str]]):
    """Adiciona colunas sem derrubar dados existentes."""
    existentes = {row[1] for row in conn.execute(f"PRAGMA table_info({tabela})")}
    for nome, definicao in colunas:
        if nome not in existentes:
            conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {nome} {definicao}")


def gravar_rodada(rodada):
    with _conn() as c:
        c.execute(
            "INSERT INTO rodadas (ts, bloco, bloco_dia, mult, categoria, temperatura) VALUES (?,?,?,?,?,?)",
            (rodada.timestamp.isoformat(), rodada.bloco_id, rodada.bloco_dia,
             rodada.multiplicador, "", rodada.temperatura),
        )


def gravar_sinal(sinal, bloco: str, rodada_ts: str = ""):
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO sinais (ts, bloco, estado, score, mensagem, dados_json, rodada_ts) VALUES (?,?,?,?,?,?,?)",
            (datetime.now().isoformat(), bloco, sinal.estado,
             sinal.score_final, sinal.mensagem, json.dumps(sinal.dados_agentes), rodada_ts),
        )
        return cur.lastrowid


def gravar_resultado(sinal_id: int, ganhou: bool, mult_real: float, gale: int = 0):
    with _conn() as c:
        c.execute(
            "INSERT INTO resultados (sinal_id, ts, ganhou, mult_real, gale_usado) VALUES (?,?,?,?,?)",
            (sinal_id, datetime.now().isoformat(), int(ganhou), mult_real, gale),
        )


def resumo_sessao() -> dict:
    with _conn() as c:
        row = c.execute("""
            SELECT COUNT(*), SUM(ganhou), AVG(gale_usado)
            FROM resultados
            WHERE ts >= date('now', 'start of day')
        """).fetchone()
    total, ganhos, gale_medio = row
    total  = total  or 0
    ganhos = ganhos or 0
    return {
        "total_sinais_hoje": total,
        "acertos": ganhos,
        "taxa_acerto": round(ganhos / total, 4) if total > 0 else 0,
        "gale_medio": round(gale_medio or 0, 2),
    }


def total_rodadas_ao_vivo() -> int:
    with _conn() as c:
        row = c.execute("SELECT COUNT(*) FROM rodadas").fetchone()
    return row[0] if row else 0


def exportar_rodadas_para_retreino(caminho_csv: str):
    """Exporta todas as rodadas ao vivo do SQLite para CSV de retreino.
    Formato compatível com loader_csv.py (Numero, Mult, Data, Horario, Temperatura).
    """
    with _conn() as c:
        rows = c.execute(
            "SELECT id, ts, bloco, bloco_dia, mult, categoria, temperatura FROM rodadas ORDER BY id"
        ).fetchall()

    Path(caminho_csv).parent.mkdir(parents=True, exist_ok=True)
    with open(caminho_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["Numero", "Mult", "Data", "Horario", "BlocoDia", "Temperatura"])
        for row in rows:
            rid, ts, bloco, bloco_dia, mult, cat, temp = row
            try:
                dt = datetime.fromisoformat(ts)
                data_str = dt.strftime("%d/%m/%Y")
                hora_str = dt.strftime("%H:%M:%S")
            except Exception:
                data_str = hora_str = ""
            writer.writerow([rid, mult, data_str, hora_str, bloco_dia or bloco, temp or 0])

    return len(rows)


def salvar_modelo(alvo: float, dados_json: str):
    with _conn() as c:
        c.execute("""
            INSERT INTO modelos_treinados (alvo, ts_treino, dados_json)
            VALUES (?, ?, ?)
            ON CONFLICT(alvo) DO UPDATE SET ts_treino=excluded.ts_treino, dados_json=excluded.dados_json
        """, (alvo, datetime.now().isoformat(), dados_json))


def carregar_modelo(alvo: float) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT dados_json FROM modelos_treinados WHERE alvo = ?", (alvo,)).fetchone()
    return row[0] if row else None


def salvar_config(chave: str, valor: str):
    """Grava ou atualiza uma configuração persistente."""
    with _conn() as c:
        c.execute("""
            INSERT INTO configuracoes (chave, valor, ts)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor, ts=excluded.ts
        """, (chave, valor))


def ler_config(chave: str, padrao: str = "") -> str:
    """Lê uma configuração persistente; retorna `padrao` se não existir."""
    with _conn() as c:
        row = c.execute(
            "SELECT valor FROM configuracoes WHERE chave = ?", (chave,)
        ).fetchone()
    return row[0] if row else padrao


def ler_configs(prefixo: str = "") -> dict:
    """Retorna todas as configs (opcionalmente filtradas por prefixo) como dict."""
    with _conn() as c:
        if prefixo:
            rows = c.execute(
                "SELECT chave, valor FROM configuracoes WHERE chave LIKE ?",
                (prefixo + "%",)
            ).fetchall()
        else:
            rows = c.execute("SELECT chave, valor FROM configuracoes").fetchall()
    return {k: v for k, v in rows}


def gravar_feedback_ia(bloco: str, prompt: str, resposta: str, score: float) -> int:
    with _conn() as c:
        cur = c.execute("""
            INSERT INTO feedback_ia (ts, bloco, prompt, resposta, score)
            VALUES (?, ?, ?, ?, ?)
        """, (datetime.now().isoformat(), bloco, prompt, resposta, score))
        return cur.lastrowid


def atualizar_feedback_ia(feedback_id: int, mult_real: float, acertou: bool):
    with _conn() as c:
        c.execute("""
            UPDATE feedback_ia SET mult_real = ?, acertou = ? WHERE id = ?
        """, (mult_real, int(acertou), feedback_id))

def ler_historico_db(limite: int = 500) -> list[dict]:
    """Retorna as últimas rodadas processadas do banco com o respectivo sinal (se houver)."""
    with _conn() as c:
        rows = c.execute("""
            SELECT r.id, r.ts, r.bloco, r.mult, r.temperatura,
                   s.estado, s.score, s.dados_json
            FROM rodadas r
            LEFT JOIN sinais s ON s.rodada_ts = r.ts
            ORDER BY r.id DESC
            LIMIT ?
        """, (limite,)).fetchall()
        
    res = []
    for r in rows:
        item = {
            "id": r[0],
            "ts": r[1],
            "bloco": r[2],
            "mult": r[3],
            "temp": r[4]
        }
        if r[5]:  # tem sinal
            item["sinal"] = {
                "estado": r[5],
                "score": r[6],
                "dados": json.loads(r[7]) if r[7] else {}
            }
        res.append(item)
    return res
