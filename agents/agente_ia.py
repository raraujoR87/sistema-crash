import asyncio
import json
import logging
import httpx
import config
import banco
from .base import AgenteBase, Veredito

log = logging.getLogger("agente_ia")

_GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
               "gemini-2.5-flash:generateContent?key={key}")


async def _chamar_gemini(prompt: str) -> str | None:
    """Chamada async ao Gemini 2.5 Flash. Retorna o texto bruto ou None."""
    if not config.GEMINI_API_KEY:
        return None
    url = _GEMINI_URL.format(key=config.GEMINI_API_KEY)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048},
    }
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(url, json=payload, timeout=25.0)
        if r.status_code == 200:
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        log.warning(f"Gemini HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.warning(f"Gemini erro: {e}")
    return None


class AgenteIA(AgenteBase):
    nome = "ia_gemini"

    # Chama Gemini a cada N rodadas para reduzir custo de API
    COOLDOWN_ROUNDS = 5

    def __init__(self, alvo: float):
        self.alvo = alvo
        self._ultimo_resultado: Veredito | None = None
        self._task: asyncio.Task | None = None
        self.callback_update = None
        self._rounds_desde_chamada = 0

    def analisar(self, memoria) -> Veredito:
        """Retorna o último resultado disponível e dispara nova análise em background.
        Só chama Gemini a cada COOLDOWN_ROUNDS rodadas para economizar custos."""
        if not config.GEMINI_API_KEY:
            return Veredito(self.nome, 0.5, "AGUARDAR",
                            "Gemini: API key não configurada (dashboard → Configurações → IA)", {})

        rodadas = memoria.ultimas(20)
        if len(rodadas) < 5:
            return Veredito(self.nome, 0.5, "AGUARDAR", "Sem rodadas suficientes para a IA", {})

        # Cooldown: só dispara nova chamada Gemini a cada N rounds
        self._rounds_desde_chamada += 1
        if self._rounds_desde_chamada >= self.COOLDOWN_ROUNDS:
            if self._task is None or self._task.done():
                self._rounds_desde_chamada = 0
                try:
                    import asyncio
                    asyncio.get_running_loop()
                    self._task = asyncio.create_task(self._analisar_async(memoria))
                except RuntimeError:
                    pass  # Estamos em backtest síncrono, ignora a chamada ao Gemini

        # Retorna o último resultado enquanto a nova análise roda
        if self._ultimo_resultado:
            return self._ultimo_resultado

        return Veredito(self.nome, 0.5, "AGUARDAR", "Gemini: aguardando primeira análise...", {})

    async def _analisar_async(self, memoria):
        """Executa a chamada ao Gemini e atualiza o cache de resultado."""
        rodadas    = memoria.ultimas(20)
        bloco_atual = memoria.bloco_atual()
        cats  = [config.classificar_rodada(r.multiplicador, self.alvo) for r in rodadas]
        mults = [f"{r.multiplicador:.2f}x" for r in rodadas]
        temps = [str(r.temperatura) for r in rodadas]

        # Últimas lições aprendidas
        licoes = []
        try:
            with banco._conn() as c:
                rows = c.execute(
                    "SELECT acertou FROM feedback_ia ORDER BY id DESC LIMIT 5"
                ).fetchall()
                acertos = sum(1 for (a,) in rows if a)
                if rows:
                    licoes.append(f"Últimas {len(rows)} análises: {acertos} corretas")
        except Exception:
            pass

        licoes_str = " | ".join(licoes) if licoes else ""

        prompt = (
            f"Você é um analista preditivo de crash games. Alvo: {self.alvo}x.\n"
            f"Últimas {len(rodadas)} rodadas — Multiplicadores: {', '.join(mults)}\n"
            f"Categorias: {', '.join(cats)}\n"
            f"Temperatura (volume 1-5): {', '.join(temps)}\n"
            f"{f'Histórico recente: {licoes_str}' if licoes_str else ''}\n\n"
            f"Qual a probabilidade da PRÓXIMA rodada ser >= {self.alvo}x?\n"
            "Retorne APENAS JSON válido:\n"
            '{"score": 0.0~1.0, "estado": "ENTRAR"|"AGUARDAR"|"ATENCAO"|"ABORTAR", "motivo": "frase curta"}'
        )

        texto = await _chamar_gemini(prompt)
        if not texto:
            return

        try:
            # 1. Tenta limpar blocos markdown
            texto_limpo = texto.strip()
            if texto_limpo.startswith("```"):
                linhas = texto_limpo.splitlines()
                if len(linhas) >= 2:
                    if linhas[0].startswith("```"):
                        linhas = list(linhas[1:])
                    if linhas and linhas[-1].startswith("```"):
                        linhas = list(linhas[:-1])
                    texto_limpo = "\n".join(linhas).strip()

            # 2. Tenta parsear
            resultado = None
            try:
                resultado = json.loads(texto_limpo)
            except Exception:
                # 3. Tenta ast.literal_eval se a LLM usou aspas simples
                import ast
                try:
                    resultado = ast.literal_eval(texto_limpo)
                except Exception:
                    # 4. Busca substring entre { e }
                    start = texto_limpo.find("{")
                    end = texto_limpo.rfind("}")
                    if start != -1 and end != -1:
                        bloco = texto_limpo[start:end+1]
                        try:
                            resultado = json.loads(bloco)
                        except Exception:
                            try:
                                resultado = ast.literal_eval(bloco)
                            except Exception:
                                pass

            if not resultado or not isinstance(resultado, dict):
                raise ValueError("Formato JSON invalido")

            score  = max(0.0, min(1.0, float(resultado.get("score", 0.5))))
            estado = str(resultado.get("estado", "AGUARDAR")).upper()
            motivo = str(resultado.get("motivo", "Análise IA"))
            if estado not in ("ENTRAR", "AGUARDAR", "ATENCAO", "ABORTAR"):
                estado = "AGUARDAR"

            self._ultimo_resultado = Veredito(
                agente=self.nome,
                score=score,
                estado=estado,
                motivo=f"IA: {motivo}",
                dados={"raw": texto_limpo[:120]},
            )
            log.info(f"Gemini [{self.alvo}x]: {estado} score={score:.2f} — {motivo}")

            if self.callback_update:
                if asyncio.iscoroutinefunction(self.callback_update):
                    asyncio.create_task(self.callback_update())
                else:
                    self.callback_update()

            try:
                banco.gravar_feedback_ia(bloco_atual, prompt, texto_limpo, score)
            except Exception:
                pass

        except Exception as e:
            log.warning(f"Gemini parse erro: {e} | raw: {texto[:100]}")
