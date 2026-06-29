import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── ALVOS E MODELOS ──────────────────────────────────────────────────────────
ALVOS_ATIVOS = [1.50, 2.00]  # Sistema processa e avalia ambos simultaneamente
MAX_GALE = 2                 # Níveis máximos de Martingale (G0, G1, G2)
STAKE_BASE = 10.0            # Valor base da entrada em reais
RTP_JOGO = 0.95              # Return To Player matemático do jogo (ex: 95%)

# ── INTELIGÊNCIA ARTIFICIAL (GEMINI) ─────────────────────────────────────────
# Substitua pela sua chave do Google Gemini Studio
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# ── JANELA TEMPORAL E RETREINO ───────────────────────────────────────────────
BLOCO_MINUTOS = 10           # Tamanho do bloco de análise em minutos
JANELA_MEMORIA = 150         # Rodadas mantidas em RAM para análise local
JANELA_STREAK_MAX = 20       # Máximo de rodadas retroativas para contar streak
RETREINO_DIAS = 2            # Intervalo entre os retreinos automáticos

# ── EMA — MARGEM DE BAIXA ────────────────────────────────────────────────────
EMA_ALPHA = 0.18             # Fator de decaimento (< = mais lento, > = mais reativo)
LIMIAR_RETENCAO_SEVERA = 1.15
LIMIAR_RETENCAO_MODERADA = 1.35
LIMIAR_MERCADO_SOLTO = 2.50

# ── GATILHO DINÂMICO ─────────────────────────────────────────────────────────
GATILHO_CONTRAIDO = 2
GATILHO_BASE = 4
GATILHO_EXPANDIDO = 6
GATILHO_SEVERO = 8

# ── PESOS DOS AGENTES (somam 1.0) ────────────────────────────────────────────
PESO_AGENTE_STREAK      = 0.15
PESO_AGENTE_TEMPORAL    = 0.10
PESO_AGENTE_MARGEM      = 0.15
PESO_AGENTE_COVARIANCA  = 0.10
PESO_AGENTE_TEMPERATURA = 0.10
PESO_AGENTE_RISCO       = 0.10
PESO_AGENTE_IA          = 0.10
PESO_AGENTE_RTP         = 0.20   # Alto peso: matemática pura do casino

# ── THRESHOLD DE SINAL ───────────────────────────────────────────────────────
SCORE_MINIMO_ENTRADA = 0.60
SCORE_ALERTA_ATENCAO = 0.50

# ── CATEGORIAS DE MULTIPLICADOR (Dinâmicas por Alvo) ─────────────────────────
CATEGORIAS_LABEL = {
    "MICRO_CRASH":   "Crash (<1.2x)",
    "PRETO_CURTO":   "Baixo (1.2-alvo)",
    "ALVO_PRIMARIO": "Alvo (hit)",
    "VERDE_PADRAO":  "Verde (alvo+)",
    "SUPER_VELA":    "Foguete (5x+)",
}

def classificar_rodada(mult: float, alvo: float) -> str:
    """Classifica o multiplicador com base no alvo específico daquele modelo."""
    if mult < 1.20:
        return "MICRO_CRASH"
    if mult < alvo:
        return "PRETO_CURTO"
    
    teto_verde = max(5.00, alvo)
    
    if mult < max(2.00, alvo):
        return "ALVO_PRIMARIO"
    elif mult < teto_verde:
        return "VERDE_PADRAO"
    else:
        return "SUPER_VELA"


# ── TELEGRAM (preencher na Fase 2) ───────────────────────────────────────────
TELEGRAM_TOKEN   = ""
TELEGRAM_CHAT_ID = ""

# ── BANCO LOCAL ──────────────────────────────────────────────────────────────
DB_PATH = "data/sessoes.db"
LOG_PATH = "logs/sistema.log"
