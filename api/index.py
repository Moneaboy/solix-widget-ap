"""Balkonkraftwerk-Widget: Anker SOLIX Cloud -> kompaktes JSON.

Ein-Datei-Version fuer das Anlegen direkt im GitHub-Webeditor.

    GET /api?key=DEIN_KEY           normalisierte Werte fuer das Widget
    GET /api?key=DEIN_KEY&raw=1     Rohantwort der Anker-Cloud (Diagnose)
    GET /api?key=DEIN_KEY&fresh=1   Antwort-Cache umgehen

Vorzeichen:
    grid    > 0  Bezug aus dem Netz      < 0  Einspeisung ins Netz
    battery > 0  Speicher wird geladen   < 0  Speicher wird entladen
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

LOGGER = logging.getLogger("solix")
LOGGER.setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Konfiguration ueber Environment-Variablen
# ---------------------------------------------------------------------------

ANKER_EMAIL = os.environ.get("ANKER_EMAIL", "")
ANKER_PASSWORD = os.environ.get("ANKER_PASSWORD", "")
ANKER_COUNTRY = os.environ.get("ANKER_COUNTRY", "DE")
SITE_ID = os.environ.get("ANKER_SITE_ID", "")

# Nutzbare Speicherkapazitaet in Wh. E2700 Pro = 2690 Wh.
# Bei Erweiterungsakku BP2700 entsprechend erhoehen (z. B. 5380).
BATTERY_WH = int(os.environ.get("BATTERY_WH", "2690"))

# So lange wird eine Antwort wiederverwendet, bevor die Anker-Cloud
# erneut gefragt wird. Schuetzt vor Rate-Limits, wenn mehrere
# Widget-Instanzen gleichzeitig pollen.
CACHE_TTL = int(os.environ.get("CACHE_TTL", "45"))

# Ab diesem Alter der Cloud-Daten gilt der Wert als veraltet (Sekunden).
STALE_AFTER = int(os.environ.get("STALE_AFTER", "300"))

WIDGET_API_KEY = os.environ.get("WIDGET_API_KEY", "")

# Upstash / Vercel KV (optional, aber dringend empfohlen).
# Ohne Redis liegt der Login-Token nur in /tmp und geht bei jedem
# Cold Start verloren -> neuer Login -> Anker-App wird ausgeloggt.
REDIS_URL = os.environ.get("KV_REST_API_URL") or os.environ.get(
    "UPSTASH_REDIS_REST_URL", ""
)
REDIS_TOKEN = os.environ.get("KV_REST_API_TOKEN") or os.environ.get(
    "UPSTASH_REDIS_REST_TOKEN", ""
)

TMP = Path("/tmp/solix")
TMP.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Minimaler Redis-Client ueber die REST-Schnittstelle (keine Dependency noetig)
# ---------------------------------------------------------------------------


def _redis(command: list[str]) -> object | None:
    if not (REDIS_URL and REDIS_TOKEN):
        return None
    url = REDIS_URL.rstrip("/") + "/" + "/".join(
        urllib.parse.quote(str(part), safe="") for part in command
    )
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {REDIS_TOKEN}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode()).get("result")
    except (urllib.error.URLError, ValueError, TimeoutError) as err:
        LOGGER.warning("Redis nicht erreichbar: %s", err)
        return None


def kv_get(key: str) -> str | None:
    value = _redis(["get", key])
    return value if isinstance(value, str) else None


def kv_set(key: str, value: str, ttl: int | None = None) -> None:
    command = ["set", key, value]
    if ttl:
        command += ["EX", str(ttl)]
    _redis(command)


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------


def _num(value: object, default: float = 0.0) -> float:
    """Anker liefert Zahlen mal als String, mal als Zahl, mal als None."""
    if value is None or value == "":
        return default
    try:
        return float(str(value).strip().rstrip("Ww"))
    except (TypeError, ValueError):
        return default


def _parse_time(value: str) -> float | None:
    """'2025-07-18 13:22:23' -> Unix-Timestamp. Anker sendet Ortszeit.

    Manche Systeme liefern Platzhalter statt einer echten Zeit
    ('01-01-0001...' oder '1970-01-01...'). Die gelten als 'keine Angabe',
    sonst waeren die Daten rechnerisch Jahrzehnte alt.
    """
    if not value or value.startswith(("01-01-0001", "1970-01-01", "0001-01-01")):
        return None
    try:
        stamp = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").astimezone().timestamp()
    except ValueError:
        return None
    # Alles vor 2020 ist ebenfalls ein Platzhalter
    return stamp if stamp > 1577836800 else None


# ---------------------------------------------------------------------------
# Auth-Cache: Redis <-> lokale Datei der Library
# ---------------------------------------------------------------------------

AUTH_KEY = f"solix:auth:{ANKER_EMAIL}"


def _restore_auth_file(auth_file: str) -> None:
    cached = kv_get(AUTH_KEY)
    if not cached:
        return
    path = Path(auth_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cached, encoding="utf-8")
    LOGGER.info("Login-Token aus Redis wiederhergestellt")


def _persist_auth_file(auth_file: str) -> None:
    path = Path(auth_file)
    if not path.is_file():
        return
    content = path.read_text(encoding="utf-8")
    if content and content != kv_get(AUTH_KEY):
        # 25 Tage: kuerzer als die uebliche Token-Laufzeit von 30 Tagen
        kv_set(AUTH_KEY, content, ttl=25 * 24 * 3600)
        LOGGER.info("Login-Token in Redis gesichert")


# ---------------------------------------------------------------------------
# Normalisierung
# ---------------------------------------------------------------------------


def normalize(scene: dict) -> dict:
    sb = scene.get("solarbank_info") or {}
    grid = scene.get("grid_info") or {}
    banks = sb.get("solarbank_list") or []

    pv = _num(sb.get("total_photovoltaic_power"))
    strings = [_num(sb.get(f"solar_power_{i}")) for i in range(1, 5)]
    micro = _num(sb.get("micro_inverter_power"))
    if micro:
        pv += micro

    home = _num(scene.get("home_load_power"))
    grid_import = _num(grid.get("grid_to_home_power"))
    grid_export = _num(grid.get("photovoltaic_to_grid_power"))

    # SoC: site-weit als Anteil 0..1, pro Geraet als Prozent
    soc = _num(sb.get("total_battery_power")) * 100
    if not soc and banks:
        soc = _num(banks[0].get("battery_power"))

    charge = _num(sb.get("total_charging_power"))
    discharge = _num(sb.get("battery_discharge_power"))
    if not (charge or discharge) and banks:
        charge = sum(_num(b.get("bat_charge_power")) for b in banks)
        discharge = sum(_num(b.get("bat_discharge_power")) for b in banks)
    battery = charge - discharge

    # Restlaufzeit bzw. Zeit bis voll, in Minuten
    runtime = None
    if discharge > 5:
        runtime = int((soc / 100) * BATTERY_WH / discharge * 60)
    elif charge > 5:
        runtime = int((1 - soc / 100) * BATTERY_WH / charge * 60)

    # Autarkie: welcher Anteil des Verbrauchs kam nicht aus dem Netz
    autarky = None
    if home > 0:
        autarky = max(0, min(100, round((home - grid_import) / home * 100)))

    stats = {s.get("type"): s for s in (scene.get("statistics") or [])}
    updated = _parse_time(sb.get("updated_time") or "")
    now = time.time()
    age = int(now - updated) if updated else None

    # Cloud-Verbindungsstatus der Geraete. Liefert die Solarbank keinen
    # brauchbaren Zeitstempel, ist das die einzige Aktualitaetsaussage.
    grid_devices = grid.get("grid_list") or []
    online = bool(banks) and all(
        str(d.get("status", "1")) == "1" for d in banks + grid_devices
    )
    errors = [int(_num(b.get("err_code"))) for b in banks]

    # Namen der PV-Strings, wie in der Anker-App vergeben
    names = (banks[0].get("pv_name") or {}) if banks else {}
    pv_names = [
        names.get(f"pv{i}_name") or f"PV{i}" for i in range(1, 5)
    ]

    return {
        "ok": True,
        "ts": int(now),
        "age": age,
        "online": online,
        "stale": (not online) or (age is not None and age > STALE_AFTER),
        # Die fuenf Kernwerte
        "pv": round(pv),
        "home": round(home),
        "grid": round(grid_import - grid_export),
        "soc": round(soc),
        "battery": round(battery),
        # Aufgeschluesselt, falls das Widget Pfeilrichtungen braucht
        "grid_import": round(grid_import),
        "grid_export": round(grid_export),
        "bat_charge": round(charge),
        "bat_discharge": round(discharge),
        # Ergaenzungen
        "pv_strings": [round(s) for s in strings],
        "pv_names": pv_names,
        "sb_output": round(_num(sb.get("total_output_power"))),
        "grid_to_battery": round(_num(sb.get("grid_to_battery_power"))),
        "heating": round(_num(sb.get("pei_heating_power"))),
        "feed_limit": round(_num(sb.get("micro_inverter_power_limit"))),
        "err": max(errors) if errors else 0,
        "autarky": autarky,
        "runtime_min": runtime,
        "total_kwh": _num((stats.get("1") or {}).get("total")),
        "saved_eur": _num((stats.get("3") or {}).get("total")),
        "co2_kg": _num((stats.get("2") or {}).get("total")),
        "mode": scene.get("user_scene_mode") or scene.get("scene_mode"),
    }


# ---------------------------------------------------------------------------
# Abruf
# ---------------------------------------------------------------------------


async def fetch_scene() -> dict:
    """Roh-scene_info von der Anker-Cloud holen."""
    from aiohttp import ClientSession
    from anker_solix_api import api  # type: ignore[import-not-found]

    async with ClientSession() as websession:
        client = api.AnkerSolixApi(
            ANKER_EMAIL, ANKER_PASSWORD, ANKER_COUNTRY, websession, LOGGER
        )
        auth_file = client.apisession._authFile  # noqa: SLF001
        _restore_auth_file(auth_file)

        await client.apisession.async_authenticate()

        site_id = SITE_ID or kv_get("solix:site_id") or ""
        if not site_id:
            sites = await client.get_site_list()
            entries = sites.get("site_list") or []
            if not entries:
                raise RuntimeError(
                    "Kein System im Anker-Konto gefunden. Ist das System "
                    "in der App angelegt und mit diesem Konto geteilt?"
                )
            site_id = entries[0]["site_id"]
            kv_set("solix:site_id", site_id, ttl=30 * 24 * 3600)

        scene = await client.get_scene_info(site_id)
        _persist_auth_file(auth_file)
        return scene


async def get_status(force: bool = False) -> dict:
    """Normalisierte Werte, mit kurzem Antwort-Cache."""
    cache_file = TMP / "status.json"

    if not force and cache_file.is_file():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if time.time() - cached.get("ts", 0) < CACHE_TTL:
                cached["cached"] = True
                return cached
        except (ValueError, OSError):
            pass

    result = normalize(await fetch_scene())
    try:
        cache_file.write_text(json.dumps(result), encoding="utf-8")
    except OSError:
        pass
    return result


# -------------------------------------------------------------------------
# HTTP-Handler
# -------------------------------------------------------------------------


class handler(BaseHTTPRequestHandler):  # noqa: N801  (Vercel erwartet diesen Namen)
    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        params = parse_qs(urlparse(self.path).query)
        key = (params.get("key") or [""])[0]
        header_key = (self.headers.get("Authorization") or "").removeprefix("Bearer ")

        if WIDGET_API_KEY and key != WIDGET_API_KEY and header_key != WIDGET_API_KEY:
            self._send(401, {"ok": False, "error": "unauthorized"})
            return

        raw = (params.get("raw") or ["0"])[0] == "1"
        fresh = (params.get("fresh") or ["0"])[0] == "1"

        try:
            if raw:
                self._send(200, {"ok": True, "scene": asyncio.run(fetch_scene())})
            else:
                self._send(200, asyncio.run(get_status(force=fresh)))
        except Exception as err:  # noqa: BLE001
            self._send(
                502,
                {
                    "ok": False,
                    "error": f"{type(err).__name__}: {err}",
                    "trace": traceback.format_exc(limit=4),
                },
            )
