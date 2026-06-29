import logging
import random
import httpx
import config

log = logging.getLogger("agente_marketing")

GIFS = {
    "vitoria": [
        "https://media.giphy.com/media/26tPcgtbCgksObmfe/giphy.mp4",
        "https://media.giphy.com/media/l0GoEqDf84Hn9s7C0/giphy.mp4"
    ],
    "streak_alto": [
        "https://media.giphy.com/media/l41JIkTxbCMZqOCZ2/giphy.mp4",
        "https://media.giphy.com/media/1xOfip1uaV9oK2A28D/giphy.mp4"
    ],
    "stop": [
        "https://media.giphy.com/media/xT8qB3utUzMWqdgO5y/giphy.mp4",
        "https://media.giphy.com/media/H4u08458rQ4NDLMNDQ/giphy.mp4"
    ],
    "recuperacao": [
        "https://media.giphy.com/media/1iUZaAgMOLlr2/giphy.mp4"
    ],
    "entrada_forte": [
        "https://media.giphy.com/media/26tPcgtbCgksObmfe/giphy.mp4"
    ],
    "aguardando": [
        "https://media.giphy.com/media/3o6Zt8qDiPE2cToJCE/giphy.mp4"
    ],
    "geral": [
        "https://media.giphy.com/media/d3mlE7uhX8KFgEmY/giphy.mp4"
    ]
}

def _gif_para_contexto(contexto: str) -> str:
    pool = GIFS.get(contexto, GIFS["geral"])
    return random.choice(pool)

async def _gemini_texto(prompt: str, temperatura: float = 0.85) -> str | None:
    if not config.GEMINI_API_KEY:
        return None
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"gemini-2.5-flash:generateContent?key={config.GEMINI_API_KEY}")
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperatura, "maxOutputTokens": 2048},
    }
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(url, json=payload, timeout=25.0)
        if r.status_code == 200:
            return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        log.warning(f"AgenteMarketing Gemini erro: {e}")
    return None

def _detectar_contexto(dados: dict) -> str:
    """Identifica o contexto mais relevante a partir dos dados ao vivo."""
    greens_seq  = dados.get("greens_seq", 0)    # greens consecutivos
    stops_seq   = dados.get("stops_seq", 0)      # stops consecutivos
    pl          = dados.get("pl_sessao", 0)
    recuperacao = dados.get("modo_recuperacao", False)
    tendencia   = dados.get("tendencia", "NEUTRA")
    sinal_cons  = dados.get("sinal_consolidado", "AGUARDAR")
    score_cons  = dados.get("score_consolidado", 0.0)

    if stops_seq >= 2:
        return "stop"
    if recuperacao and pl < -10:
        return "recuperacao"
    if sinal_cons == "ENTRAR" and score_cons >= 0.65:
        return "entrada_forte"
    if greens_seq >= 3:
        return "streak_alto"
    if greens_seq >= 1 and tendencia == "ALTA":
        return "vitoria"
    if tendencia == "BAIXA":
        return "aguardando"
    return "geral"

async def gerar_mensagem_marketing(dados: dict) -> tuple[str, str]:
    """
    Retorna (mensagem_texto, gif_url).
    Usa templates locais com dados dinâmicos para evitar chamadas desnecessárias
    ao Gemini. A IA é reservada apenas para o insight periódico (a cada 30min).
    """
    contexto = _detectar_contexto(dados)
    gif_url  = _gif_para_contexto(contexto)

    pl       = dados.get("pl_sessao", 0)
    wr       = dados.get("win_rate", 0)
    greens   = dados.get("greens_seq", 0)
    stops    = dados.get("stops_seq", 0)
    score_c  = dados.get("score_consolidado", 0.5)
    tend     = dados.get("tendencia", "NEUTRA")

    templates = {
        "vitoria": [
            f"🟢 Confirmado! Mercado em alta e sinal validado. Próximos minutos favoráveis! 🎯",
            f"🟢 GREEN batido! Modelo calibrado, tendência {tend}. WR: {wr:.0f}% 🎯",
            f"✅ Mais um green! Agentes convergindo, P&L: R${pl:+.0f}. Seguimos! 🚀",
        ],
        "streak_alto": [
            f"🔥 {greens} GREENS SEGUIDOS! Agentes alinhados e batendo metas! 🚀",
            f"🔥 Sequência de {greens}! Modelo on fire, WR {wr:.0f}%. Próximos rounds promissores! 💪",
        ],
        "stop": [
            "⚠️ Modelo detectou instabilidade. Seguramos as entradas pelos próximos minutos.",
            f"⚠️ Stop detectado. Agentes recalibrando para reverter. Tendência: {tend}.",
            f"🛑 Cautela! {stops} stops seguidos. Algoritmo em modo proteção. Paciência.",
        ],
        "recuperacao": [
            f"💪 Reajuste de stake ativo. Algoritmo buscando reversão no curto prazo.",
            f"💪 Modo recuperação ON. Agentes buscando ponto de virada. P&L: R${pl:+.0f}",
        ],
        "entrada_forte": [
            f"🚀 SINAL FORTE! Score {score_c:.2f} com alta convergência dos agentes! 🟢",
            f"🚀 Momento excelente! Score {score_c:.2f}, agentes alinhados. Tendência: {tend} 🎯",
        ],
        "aguardando": [
            "🕐 Aguardando. Agentes lendo alta densidade de quebras rápidas. Paciência.",
            f"🕐 Tendência {tend}. Aguardando reversão para próxima entrada. WR: {wr:.0f}%",
        ],
        "geral": [
            f"📊 Monitoramento ativo. Agente Risco e IA alinhados. WR: {wr:.0f}%",
            f"📊 Sistema operando. Tendência: {tend} | Score: {score_c:.2f} | P&L: R${pl:+.0f}",
        ],
    }

    pool = templates.get(contexto, templates["geral"])
    texto = random.choice(pool)

    return texto, gif_url

async def gerar_insight_periodico(dados: dict) -> tuple[str, str]:
    """Insight mais analítico enviado a cada período (30min default).
    Inclui análise de horário, tendência e dica estratégica.
    """
    pl        = dados.get("pl_sessao", 0)
    wr        = dados.get("win_rate", 0)
    entradas  = dados.get("entradas", 0)
    tend      = dados.get("tendencia", "NEUTRA")
    mb        = dados.get("melhores_blocos", [])
    pb        = dados.get("piores_blocos", [])
    insight   = dados.get("insight_bloco", "")
    blame     = dados.get("blame_report", [])

    blame_str = ""
    if blame:
        culpados = [b for b in blame if b.get("loss_rate", 0) >= 0.60 and b["total"] >= 5]
        if culpados:
            blame_str = "Agentes com mais erros: " + ", ".join(
                f"{b['agente']} ({b['loss_rate']:.0%} erros)" for b in culpados[:3]
            )

    mb_str = ", ".join(f"{b['bloco']}({b['wr']:.0%})" for b in mb[:3]) if mb else "sem dados"
    pb_str = ", ".join(f"{b['bloco']}({b['wr']:.0%})" for b in pb[:2]) if pb else "sem dados"

    veredito_tend   = dados.get("veredito_tendencia", tend)
    motivo_tend     = dados.get("motivo_tendencia", "")
    veredito_bloco  = dados.get("veredito_bloco", "")
    motivo_bloco    = dados.get("motivo_bloco", "")
    culpados        = dados.get("agentes_problematicos", [])

    prompt = f"""Você é o Analista Chefe do CrashIQ. Gere um relatório motivacional e analítico de 2-4 frases para o grupo do Telegram.
O relatório deve focar em insights práticos sobre o comportamento do robô e do mercado no momento atual e nos próximos minutos.
Utilize os dados abaixo de forma natural (não cite todos os números brutos, apenas os destaques):
  - P&L Acumulado: R${pl:+.0f} | Win Rate: {wr:.1f}% ({entradas} rodadas)
  - Tendência Curto Prazo: {tend} (Agente Tendência votou: {veredito_tend} - {motivo_tend})
  - Análise de Horários: Agente Bloco votou: {veredito_bloco} - {motivo_bloco}
  - Melhores Blocos de Horário: {mb_str} | Piores Blocos: {pb_str}
  {f'- Histórico de Performance dos Agentes: {blame_str}' if blame_str else ''}

Regras:
1. Tom de trader profissional, carismático e entusiasmado.
2. Destaque quais agentes estão indo muito bem e quais estão sob análise (culpados).
3. Faça uma estimativa para os próximos rounds com base na tendência e nos blocos de horários.
4. Retorne APENAS o texto da mensagem, sem aspas, focado nas condições atuais e projeções para os próximos minutos.
"""
    texto = await _gemini_texto(prompt, temperatura=0.7)
    if not texto:
        texto = f"📊 *CrashIQ Insights*:\nP&L da sessão: R${pl:+.0f} | WR: {wr:.1f}%\nTendência do mercado: *{tend}*.\nBloco de horário atual sob análise."
    return texto, "https://media.giphy.com/media/d3mlE7uhX8KFgEmY/giphy.mp4"
