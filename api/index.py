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


# Betriebsmodi, wie die Anker-App sie benennt
MODE_NAMES = {
    1: "Smartmeter",
    2: "Smart Plugs",
    3: "Manueller Plan",
    4: "Notstrom",
    5: "Nutzungszeit",
    7: "KI-Modus",
    8: "Dynam. Tarif",
}

CHARGE_NAMES = {"0": "bereit", "1": "lädt", "2": "entlädt", "3": "Bypass"}


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


def normalize(scene: dict, today: dict | None = None) -> dict:
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
    total_kwh = _num((stats.get("1") or {}).get("total"))
    total_eur = _num((stats.get("3") or {}).get("total"))

    # Anker liefert fuer Tagesabfragen keinen Geldwert. Aus den
    # Lebenszeit-Summen laesst sich der unterstellte Arbeitspreis
    # ableiten und auf den heute selbst genutzten Strom anwenden.
    # Das ist eine Schaetzung, keine Anker-Zahl.
    self_used = ((today or {}).get("home_from_solar") or 0) + (
        (today or {}).get("home_from_bat") or 0
    )
    today_eur = (
        round(self_used * (total_eur / total_kwh), 2)
        if total_kwh > 0 and self_used > 0
        else None
    )
    updated = _parse_time(sb.get("updated_time") or "")
    now = time.time()
    age = int(now - updated) if updated else None

    # Cloud-Verbindungsstatus der Geraete. Liefert die Solarbank keinen
    # brauchbaren Zeitstempel, ist das die einzige Aktualitaetsaussage.
    grid_devices = grid.get("grid_list") or []
    mode = scene.get("user_scene_mode") or scene.get("scene_mode")
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
        # Gruppe B: Zustand und Einstellungen aus derselben Antwort
        "mode": mode,
        "mode_text": MODE_NAMES.get(mode, f"Modus {mode}" if mode else "-"),
        "retain_load": round(_num(scene.get("retain_load"))),
        "backup_full_min": (
            round(_num((sb.get("backup_info") or {}).get("full_time")))
            if (sb.get("backup_info") or {}).get("full_time")
            else None
        ),
        "backup_active": bool(
            (scene.get("feature_switch") or {}).get("backup_reserve_effective")
        ),
        "charge_state": CHARGE_NAMES.get(
            str(sb.get("charging_status", "")), "-"
        ),
        "sb_online": bool(banks) and all(str(d.get("status", "1")) == "1" for d in banks),
        "meter_online": bool(grid_devices)
        and all(str(d.get("status", "1")) == "1" for d in grid_devices),
        "expansions": sum(int(_num(b.get("sub_package_num"))) for b in banks),
        "third_party_pv": round(
            _num(scene.get("third_party_pv")) + _num(sb.get("other_input_power"))
        ),
        "autarky": autarky,
        "runtime_min": runtime,
        # Tages-, Prognose- und Verteilungswerte
        **{k: v for k, v in (today or {}).items() if k not in ("day", "at")},
        "today_eur": today_eur,
        "total_kwh": total_kwh,
        "saved_eur": _num((stats.get("3") or {}).get("total")),
        "co2_kg": _num((stats.get("2") or {}).get("total")),
    }


# ---------------------------------------------------------------------------
# Abruf
# ---------------------------------------------------------------------------


# Die Tages- und Prognosewerte aendern sich langsam und kosten eigene
# Cloud-Aufrufe. Sie werden daher deutlich laenger vorgehalten als die
# Momentanwerte.
TODAY_TTL = int(os.environ.get("TODAY_TTL", "300"))

# Die vier Auswertungen, die Anker getrennt liefert
ENERGY_TYPES = ("solar_production", "home_usage", "grid", "solarbank")


def _today_key() -> str:
    return "solix:energy:" + datetime.now().strftime("%Y-%m-%d")


def _today_cache() -> dict | None:
    """Zwischenspeicher: erst Redis (ueberlebt Kaltstarts), dann /tmp."""
    raw = kv_get(_today_key())
    if raw is None:
        path = TMP / "today.json"
        raw = path.read_text(encoding="utf-8") if path.is_file() else None
    if not raw:
        return None
    try:
        cached = json.loads(raw)
    except ValueError:
        return None
    if cached.get("day") != datetime.now().strftime("%Y-%m-%d"):
        return None
    if time.time() - cached.get("at", 0) > TODAY_TTL:
        return None
    return cached


def _store_today(data: dict) -> None:
    payload = json.dumps(data)
    kv_set(_today_key(), payload, ttl=2 * 24 * 3600)
    try:
        (TMP / "today.json").write_text(payload, encoding="utf-8")
    except OSError:
        pass


def _split_forecast(trend: list) -> tuple[float, float]:
    """Trendliste in Rest-heute und Morgen teilen.

    Anker liefert nur Uhrzeiten ohne Datum. Sobald die Stunde kleiner
    wird als die vorherige, hat der naechste Tag begonnen.
    """
    rest_today = 0.0
    tomorrow = 0.0
    last_hour = -1
    wrapped = False
    for slot in trend:
        label = str(slot.get("time") or "")
        try:
            hour = int(label.split(":")[0])
        except (ValueError, IndexError):
            continue
        if hour < last_hour:
            wrapped = True
        last_hour = hour
        watts = _num(slot.get("value"))
        if wrapped:
            tomorrow += watts
        else:
            rest_today += watts
    return rest_today / 1000, tomorrow / 1000


async def _fetch_energy(client, site_id: str, device_sn: str) -> dict:
    """Alle Tageswerte und die Prognose. Fehler kippen den Rest nicht."""
    cached = _today_cache()
    if cached:
        return cached

    import asyncio

    today = datetime.now()
    stamp = today.strftime("%Y-%m-%d")

    async def one(dev_type: str) -> dict:
        return await client.energy_analysis(
            siteId=site_id,
            deviceSn=device_sn,
            rangeType="day",
            startDay=today,
            endDay=today,
            devType=dev_type,
        )

    answers = await asyncio.gather(
        *(one(t) for t in ENERGY_TYPES), return_exceptions=True
    )

    parts: dict[str, dict] = {}
    for name, answer in zip(ENERGY_TYPES, answers):
        if isinstance(answer, BaseException):
            LOGGER.warning("Auswertung %s fehlgeschlagen: %s", name, answer)
            continue
        parts[name] = answer.get("data", answer) if isinstance(answer, dict) else {}

    solar = parts.get("solar_production") or {}
    home = parts.get("home_usage") or {}
    grid = parts.get("grid") or {}
    bank = parts.get("solarbank") or {}

    def val(source: dict, key: str) -> float | None:
        raw = source.get(key)
        return None if raw in (None, "") else _num(raw)

    result: dict = {"day": stamp, "at": time.time()}

    # Solarproduktion
    result["today_kwh"] = val(solar, "solar_total")
    result["today_to_home"] = val(solar, "solar_to_home_total")
    result["today_to_bat"] = val(solar, "solar_to_battery_total")
    result["today_to_grid"] = val(solar, "solar_to_grid_total")

    produced = result["today_kwh"] or 0
    to_home = result["today_to_home"]
    result["today_direct_share"] = (
        round(to_home / produced * 100) if produced > 0 and to_home is not None else None
    )

    # Prognose steckt in derselben Antwort
    has_trend = bool(solar.get("forecast_trend"))
    rest, tomorrow = _split_forecast(solar.get("forecast_trend") or [])
    result["forecast_today"] = val(solar, "forecast_total") if has_trend else None
    result["forecast_rest"] = round(rest, 2) if has_trend else None
    result["forecast_tomorrow"] = round(tomorrow, 2) if has_trend else None

    # Hausverbrauch
    result["home_today"] = val(home, "home_usage_total")
    result["home_from_grid"] = val(home, "grid_to_home_total")
    result["home_from_bat"] = val(home, "battery_to_home_total")
    result["home_from_solar"] = val(home, "solar_to_home_total")

    used = result["home_today"] or 0
    from_grid = result["home_from_grid"]
    result["autarky_today"] = (
        max(0, min(100, round((used - from_grid) / used * 100)))
        if used > 0 and from_grid is not None
        else None
    )

    # Netz
    result["grid_import_today"] = val(grid, "grid_imported_total")
    result["grid_export_today"] = val(grid, "solar_to_grid_total")
    result["grid_to_bat_today"] = val(grid, "grid_to_battery_total")

    # Speicher
    result["bat_charge_today"] = val(bank, "charge_total")
    result["bat_discharge_today"] = val(bank, "discharge_total")
    result["bat_to_home_today"] = val(bank, "battery_to_home_total")
    result["ac_socket_today"] = val(bank, "ac_out_put_total")

    if parts:
        _store_today(result)
    return result


async def fetch_all(with_today: bool = True) -> tuple[dict, dict]:
    """Momentanwerte und optional den Tagesertrag holen."""
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

        today: dict = {}
        if with_today:
            banks = (scene.get("solarbank_info") or {}).get("solarbank_list") or []
            device_sn = banks[0].get("device_sn", "") if banks else ""
            today = await _fetch_energy(client, site_id, device_sn)

        _persist_auth_file(auth_file)
        return scene, today


async def fetch_scene() -> dict:
    """Nur die Rohantwort, fuer den Diagnose-Endpoint."""
    scene, _ = await fetch_all(with_today=False)
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

    scene, today = await fetch_all()
    result = normalize(scene, today)
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
