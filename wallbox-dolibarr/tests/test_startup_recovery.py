"""Regressionstests für check_startup_session — das Datenverlust-Bug vom
25./26. Juli: ein Neustart mitten in einer Ladung verwarf die laufende
Session als 'incomplete', obwohl der (kumulative) Zähler die Energie
korrekt mitgezählt hatte. ~25 kWh gingen dadurch nicht in die Abrechnung.

Ausführen:  cd wallbox-dolibarr && python3 -m pytest tests/ -q
"""
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ADDON_DIR = os.path.dirname(_HERE)
sys.path.insert(0, _ADDON_DIR)

import main  # noqa: E402
import wallbox_profile  # noqa: E402
from session_manager import SessionManager  # noqa: E402


class _FakeHaWs:
    """Ersetzt den Websocket-Snapshot beim Start mit festen Sensor-Werten."""

    def __init__(self, state_val, energy_val, rfid_val=""):
        self.state_val = state_val
        self.energy_val = energy_val
        self.rfid_val = rfid_val

    async def get_all_states(self):
        profile = main.profile
        snapshot = {}
        if profile.sensor_state:
            snapshot[profile.sensor_state] = {"state": self.state_val}
        if profile.sensor_energy:
            snapshot[profile.sensor_energy] = {"state": self.energy_val}
        if profile.sensor_rfid:
            snapshot[profile.sensor_rfid] = {"state": self.rfid_val}
        return snapshot


@pytest.fixture()
def addon_env(tmp_path):
    """Setzt main.py's Modul-Globals auf einen sauberen, isolierten Zustand
    (frische SQLite-DB, Alfen-Standardprofil, kein API-Client)."""
    db_path = tmp_path / "sessions.db"
    main.session_manager = SessionManager(db_path=str(db_path))
    main.current_config = {}
    main.profile = wallbox_profile.resolve_profile({})  # Alfen-Standard
    main.api_client = None
    main.api_state = {"client": None, "current_energy": None, "wallbox_state": None, "last_update": None}
    main._pending_auth = None
    main._latest_energy = None
    main._latest_rfid = None
    main._last_power_high_time = None
    main._last_energy_change_time = None
    yield main.session_manager


@pytest.mark.asyncio
async def test_session_ended_during_outage_is_closed_with_correct_kwh(addon_env):
    """Kernszenario des Bugs: Ladung lief, HA/Addon startete neu, Auto war beim
    Wiederverbinden schon abgesteckt (state='Available'). Die Session MUSS mit
    dem beim Neustart aktuellen (kumulativen) Zählerstand korrekt abgeschlossen
    werden — NICHT als 'incomplete' verworfen."""
    sm = addon_env
    sm.start_session("6C62083E", start_energy_kwh=5774.47, wallbox_id="alfen_eve")

    main.ha_ws = _FakeHaWs(state_val="Available", energy_val="5812.55")
    await main.check_startup_session()

    rows = sm.get_completed_sessions(limit=5)
    assert len(rows) == 1, "Session muss als 'completed' übertragbar sein, nicht verworfen"
    assert rows[0]["total_kwh"] == pytest.approx(38.08, abs=0.01)
    assert sm.get_active_session() is None


@pytest.mark.asyncio
async def test_session_still_charging_at_restart_stays_open(addon_env):
    """Lädt die Wallbox beim Neustart noch, darf die Session NICHT angefasst
    werden — sie läuft weiter und wird ganz normal später beendet."""
    sm = addon_env
    sid = sm.start_session("6C62083E", start_energy_kwh=100.0, wallbox_id="alfen_eve")

    main.ha_ws = _FakeHaWs(state_val="Charging Power On", energy_val="105.0")
    await main.check_startup_session()

    active = sm.get_active_session()
    assert active is not None and active["id"] == sid
    assert sm.get_completed_sessions(limit=5) == []


@pytest.mark.asyncio
async def test_session_paused_by_load_management_at_restart_stays_open(addon_env):
    """Lastmanagement-Pause beim Neustart (z.B. 'Suspended EVSE') darf die
    Session ebenfalls nicht beenden — Fahrzeug ist noch angesteckt."""
    sm = addon_env
    sid = sm.start_session("6C62083E", start_energy_kwh=200.0, wallbox_id="alfen_eve")

    main.ha_ws = _FakeHaWs(state_val="Suspended EVSE", energy_val="203.0")
    await main.check_startup_session()

    active = sm.get_active_session()
    assert active is not None and active["id"] == sid


@pytest.mark.asyncio
async def test_session_ended_during_outage_without_energy_reading_is_incomplete(addon_env):
    """Ohne einen gültigen Zählerstand beim Neustart lässt sich die kWh nicht
    berechnen — dann bleibt 'incomplete' die einzig ehrliche Option."""
    sm = addon_env
    sm.start_session("6C62083E", start_energy_kwh=50.0, wallbox_id="alfen_eve")

    main.ha_ws = _FakeHaWs(state_val="Available", energy_val="unavailable")
    await main.check_startup_session()

    assert sm.get_active_session() is None
    assert sm.get_completed_sessions(limit=5) == []
