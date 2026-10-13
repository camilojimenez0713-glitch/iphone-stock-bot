#!/usr/bin/env python3
"""
Prueba de extremo a extremo: hace una ronda real contra Apple y manda al chat
privado una alerta de EJEMPLO, marcada como prueba. No toca el estado del bot.

Uso:
    python prueba_alerta.py
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv

import bot

log = logging.getLogger("stockbot.prueba")


def sample_alert(cfg: bot.Config, client: bot.AppleClient) -> str:
    part, name = next(iter(cfg.parts.items()))
    row = {"store": "Tienda de ejemplo", "city": "Manchester", "state": "NH", "quote": "Available Today"}
    return bot.format_alert(name, [row], client.buy_link(part), True)


def main() -> None:
    load_dotenv(bot.BASE_DIR / ".env")
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    # estado aparte: la prueba no debe pisar el del bot real
    os.environ["STATE_FILE"] = str(Path(tempfile.mkdtemp()) / "state.json")
    origin = "GitHub" if os.getenv("GITHUB_ACTIONS") else "tu computadora"

    stock_bot = bot.Bot(bot.Config.load(bot.BASE_DIR / "config.yaml"))
    try:
        stock_bot.check()
    except Exception as e:  # noqa: BLE001
        stock_bot.tg.private(f"🧪 <b>PRUEBA desde {origin}</b>: la ronda falló.\n{bot.esc(bot.short(e))}")
        raise SystemExit(f"La ronda de prueba falló: {e}") from e

    total, available = len(stock_bot.avail), sum(stock_bot.avail.values())
    stock_bot.tg.private("\n".join([
        f"🧪 <b>PRUEBA desde {origin}</b> — no es stock real",
        "",
        f"Ronda real recién hecha: {total} combinaciones tienda/modelo, {available} disponibles.",
        "",
        "Así se verá una alerta de stock:",
        "",
        sample_alert(stock_bot.cfg, stock_bot.apple),
    ]))


if __name__ == "__main__":
    main()
