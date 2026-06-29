# notificador.py — Entrega de sinais. Console agora; Telegram na Fase 2.
# REGRA: nunca bloqueia o loop principal (fire-and-forget via asyncio).

import asyncio
import logging
from datetime import datetime
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger("notificador")


class Notificador:
    def __init__(self, usar_telegram: bool = False):
        self._telegram = usar_telegram and bool(TELEGRAM_TOKEN) and bool(TELEGRAM_CHAT_ID)
        if self._telegram:
            try:
                import httpx
                self._http = httpx.AsyncClient(timeout=5.0)
            except ImportError:
                logger.warning("httpx não instalado — Telegram desativado")
                self._telegram = False

    async def enviar(self, mensagem: str, estado: str = ""):
        """Fire-and-forget: não bloqueia o loop de análise."""
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{ts}] {mensagem}\n{'─'*60}")

        if self._telegram and estado in ("ENTRAR", "ATENCAO", "ABORTAR"):  # noqa
            asyncio.create_task(self._telegram_send(mensagem))

    async def _telegram_send(self, texto: str):
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        try:
            await self._http.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": texto})
        except Exception as e:
            logger.error(f"Telegram falhou: {e}")
