"""Tests fuer den Telegram-Listener -- ohne Netzwerk, ohne echte Prozesse.

``os.kill`` wird durchgehend gemockt statt auf echtes Betriebssystemverhalten
zu vertrauen: ``os.kill(pid, 0)`` fuer eine tote PID verhaelt sich unter
Windows nachweislich anders als unter Linux (dem tatsaechlichen Zielsystem,
ein VPS). Ein Test, der sich auf reale PID-Lebenszeit verlaesst, waere auf
dieser Entwicklungsmaschine nicht aussagekraeftig.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import listener  # noqa: E402


class FakePopen:
    """Minimaler Ersatz fuer subprocess.Popen -- nur was ListenerState nutzt."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self._returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def finish(self, returncode: int = 0) -> None:
        self._returncode = returncode


@pytest.fixture
def spawn_calls() -> list[FakePopen]:
    return []


def make_state(tmp_path: Path, spawn_calls: list[FakePopen]) -> listener.ListenerState:
    lock_path = tmp_path / "scout.lock"

    def spawn() -> FakePopen:
        p = FakePopen(pid=1000 + len(spawn_calls))
        spawn_calls.append(p)
        return p

    return listener.ListenerState(lock_path, spawn=spawn)


# --- _read_lock_pid ------------------------------------------------------------
def test_lock_pid_fehlende_datei(tmp_path: Path) -> None:
    assert listener._read_lock_pid(tmp_path / "nope.lock") is None


def test_lock_pid_ungueltiger_inhalt(tmp_path: Path) -> None:
    lock = tmp_path / "scout.lock"
    lock.write_text("nicht-numerisch")
    assert listener._read_lock_pid(lock) is None


def test_lock_pid_lebender_prozess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock = tmp_path / "scout.lock"
    lock.write_text("777")
    monkeypatch.setattr(listener.os, "kill", lambda pid, sig: None)
    assert listener._read_lock_pid(lock) == 777


def test_lock_pid_toter_prozess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock = tmp_path / "scout.lock"
    lock.write_text("777")

    def raise_dead(pid: int, sig: int) -> None:
        raise OSError("no such process")

    monkeypatch.setattr(listener.os, "kill", raise_dead)
    assert listener._read_lock_pid(lock) is None


# --- ListenerState.start ---------------------------------------------------
def test_start_spawnt_wenn_idle(tmp_path: Path, spawn_calls: list[FakePopen]) -> None:
    state = make_state(tmp_path, spawn_calls)
    reply = state.start()
    assert len(spawn_calls) == 1
    assert "Lauf gestartet" in reply
    assert str(spawn_calls[0].pid) in reply


def test_start_verweigert_bei_eigenem_aktivem_lauf(
    tmp_path: Path, spawn_calls: list[FakePopen]
) -> None:
    state = make_state(tmp_path, spawn_calls)
    state.start()
    reply = state.start()
    assert len(spawn_calls) == 1  # kein zweiter Spawn
    assert "bereits aktiv" in reply


def test_start_nach_prozessende_spawnt_erneut(
    tmp_path: Path, spawn_calls: list[FakePopen]
) -> None:
    state = make_state(tmp_path, spawn_calls)
    state.start()
    spawn_calls[0].finish(0)
    reply = state.start()
    assert len(spawn_calls) == 2
    assert "Lauf gestartet" in reply


def test_start_verweigert_bei_externem_lock(
    tmp_path: Path, spawn_calls: list[FakePopen], monkeypatch: pytest.MonkeyPatch
) -> None:
    state = make_state(tmp_path, spawn_calls)
    state.lock_path.write_text("555")
    monkeypatch.setattr(listener.os, "kill", lambda pid, sig: None)  # PID lebt
    reply = state.start()
    assert len(spawn_calls) == 0
    assert "555" in reply
    assert "bereits aktiv" in reply


# --- ListenerState.stop -----------------------------------------------------
def test_stop_ohne_aktiven_lauf(tmp_path: Path, spawn_calls: list[FakePopen]) -> None:
    state = make_state(tmp_path, spawn_calls)
    assert "Kein Lauf aktiv" in state.stop()


def test_stop_beendet_eigenen_prozess(tmp_path: Path, spawn_calls: list[FakePopen]) -> None:
    state = make_state(tmp_path, spawn_calls)
    state.start()
    reply = state.stop()
    assert spawn_calls[0].terminated is True
    assert state.terminate_requested_at is not None
    assert "Abbruch angefordert" in reply


def test_stop_signalisiert_externe_pid(
    tmp_path: Path, spawn_calls: list[FakePopen], monkeypatch: pytest.MonkeyPatch
) -> None:
    state = make_state(tmp_path, spawn_calls)
    state.lock_path.write_text("555")

    signaled = []

    def tracking_kill(pid: int, sig: int) -> None:
        signaled.append((pid, sig))  # simuliert eine lebende, erreichbare PID

    monkeypatch.setattr(listener.os, "kill", tracking_kill)
    reply = state.stop()
    assert (555, listener.signal.SIGTERM) in signaled
    assert "555" in reply


# --- ListenerState.status ----------------------------------------------------
def test_status_idle(tmp_path: Path, spawn_calls: list[FakePopen]) -> None:
    state = make_state(tmp_path, spawn_calls)
    assert "Kein Lauf aktiv" in state.status()


def test_status_eigener_lauf(tmp_path: Path, spawn_calls: list[FakePopen]) -> None:
    state = make_state(tmp_path, spawn_calls)
    state.start()
    assert "aktiv" in state.status()
    assert str(spawn_calls[0].pid) in state.status()


def test_status_externer_lauf(
    tmp_path: Path, spawn_calls: list[FakePopen], monkeypatch: pytest.MonkeyPatch
) -> None:
    state = make_state(tmp_path, spawn_calls)
    state.lock_path.write_text("555")
    monkeypatch.setattr(listener.os, "kill", lambda pid, sig: None)
    assert "555" in state.status()


# --- ListenerState.poll_finished --------------------------------------------
def test_poll_finished_ohne_prozess(tmp_path: Path, spawn_calls: list[FakePopen]) -> None:
    state = make_state(tmp_path, spawn_calls)
    assert state.poll_finished() is None


def test_poll_finished_meldet_erfolg_und_raeumt_auf(
    tmp_path: Path, spawn_calls: list[FakePopen]
) -> None:
    state = make_state(tmp_path, spawn_calls)
    state.start()
    spawn_calls[0].finish(0)
    note = state.poll_finished()
    assert note is not None and "✅" in note
    assert state.process is None
    assert state.started_at is None


def test_poll_finished_meldet_abbruch_bei_130(
    tmp_path: Path, spawn_calls: list[FakePopen]
) -> None:
    state = make_state(tmp_path, spawn_calls)
    state.start()
    spawn_calls[0].finish(130)
    note = state.poll_finished()
    assert note is not None and "abgebrochen" in note


def test_poll_finished_meldet_fehler_bei_anderem_exitcode(
    tmp_path: Path, spawn_calls: list[FakePopen]
) -> None:
    state = make_state(tmp_path, spawn_calls)
    state.start()
    spawn_calls[0].finish(1)
    note = state.poll_finished()
    assert note is not None and "fehlgeschlagen" in note and "Exit 1" in note


def test_poll_finished_eskaliert_nach_grace_periode(
    tmp_path: Path, spawn_calls: list[FakePopen]
) -> None:
    """Reagiert der Prozess lange nach dem SIGTERM nicht, wird SIGKILL geschickt."""
    state = make_state(tmp_path, spawn_calls)
    state.start()
    state.stop()
    assert spawn_calls[0].killed is False

    # Grace-Periode kuenstlich verstreichen lassen, ohne echte Zeit zu warten.
    state.terminate_requested_at = datetime.now(timezone.utc) - timedelta(
        seconds=listener.TERMINATE_GRACE_SECONDS + 10
    )
    note = state.poll_finished()
    assert spawn_calls[0].killed is True
    assert note is None  # Prozess laeuft (in diesem Fake) noch, keine Abschlussmeldung
    assert state.terminate_requested_at is None  # nur einmal eskalieren


def test_poll_finished_eskaliert_nicht_vor_grace_periode(
    tmp_path: Path, spawn_calls: list[FakePopen]
) -> None:
    state = make_state(tmp_path, spawn_calls)
    state.start()
    state.stop()
    state.terminate_requested_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    state.poll_finished()
    assert spawn_calls[0].killed is False


# --- dispatch ------------------------------------------------------------------
def test_dispatch_scan_startet(tmp_path: Path, spawn_calls: list[FakePopen]) -> None:
    state = make_state(tmp_path, spawn_calls)
    reply = listener.dispatch("/scan", state)
    assert len(spawn_calls) == 1
    assert "Lauf gestartet" in reply


def test_dispatch_run_ist_alias_fuer_scan(
    tmp_path: Path, spawn_calls: list[FakePopen]
) -> None:
    state = make_state(tmp_path, spawn_calls)
    listener.dispatch("/run", state)
    assert len(spawn_calls) == 1


def test_dispatch_ist_gross_klein_unabhaengig(
    tmp_path: Path, spawn_calls: list[FakePopen]
) -> None:
    state = make_state(tmp_path, spawn_calls)
    listener.dispatch("/SCAN", state)
    assert len(spawn_calls) == 1


def test_dispatch_stop(tmp_path: Path, spawn_calls: list[FakePopen]) -> None:
    state = make_state(tmp_path, spawn_calls)
    state.start()
    reply = listener.dispatch("/stop", state)
    assert spawn_calls[0].terminated is True
    assert "Abbruch" in reply


def test_dispatch_status(tmp_path: Path, spawn_calls: list[FakePopen]) -> None:
    state = make_state(tmp_path, spawn_calls)
    assert "Kein Lauf aktiv" in listener.dispatch("/status", state)


@pytest.mark.parametrize("text", ["/start", "/help", "", "   ", "hallo", "/unbekannt"])
def test_dispatch_faellt_auf_hilfe_zurueck(
    tmp_path: Path, spawn_calls: list[FakePopen], text: str
) -> None:
    state = make_state(tmp_path, spawn_calls)
    reply = listener.dispatch(text, state)
    assert reply == listener.HELP_TEXT
    assert len(spawn_calls) == 0  # /start loest KEINEN Scan aus (siehe Modul-Docstring)


# --- _spawn_scan -------------------------------------------------------------
def test_spawn_scan_baut_korrekten_aufruf(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_popen(args: list[str], **kwargs: Any) -> FakePopen:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakePopen()

    monkeypatch.setattr(listener.subprocess, "Popen", fake_popen)
    listener._spawn_scan(python_bin="/opt/venv/bin/python")

    assert captured["args"][0] == "/opt/venv/bin/python"
    assert captured["args"][1] == str(listener.SCOUT_SCRIPT)
    assert "--notify-telegram" in captured["args"]
    assert "--quiet" in captured["args"]
    assert captured["kwargs"]["cwd"] == str(listener.PROJECT_DIR)
