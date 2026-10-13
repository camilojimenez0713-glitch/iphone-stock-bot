#!/usr/bin/env python3
"""
Bot de stock de iPhones (Apple Store EE.UU., recogida en tienda).

- Consulta /shop/retail/pickup-message de Apple tienda por tienda (≈9 KB por consulta)
  o por código postal (≈70-100 KB por consulta).
- Sale por proxies con rotación, reintentos y enfriamiento de IPs bloqueadas.
- Avisa por Telegram (chat privado + canal opcional) cuando un modelo pasa a disponible.
- Comandos en Telegram: /status /pause /resume /check
- "Cacería agresiva": cuando aparece stock, revisa más seguido durante unos minutos.
- Lleva la cuenta del tráfico de proxy por día y frena solo si se pasa del límite.

Uso:
    python bot.py                 # modo continuo
    python bot.py --once          # una sola ronda (cron / GitHub Actions)
    python bot.py --test-proxies  # prueba los proxies contra Apple
    python bot.py --test-notify   # mensaje de prueba a Telegram
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("stockbot")

API_PATH = "/shop/retail/pickup-message"
MIN_INTERVAL = 45  # piso de seguridad para el modo normal

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:143.0) Gecko/20100101 Firefox/143.0",
]


# --------------------------------------------------------------------------- #
# Configuración
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    country_path: str
    stores: list[str]
    locations: list[str]
    states: list[str]
    parts: dict[str, str]
    interval: int
    jitter: int
    aggressive_minutes: int
    aggressive_interval: int
    max_mb_per_day: float
    notify_on_unavailable: bool
    timeout: int
    proxies_file: Path
    proxy_cooldown: int
    state_file: Path

    @classmethod
    def load(cls, path: Path) -> "Config":
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        parts = raw.get("parts") or {}
        if isinstance(parts, list):
            parts = {p: p for p in parts}
        if not parts:
            sys.exit("config.yaml: añade al menos un part number en 'parts'.")

        stores = [str(s).strip().upper() for s in (raw.get("stores") or []) if str(s).strip()]
        locations = raw.get("locations") or raw.get("location") or []
        if isinstance(locations, (str, int)):
            locations = [locations]
        locations = [str(z).strip() for z in locations if str(z).strip()]
        if not stores and not locations:
            sys.exit("config.yaml: pon 'stores' (recomendado) o 'locations' (códigos postales).")

        interval = int(raw.get("interval_seconds", 120))
        if interval < MIN_INTERVAL:
            log.warning("interval_seconds=%s es muy bajo; se usa %s.", interval, MIN_INTERVAL)
            interval = MIN_INTERVAL

        agg = raw.get("aggressive") or {}
        country = (raw.get("country_path") or "").strip().rstrip("/")
        if country and not country.startswith("/"):
            country = "/" + country

        return cls(
            country_path=country,
            stores=stores,
            locations=locations,
            states=[str(s).upper() for s in (raw.get("states") or [])],
            parts={str(k).strip(): str(v) for k, v in parts.items()},
            interval=interval,
            jitter=int(raw.get("jitter_seconds", 30)),
            aggressive_minutes=int(agg.get("window_minutes", 10)),
            aggressive_interval=max(15, int(agg.get("interval_seconds", 20))),
            max_mb_per_day=float(raw.get("max_mb_per_day", 30)),
            notify_on_unavailable=bool(raw.get("notify_on_unavailable", False)),
            timeout=int(raw.get("timeout_seconds", 20)),
            proxies_file=BASE_DIR / raw.get("proxies_file", "proxies.txt"),
            proxy_cooldown=int(raw.get("proxy_cooldown_seconds", 300)),
            state_file=BASE_DIR / (os.getenv("STATE_FILE") or raw.get("state_file", "state.json")),
        )


# --------------------------------------------------------------------------- #
# Proxies
# --------------------------------------------------------------------------- #
def normalize_proxy(line: str) -> str | None:
    """
    Formatos aceptados:
      http://user:pass@host:port   socks5://user:pass@host:port
      user:pass@host:port          host:port:user:pass          host:port
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "://" in line:
        return line
    if "@" in line:
        return "http://" + line
    bits = line.split(":")
    if len(bits) == 2:
        return f"http://{bits[0]}:{bits[1]}"
    if len(bits) == 4:
        host, port, user, pwd = bits
        return f"http://{user}:{pwd}@{host}:{port}"
    log.warning("Formato de proxy no reconocido, se ignora: %s", mask(line))
    return None


def mask(proxy: str) -> str:
    """Oculta la contraseña del proxy en logs y mensajes."""
    if "@" not in proxy:
        return proxy
    creds, host = proxy.rsplit("@", 1)
    scheme = ""
    if "://" in creds:
        scheme, creds = creds.split("://", 1)
        scheme += "://"
    return f"{scheme}{creds.split(':', 1)[0]}:***@{host}"


@dataclass
class ProxyState:
    url: str
    fails: int = 0
    banned_until: float = 0.0


@dataclass
class ProxyPool:
    cooldown: int
    items: list[ProxyState] = field(default_factory=list)
    _idx: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def load(cls, path: Path, cooldown: int) -> "ProxyPool":
        pool = cls(cooldown=cooldown)
        env_list = os.getenv("PROXIES", "")
        lines = env_list.replace(",", "\n").splitlines() if env_list else []
        if path.exists():
            lines += path.read_text(encoding="utf-8").splitlines()
        seen = set()
        for line in lines:
            url = normalize_proxy(line)
            if url and url not in seen:
                seen.add(url)
                pool.items.append(ProxyState(url))
        random.shuffle(pool.items)
        return pool

    def __len__(self) -> int:
        return len(self.items)

    def next(self) -> ProxyState | None:
        if not self.items:
            return None
        with self._lock:
            now = time.time()
            for _ in range(len(self.items)):
                p = self.items[self._idx % len(self.items)]
                self._idx += 1
                if p.banned_until <= now:
                    return p
            return min(self.items, key=lambda x: x.banned_until)

    def success(self, p: ProxyState | None) -> None:
        if p:
            p.fails = 0

    def failure(self, p: ProxyState | None, hard: bool = False) -> None:
        if not p:
            return
        p.fails += 1
        # Con un solo endpoint rotativo (Webshare) no tiene sentido enfriarlo:
        # cada petición ya sale con otra IP.
        if len(self.items) == 1:
            return
        if hard or p.fails >= 3:
            p.banned_until = time.time() + self.cooldown * min(p.fails, 6)
            log.info("Proxy en enfriamiento %ss: %s", int(p.banned_until - time.time()), mask(p.url))

    def healthy(self) -> int:
        now = time.time()
        return sum(1 for p in self.items if p.banned_until <= now)


# --------------------------------------------------------------------------- #
# Apple
# --------------------------------------------------------------------------- #
class BlockedError(Exception):
    pass


class AppleClient:
    def __init__(self, cfg: Config, pool: ProxyPool, usage: "Usage"):
        self.cfg = cfg
        self.pool = pool
        self.usage = usage

    @property
    def url(self) -> str:
        return f"https://www.apple.com{self.cfg.country_path}{API_PATH}"

    def buy_link(self, part: str) -> str:
        return f"https://www.apple.com{self.cfg.country_path}/shop/product/{part}"

    def _headers(self) -> dict:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": f"https://www.apple.com{self.cfg.country_path}/shop/buy-iphone",
        }

    def _params(self, parts: list[str], store: str | None, location: str | None) -> dict:
        params = {"pl": "true"}
        for i, part in enumerate(parts):
            params[f"parts.{i}"] = part
        if store:
            params["store"] = store
        if location:
            params["location"] = location
        return params

    def fetch(self, parts: list[str], store: str | None = None, location: str | None = None) -> dict:
        attempts = max(3, min(len(self.pool), 6))
        last: Exception | None = None
        target = store or location
        for attempt in range(1, attempts + 1):
            proxy = self.pool.next()
            proxies = {"http": proxy.url, "https": proxy.url} if proxy else None
            via = mask(proxy.url) if proxy else "sin proxy"
            try:
                r = requests.get(self.url, params=self._params(parts, store, location),
                                 headers=self._headers(), proxies=proxies, timeout=self.cfg.timeout)
                # bytes en el cable: el cuerpo comprimido si viene comprimido, + cabeceras/TLS
                wire = int(r.headers.get("Content-Length") or len(r.content))
                self.usage.add(wire + 1500)
                if r.status_code in (403, 429, 503):
                    self.pool.failure(proxy, hard=True)
                    raise BlockedError(f"HTTP {r.status_code}")
                if r.status_code >= 500 or r.status_code == 404:
                    raise ValueError(f"HTTP {r.status_code} (¿Apple cambió el endpoint?)")
                r.raise_for_status()
                data = r.json()
                if "body" not in data:
                    raise ValueError("respuesta sin 'body'")
                self.pool.success(proxy)
                return data
            except BlockedError as e:
                last = e
                log.warning("[%s] intento %d/%d bloqueado (%s) vía %s", target, attempt, attempts, e, via)
            except (requests.RequestException, ValueError) as e:
                last = e
                self.pool.failure(proxy)
                log.warning("[%s] intento %d/%d falló vía %s: %s", target, attempt, attempts, via, short(e))
            time.sleep(min(2 ** attempt, 15) + random.random())
        raise RuntimeError(f"[{target}] todos los intentos fallaron: {short(last)}")


def short(e: object) -> str:
    s = str(e) if e else "error desconocido"
    return s if len(s) < 180 else s[:177] + "..."


def strip_tags(text: str) -> str:
    out, inside = [], False
    for ch in text or "":
        if ch == "<":
            inside = True
        elif ch == ">":
            inside = False
        elif not inside:
            out.append(ch)
    return "".join(out).replace(" ", " ").strip()


def parse_stores(data: dict, parts: list[str]) -> list[dict]:
    body = data.get("body") or {}
    stores = body.get("stores")
    if stores is None:  # formato alternativo que Apple ha usado antes
        stores = ((body.get("content") or {}).get("pickupMessage") or {}).get("stores") or []
    rows = []
    for s in stores:
        avail = s.get("partsAvailability") or {}
        for part in parts:
            info = avail.get(part)
            if not info:
                continue
            title = ((info.get("messageTypes") or {}).get("regular") or {}).get("storePickupProductTitle", "")
            rows.append({
                "part": part,
                "store_id": s.get("storeNumber") or s.get("storeName"),
                "store": s.get("storeName") or "",
                "city": s.get("city") or "",
                "state": (s.get("state") or "").upper(),
                "distance": s.get("storeDistanceWithUnit") or "",
                "available": (info.get("pickupDisplay") or "").lower() == "available",
                "quote": strip_tags(info.get("pickupSearchQuote") or info.get("storePickupQuote") or ""),
                "title": title,
            })
    return rows


# --------------------------------------------------------------------------- #
# Tráfico (para planes de proxy por GB)
# --------------------------------------------------------------------------- #
class Usage:
    def __init__(self, data: dict | None = None):
        data = data or {}
        self.date = data.get("date") or datetime.now().strftime("%Y-%m-%d")
        self.bytes = int(data.get("bytes", 0))
        self.total = int(data.get("total", 0))
        self._lock = threading.Lock()

    def _roll(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self.date:
            self.date, self.bytes = today, 0

    def add(self, n: int) -> None:
        with self._lock:
            self._roll()
            self.bytes += n
            self.total += n

    def mb_today(self) -> float:
        with self._lock:
            self._roll()
            return self.bytes / 1_000_000

    def dump(self) -> dict:
        return {"date": self.date, "bytes": self.bytes, "total": self.total}


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
class Telegram:
    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.user_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.channel = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()
        self.offset: int | None = None
        if not (self.token and (self.user_chat or self.channel)):
            log.warning("Telegram no configurado: las alertas solo se verán en consola.")

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _post(self, chat: str, text: str) -> None:
        try:
            r = requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                              json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                                    "disable_web_page_preview": True}, timeout=15)
            if not r.ok:
                log.error("Telegram %s respondió %s: %s", chat, r.status_code, r.text[:200])
        except requests.RequestException as e:
            log.error("No se pudo enviar a Telegram: %s", short(e))

    def alert(self, text: str) -> None:
        """Alertas de stock: chat privado + canal."""
        print("\n" + "=" * 60 + "\n" + text + "\n" + "=" * 60, flush=True)
        if not self.enabled:
            return
        for chat in {c for c in (self.user_chat, self.channel) if c}:
            self._post(chat, text)

    def private(self, text: str) -> None:
        """Mensajes de control: solo tu chat privado."""
        log.info("Telegram (privado): %s", text.replace("\n", " | ")[:200])
        if self.enabled and self.user_chat:
            self._post(self.user_chat, text)

    def poll_commands(self, timeout: int = 25) -> list[str]:
        if not (self.enabled and self.user_chat):
            time.sleep(timeout)
            return []
        params = {"timeout": timeout, "allowed_updates": json.dumps(["message"])}
        if self.offset is not None:
            params["offset"] = self.offset
        try:
            r = requests.get(f"https://api.telegram.org/bot{self.token}/getUpdates",
                             params=params, timeout=timeout + 10)
            r.raise_for_status()
            cmds = []
            for upd in r.json().get("result", []):
                self.offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                if str((msg.get("chat") or {}).get("id")) != self.user_chat:
                    continue  # solo aceptamos comandos de tu chat privado
                text = (msg.get("text") or "").strip().lower()
                if text.startswith("/"):
                    cmds.append(text.split()[0].split("@")[0])
            return cmds
        except requests.RequestException as e:
            log.debug("getUpdates falló: %s", short(e))
            time.sleep(5)
            return []


def esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_alert(name: str, rows: list[dict], link: str, available: bool) -> str:
    head = f"🚨 <b>{esc(name)} DISPONIBLE</b> 🚨" if available else f"🔴 <b>{esc(name)}</b> se agotó de nuevo"
    lines = [head, f"🕐 {datetime.now().strftime('%H:%M:%S')}", ""]
    for r in rows:
        place = ", ".join(x for x in (r["city"], r["state"]) if x)
        line = f"• <b>{esc(r['store'])}</b>" + (f" ({esc(place)})" if place else "")
        if available and r["quote"]:
            line += f" — {esc(r['quote'])}"
        lines.append(line)
    lines += ["", f'<a href="{link}">🛒 Abrir en Apple Store</a>']
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #
class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        saved = self._load_state()
        self.avail: dict[str, bool] = saved.get("availability", {})
        self.usage = Usage(saved.get("usage"))
        self.pool = ProxyPool.load(cfg.proxies_file, cfg.proxy_cooldown)
        self.apple = AppleClient(cfg, self.pool, self.usage)
        self.tg = Telegram()
        self.paused = threading.Event()
        self.wake = threading.Event()
        self.aggressive_until: datetime | None = None
        self.last_check: datetime | None = None
        self.last_error: str | None = None
        self.budget_warned_on: str | None = None
        self.errors_in_row = 0

    # --- estado en disco --------------------------------------------------- #
    def _load_state(self) -> dict:
        try:
            return json.loads(self.cfg.state_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_state(self) -> None:
        tmp = self.cfg.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"availability": self.avail, "usage": self.usage.dump()},
                                  indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.cfg.state_file)

    # --- una ronda ---------------------------------------------------------- #
    def check(self) -> None:
        parts = list(self.cfg.parts)
        rows: list[dict] = []
        failures = 0
        targets = [("store", s) for s in self.cfg.stores] + [("location", z) for z in self.cfg.locations]
        for kind, value in targets:
            try:
                data = self.apple.fetch(parts, **{kind: value})
                rows += parse_stores(data, parts)
            except RuntimeError as e:
                failures += 1
                log.error(str(e))
        if failures == len(targets):
            raise RuntimeError("ninguna tienda respondió en esta ronda")

        # una fila por (modelo, tienda), aunque salga en varios códigos postales
        uniq = {}
        for r in rows:
            if self.cfg.states and r["state"] and r["state"] not in self.cfg.states:
                continue
            uniq.setdefault((r["part"], r["store_id"]), r)
        rows = list(uniq.values())
        self.last_check = datetime.now()

        new_by_part: dict[str, list[dict]] = {}
        gone_by_part: dict[str, list[dict]] = {}
        for r in rows:
            key = f"{r['part']}|{r['store_id']}"
            was, now = self.avail.get(key, False), r["available"]
            if now and not was:
                new_by_part.setdefault(r["part"], []).append(r)
            elif was and not now:
                gone_by_part.setdefault(r["part"], []).append(r)
            self.avail[key] = now

        for part, rs in new_by_part.items():
            self.tg.alert(format_alert(self.cfg.parts[part], rs, self.apple.buy_link(part), True))
        if self.cfg.notify_on_unavailable:
            for part, rs in gone_by_part.items():
                self.tg.alert(format_alert(self.cfg.parts[part], rs, self.apple.buy_link(part), False))

        if new_by_part:
            self.aggressive_until = datetime.now() + timedelta(minutes=self.cfg.aggressive_minutes)
            log.info("🔥 Cacería agresiva hasta %s", self.aggressive_until.strftime("%H:%M:%S"))

        n_av = sum(1 for r in rows if r["available"])
        log.info("Ronda OK: %d tienda/modelo, %d disponibles. Tráfico hoy: %.1f MB",
                 len(rows), n_av, self.usage.mb_today())
        self._save_state()

    # --- espera entre rondas ------------------------------------------------ #
    def next_delay(self) -> int:
        now = datetime.now()
        if self.aggressive_until and now < self.aggressive_until:
            return self.cfg.aggressive_interval + random.randint(0, 5)
        if self.aggressive_until:
            log.info("Fin de la cacería agresiva.")
            self.aggressive_until = None

        delay = self.cfg.interval + random.randint(0, max(self.cfg.jitter, 0))
        mb = self.usage.mb_today()
        if self.cfg.max_mb_per_day and mb > self.cfg.max_mb_per_day:
            delay *= 3
            if self.budget_warned_on != self.usage.date:
                self.budget_warned_on = self.usage.date
                self.tg.private(f"⚠️ Hoy van {mb:.1f} MB de proxy (límite {self.cfg.max_mb_per_day:.0f} MB). "
                                "Bajo la frecuencia a un tercio hasta mañana.")
        if self.errors_in_row:
            delay = min(delay * (1 + self.errors_in_row), 1800)
        return delay

    # --- comandos de Telegram ---------------------------------------------- #
    def status_text(self) -> str:
        if self.paused.is_set():
            estado = "⏸ PAUSADO"
        elif self.aggressive_until and datetime.now() < self.aggressive_until:
            estado = f"🔥 CACERÍA hasta {self.aggressive_until.strftime('%H:%M')}"
        else:
            estado = "▶️ ACTIVO"
        disponibles = sorted({self.cfg.parts.get(k.split('|')[0], k) for k, v in self.avail.items() if v})
        donde = ", ".join(self.cfg.stores + self.cfg.locations)
        lines = [
            "ℹ️ <b>Estado del bot</b>",
            f"• Estado: {estado}",
            f"• Último chequeo: {self.last_check.strftime('%H:%M:%S') if self.last_check else 'N/A'}",
            f"• Revisando: {esc(donde)}",
            f"• Modelos: {len(self.cfg.parts)}",
            f"• Disponibles ahora: {esc(', '.join(disponibles)) if disponibles else 'ninguno'}",
            f"• Tráfico proxy hoy: {self.usage.mb_today():.1f} MB (total {self.usage.total / 1e6:.0f} MB)",
            f"• Proxies sanos: {self.pool.healthy()}/{len(self.pool)}",
        ]
        if self.last_error:
            lines.append(f"• Último error: {esc(self.last_error)}")
        return "\n".join(lines)

    def command_loop(self) -> None:
        while True:
            for cmd in self.tg.poll_commands():
                if cmd == "/pause":
                    self.paused.set()
                    self.tg.private("⏸ Bot pausado. Usa /resume para seguir.")
                elif cmd in ("/resume", "/start"):
                    self.paused.clear()
                    self.wake.set()
                    self.tg.private("▶️ Bot reanudado.")
                elif cmd == "/status":
                    self.tg.private(self.status_text())
                elif cmd == "/check":
                    self.wake.set()
                    self.tg.private("🔎 Revisando ahora…")
                else:
                    self.tg.private("Comandos: /status /pause /resume /check")

    # --- bucle principal ---------------------------------------------------- #
    def run(self, once: bool = False) -> None:
        log.info("Proxies cargados: %d%s", len(self.pool), "" if len(self.pool) else " (¡saldrá con tu IP!)")
        log.info("Tiendas: %s | ZIPs: %s | Modelos: %s",
                 self.cfg.stores or "-", self.cfg.locations or "-", list(self.cfg.parts.values()))
        if once:
            self.check()
            return

        threading.Thread(target=self.command_loop, daemon=True).start()
        self.tg.private("✅ Bot de stock iniciado.\n" + self.status_text())
        while True:
            if self.paused.is_set():
                self.wake.wait(30)
                self.wake.clear()
                continue
            try:
                self.check()
                self.errors_in_row = 0
                self.last_error = None
            except Exception as e:  # noqa: BLE001
                self.errors_in_row += 1
                self.last_error = short(e)
                log.error("Ronda fallida (%d seguidas): %s", self.errors_in_row, e)
                if self.errors_in_row == 5:
                    self.tg.private("⚠️ Llevo 5 rondas fallando. Revisa el proxy (¿se acabó el GB?) "
                                    f"o si Apple cambió algo.\nÚltimo error: {esc(self.last_error)}")
            delay = self.next_delay()
            log.info("Próxima ronda en %ss", delay)
            self.wake.wait(delay)
            self.wake.clear()


# --------------------------------------------------------------------------- #
# Utilidades de línea de comandos
# --------------------------------------------------------------------------- #
def test_proxies(cfg: Config) -> None:
    pool = ProxyPool.load(cfg.proxies_file, cfg.proxy_cooldown)
    if not len(pool):
        print("No hay proxies en", cfg.proxies_file)
        return
    part = next(iter(cfg.parts))
    target = {"store": cfg.stores[0]} if cfg.stores else {"location": cfg.locations[0]}
    print(f"Probando {len(pool)} proxy(s) contra Apple…\n")
    for p in pool.items:
        prox = {"http": p.url, "https": p.url}
        for _ in range(2 if len(pool) == 1 else 1):  # endpoint rotativo: 2 pruebas = 2 IPs
            t0 = time.time()
            try:
                ip = requests.get("https://api.ipify.org?format=json", proxies=prox, timeout=cfg.timeout).json()["ip"]
                params = {"pl": "true", "parts.0": part, **target}
                r = requests.get(f"https://www.apple.com{cfg.country_path}{API_PATH}", params=params,
                                 headers={"User-Agent": random.choice(USER_AGENTS)}, proxies=prox, timeout=cfg.timeout)
                ok = r.ok and "body" in r.text[:200]
                print(f"  {'✔' if ok else '✘'} {mask(p.url):50} IP {ip:16} {int((time.time()-t0)*1000):5} ms  "
                      f"Apple HTTP {r.status_code}  {len(r.content)/1000:.1f} KB")
            except Exception as e:  # noqa: BLE001
                print(f"  ✘ {mask(p.url):50} {short(e)}")


def main() -> None:
    load_dotenv(BASE_DIR / ".env")
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    ap = argparse.ArgumentParser(description="Bot de stock de iPhones")
    ap.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--test-proxies", action="store_true")
    ap.add_argument("--test-notify", action="store_true")
    args = ap.parse_args()

    path = Path(args.config)
    if not path.exists():
        sys.exit(f"No existe {path}.")
    cfg = Config.load(path)

    if args.test_notify:
        Telegram().alert("✅ Prueba del bot de stock: las alertas llegan bien.")
        return
    if args.test_proxies:
        test_proxies(cfg)
        return
    try:
        Bot(cfg).run(once=args.once)
    except KeyboardInterrupt:
        log.info("Detenido.")


if __name__ == "__main__":
    main()
