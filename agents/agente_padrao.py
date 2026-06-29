# agente_padrao.py — Memória vetorial de entradas + feedback de resultados.
#
# Como funciona:
#   1. A cada ENTRAR, grava um feature-vector do contexto atual.
#   2. Quando o resultado chega (win/loss), marca o vetor com o outcome.
#   3. Na próxima análise, compara o contexto atual com os K vizinhos mais
#      próximos da memória. Se a maioria são losses → veta ou reduz score.
#   4. Aprende também a sequência de multiplicadores via LSTM-manual:
#      uma célula LSTM leve treinada online com os resultados.
#
# Feature vector (dim=8):
#   [ema_rapida_norm, streak_norm, bloco_taxa, temp_norm,
#    score_entrada, hora_sin, hora_cos, p_hist_risco]

import math
import json
import logging
from collections import deque
from datetime import datetime
from .base import AgenteBase, Veredito

log = logging.getLogger("agente_padrao")

# ── LSTM leve (numpy-free, puro Python) ──────────────────────────────────────
def _sigmoid(x):
    x = max(-30.0, min(30.0, x))
    return 1.0 / (1.0 + math.exp(-x))

def _tanh(x):
    x = max(-30.0, min(30.0, x))
    return math.tanh(x)

class _LSTMCell:
    """LSTM de 1 camada, input_dim variável → hidden_dim fixo=8."""

    def __init__(self, input_dim: int = 4, hidden_dim: int = 8, lr: float = 0.01):
        self.h_dim = hidden_dim
        self.i_dim = input_dim
        self.lr    = lr
        n = input_dim + hidden_dim
        # Inicialização Xavier
        scale = math.sqrt(2.0 / (n + hidden_dim))
        def _mat(rows, cols):
            import random
            return [[random.gauss(0, scale) for _ in range(cols)] for _ in range(rows)]
        def _vec(d): return [0.0] * d
        # Pesos das 4 portas: i, f, g, o
        self.Wi=_mat(hidden_dim,n); self.bi=_vec(hidden_dim)
        self.Wf=_mat(hidden_dim,n); self.bf=[1.0]*hidden_dim  # forget bias=1
        self.Wg=_mat(hidden_dim,n); self.bg=_vec(hidden_dim)
        self.Wo=_mat(hidden_dim,n); self.bo=_vec(hidden_dim)
        # Camada de saída → escalar (prob)
        self.Wy=[scale for _ in range(hidden_dim)]; self.by=0.0
        # Estado
        self.h=[0.0]*hidden_dim; self.c=[0.0]*hidden_dim
        # Adam
        self._t=0
        self._m={k:[0.0]*len(v) for k,v in [('Wy',self.Wy)]}
        self._v={k:[0.0]*len(v) for k,v in [('Wy',self.Wy)]}

    def _mv(self, W, x):
        """Multiplica W (lista de linhas) por vetor x."""
        return [sum(W[i][j]*x[j] for j in range(len(x))) for i in range(len(W))]

    def _add(self, a, b):
        return [a[i]+b[i] for i in range(len(a))]

    def forward(self, x_t):
        """Avança um passo; retorna prob escalar."""
        xh = x_t + self.h  # concatena input + hidden
        i_g = [_sigmoid(v) for v in self._add(self._mv(self.Wi,xh), self.bi)]
        f_g = [_sigmoid(v) for v in self._add(self._mv(self.Wf,xh), self.bf)]
        g_g = [_tanh(v)    for v in self._add(self._mv(self.Wg,xh), self.bg)]
        o_g = [_sigmoid(v) for v in self._add(self._mv(self.Wo,xh), self.bo)]
        self.c = [f_g[k]*self.c[k] + i_g[k]*g_g[k] for k in range(self.h_dim)]
        self.h = [o_g[k]*_tanh(self.c[k])           for k in range(self.h_dim)]
        logit  = sum(self.Wy[k]*self.h[k] for k in range(self.h_dim)) + self.by
        return _sigmoid(logit)

    def backward_output(self, prob, target):
        """SGD simplificado apenas na camada de saída (evita BPTT completo)."""
        err = prob - target  # gradiente da BCE
        self._t += 1
        b1, b2, eps = 0.9, 0.999, 1e-8
        for k in range(self.h_dim):
            g = err * self.h[k]
            self._m['Wy'][k] = b1*self._m['Wy'][k] + (1-b1)*g
            self._v['Wy'][k] = b2*self._v['Wy'][k] + (1-b2)*g*g
            m_hat = self._m['Wy'][k] / (1 - b1**self._t)
            v_hat = self._v['Wy'][k] / (1 - b2**self._t)
            self.Wy[k] -= self.lr * m_hat / (math.sqrt(v_hat) + eps)
        self.by -= self.lr * err

    def reset_state(self):
        self.h = [0.0]*self.h_dim
        self.c = [0.0]*self.h_dim

    def to_dict(self):
        return {'Wy': self.Wy, 'by': self.by, 'h': self.h, 'c': self.c, 't': self._t}

    def from_dict(self, d):
        self.Wy = d.get('Wy', self.Wy)
        self.by = d.get('by', self.by)
        self.h  = d.get('h',  self.h)
        self.c  = d.get('c',  self.c)
        self._t = d.get('t',  self._t)


# ── Feature helpers ───────────────────────────────────────────────────────────
def _hora_ciclica(ts: datetime):
    h = ts.hour + ts.minute / 60.0
    return math.sin(2*math.pi*h/24), math.cos(2*math.pi*h/24)

def _norm(v, lo, hi):
    return max(0.0, min(1.0, (v - lo) / max(hi - lo, 1e-6)))


class AgentePadrao(AgenteBase):
    """Memória de padrão + LSTM leve + cooldown pós-stop."""

    nome = "padrao"

    def __init__(self, alvo: float):
        self.alvo = alvo

        # Memória de entradas: {fv, outcome, ts}
        self._memoria: deque = deque(maxlen=300)
        self._pendente: dict | None = None   # entrada em aberto aguardando resultado

        # LSTM leve — processa sequência de multiplicadores normalizada
        self._lstm = _LSTMCell(input_dim=4, hidden_dim=8, lr=0.008)
        self._lstm_seq: deque = deque(maxlen=30)  # últimas 30 mults para alimentar LSTM
        self._n_lstm_treino = 0

        # Cooldown pós-stop
        self._cooldown_rounds  = 0   # rounds bloqueados que faltam
        self._stops_consec     = 0   # stops consecutivos sem win entre eles
        self._COOLDOWN_BASE    = 4   # rounds mínimos pós-stop
        self._COOLDOWN_MAX     = 15  # teto de cooldown

        # Estatísticas
        self._wins  = 0
        self._losses = 0

        # Blame tracking por agente
        # Registra quais agentes votaram ENTRAR/ATENCAO na última entrada
        _AGENTES_BLAME = ["streak","margem","temporal","covariancia","temperatura","risco","ia_gemini","tendencia"]
        self._blame: dict = {a: {"wins": 0, "losses": 0} for a in _AGENTES_BLAME}
        self._vereditos_pendentes: list = []  # vereditos da entrada em aberto

    # ── Feedback externo (chamado pelo orquestrador) ──────────────────────────

    def registrar_entrada(self, fv: list, ts: datetime, vereditos: list | None = None):
        """Grava feature-vector da entrada atual como pendente."""
        self._pendente = {"fv": fv, "ts": ts.isoformat()}
        self._vereditos_pendentes = vereditos or []

    def registrar_resultado(self, acertou: bool, mult_real: float):
        """Fecha a entrada pendente com o outcome e treina o LSTM."""
        # Blame tracking — atribui crédito/culpa a cada agente que votou ENTRAR
        for v in self._vereditos_pendentes:
            nome = v.get("agente") if isinstance(v, dict) else getattr(v, "agente", "")
            estado = v.get("estado") if isinstance(v, dict) else getattr(v, "estado", "")
            if nome in self._blame and estado in ("ENTRAR", "ATENCAO"):
                if acertou:
                    self._blame[nome]["wins"]   += 1
                else:
                    self._blame[nome]["losses"] += 1

        # Cooldown adaptativo
        if acertou:
            self._stops_consec = 0
            self._wins += 1
            if self._cooldown_rounds > 0:
                self._cooldown_rounds -= 2  # win abrevia cooldown
        else:
            self._stops_consec += 1
            self._losses += 1
            extra = self._COOLDOWN_BASE + self._stops_consec * 2
            self._cooldown_rounds = min(extra, self._COOLDOWN_MAX)
            log.info(f"AgentePadrao: stop #{self._stops_consec} → cooldown {self._cooldown_rounds} rounds")

        if self._pendente:
            self._memoria.append({
                "fv":      self._pendente["fv"],
                "outcome": 1 if acertou else 0,
                "ts":      self._pendente["ts"],
                "mult":    mult_real,
            })
            # Treina LSTM com a sequência acumulada
            if len(self._lstm_seq) >= 8:
                self._treinar_lstm(acertou)
            self._pendente = None

    def _treinar_lstm(self, acertou: bool):
        """Passa a sequência de mults pelo LSTM e faz 1 passo de SGD."""
        self._lstm.reset_state()
        seq = list(self._lstm_seq)
        for m in seq:
            x = self._mult_to_feat(m)
            prob = self._lstm.forward(x)
        target = 1.0 if acertou else 0.0
        self._lstm.backward_output(prob, target)
        self._n_lstm_treino += 1

    def _mult_to_feat(self, m: float) -> list:
        """Normaliza multiplicador para feature de input do LSTM."""
        cat = 0.0 if m < 1.2 else (0.25 if m < self.alvo else (0.75 if m < 5.0 else 1.0))
        return [
            _norm(m, 1.0, 10.0),
            cat,
            1.0 if m >= self.alvo else 0.0,
            _norm(math.log(max(m, 1.01)), 0.0, 2.3),
        ]

    def _similaridade(self, fv_a: list, fv_b: list) -> float:
        """Similaridade cosseno entre dois feature vectors."""
        dot = sum(a*b for a,b in zip(fv_a, fv_b))
        na  = math.sqrt(sum(a*a for a in fv_a)) + 1e-9
        nb  = math.sqrt(sum(b*b for b in fv_b)) + 1e-9
        return dot / (na * nb)

    def _knn_outcome(self, fv: list, k: int = 12) -> tuple[float, int]:
        """Retorna (taxa_win_vizinhos, n_vizinhos) dos K mais próximos."""
        if len(self._memoria) < k:
            return 0.5, 0
        sims = [(self._similaridade(fv, m["fv"]), m["outcome"]) for m in self._memoria]
        top  = sorted(sims, key=lambda x: -x[0])[:k]
        taxa = sum(o for _, o in top) / len(top)
        return taxa, len(top)

    def _lstm_prob(self) -> float:
        """Roda LSTM sobre a sequência atual e retorna P(win)."""
        if len(self._lstm_seq) < 5:
            return 0.5
        self._lstm.reset_state()
        for m in self._lstm_seq:
            p = self._lstm.forward(self._mult_to_feat(m))
        return p  # última saída = prob da próxima

    # ── Análise principal ─────────────────────────────────────────────────────

    def analisar(self, memoria) -> Veredito:
        rodadas = memoria.snapshot()
        if not rodadas:
            return Veredito(self.nome, 0.5, "AGUARDAR", "Sem dados", {})

        # Atualiza sequência LSTM com última rodada
        ultima = rodadas[-1]
        self._lstm_seq.append(ultima.multiplicador)

        # Decrementa cooldown
        if self._cooldown_rounds > 0:
            self._cooldown_rounds -= 1
            taxa_total = self._wins/(self._wins+self._losses) if (self._wins+self._losses) else 0
            return Veredito(
                self.nome, 0.10, "AGUARDAR",
                f"Cooldown pós-stop: {self._cooldown_rounds+1} rounds restantes | "
                f"WR sessão: {taxa_total:.0%}",
                {"cooldown": self._cooldown_rounds, "stops_consec": self._stops_consec,
                 "wins": self._wins, "losses": self._losses},
            )

        # Feature vector do momento atual
        ts      = rodadas[-1].timestamp
        h_sin, h_cos = _hora_ciclica(ts)
        dados   = {}
        ema     = getattr(rodadas[-1], '_ema_snapshot', None)

        # Extrai features disponíveis dos dados da rodada/memória
        reds_consec = sum(1 for r in reversed(rodadas) if r.multiplicador < self.alvo
                          and True or False)  # simplificado
        # Conta streak real
        streak = 0
        for r in reversed(rodadas):
            if r.multiplicador < self.alvo: streak += 1
            else: break
        ema_norm   = _norm(rodadas[-1].multiplicador, 1.0, 5.0)
        streak_n   = _norm(streak, 0, 10)
        temp_n     = _norm(getattr(rodadas[-1], "temperatura", 0), 0, 5)
        wins_total = self._wins + self._losses
        wr_n       = self._wins / max(wins_total, 1)

        fv = [ema_norm, streak_n, temp_n, h_sin, h_cos, wr_n, self._stops_consec / 5.0, 0.5]

        # K-NN pattern match
        taxa_knn, n_viz = self._knn_outcome(fv)

        # LSTM prob
        prob_lstm = self._lstm_prob()

        # Combina: 50% KNN + 50% LSTM (quando temos dados suficientes)
        if n_viz >= 8:
            prob = 0.5 * taxa_knn + 0.5 * prob_lstm
        elif n_viz >= 3:
            prob = 0.3 * taxa_knn + 0.7 * prob_lstm
        else:
            prob = prob_lstm

        # Decisão
        if self._stops_consec >= 2 and prob < 0.55:
            score  = 0.10
            estado = "ABORTAR"
            motivo = (f"Padrão de perda detectado: {self._stops_consec} stops consecutivos | "
                      f"P(win) KNN={taxa_knn:.0%} LSTM={prob_lstm:.0%}")
        elif prob >= 0.65:
            score  = 0.80
            estado = "ENTRAR"
            motivo = f"Padrão favorável | P(win)={prob:.0%} (KNN={taxa_knn:.0%} LSTM={prob_lstm:.0%} n={n_viz})"
        elif prob >= 0.52:
            score  = 0.55
            estado = "ATENCAO"
            motivo = f"Padrão neutro | P(win)={prob:.0%} (LSTM={prob_lstm:.0%} n={n_viz})"
        elif prob < 0.42:
            score  = 0.15
            estado = "AGUARDAR"
            motivo = f"Padrão desfavorável | P(win)={prob:.0%} (KNN={taxa_knn:.0%} LSTM={prob_lstm:.0%})"
        else:
            score  = 0.40
            estado = "AGUARDAR"
            motivo = f"Padrão inconclusivo | P(win)={prob:.0%} (n_treino_lstm={self._n_lstm_treino})"

        dados = {
            "prob_combinada":  round(prob, 3),
            "prob_lstm":       round(prob_lstm, 3),
            "taxa_knn":        round(taxa_knn, 3),
            "n_vizinhos":      n_viz,
            "n_lstm_treino":   self._n_lstm_treino,
            "stops_consec":    self._stops_consec,
            "cooldown_restante": self._cooldown_rounds,
            "fv":              [round(v, 3) for v in fv],
        }

        # Guarda fv para quando o resultado chegar
        self._ultimo_fv = fv
        self._ultima_ts = ts

        return Veredito(self.nome, round(score, 3), estado, motivo, dados)

    def blame_weights(self) -> dict:
        """Retorna multiplicador de peso por agente com base no histórico de blame.
        Agente com >65% de blame (muitos stops quando votou ENTRAR) → peso reduzido.
        Agente com <35% de blame (confiável) → peso mantido/reforçado.
        """
        weights = {}
        for nome, b in self._blame.items():
            total = b["wins"] + b["losses"]
            if total < 5:
                weights[nome] = 1.0  # sem dados suficientes → neutro
                continue
            loss_rate = b["losses"] / total
            if loss_rate >= 0.70:
                weights[nome] = 0.40   # muito culpado → peso fortemente reduzido
            elif loss_rate >= 0.55:
                weights[nome] = 0.65   # moderadamente culpado
            elif loss_rate <= 0.30:
                weights[nome] = 1.20   # muito confiável → reforçado
            elif loss_rate <= 0.45:
                weights[nome] = 1.05   # acima da média
            else:
                weights[nome] = 1.0
        return weights

    def blame_report(self) -> list:
        """Relatório de blame por agente para exibição no dashboard."""
        rows = []
        for nome, b in self._blame.items():
            total = b["wins"] + b["losses"]
            if total == 0:
                continue
            rows.append({
                "agente":     nome,
                "wins":       b["wins"],
                "losses":     b["losses"],
                "total":      total,
                "loss_rate":  round(b["losses"] / total, 3),
                "weight_mod": round(self.blame_weights().get(nome, 1.0), 2),
            })
        rows.sort(key=lambda x: -x["loss_rate"])
        return rows

    # ── Persistência ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "memoria":       list(self._memoria),
            "lstm":          self._lstm.to_dict(),
            "cooldown":      self._cooldown_rounds,
            "stops_consec":  self._stops_consec,
            "wins":          self._wins,
            "losses":        self._losses,
            "n_lstm_treino": self._n_lstm_treino,
            "blame":         self._blame,
        }

    def from_dict(self, d: dict):
        for item in d.get("memoria", []):
            self._memoria.append(item)
        if "lstm" in d:
            self._lstm.from_dict(d["lstm"])
        self._cooldown_rounds = d.get("cooldown", 0)
        self._stops_consec    = d.get("stops_consec", 0)
        self._wins            = d.get("wins", 0)
        self._losses          = d.get("losses", 0)
        self._n_lstm_treino   = d.get("n_lstm_treino", 0)
        if "blame" in d:
            for nome, b in d["blame"].items():
                if nome in self._blame:
                    self._blame[nome] = b
