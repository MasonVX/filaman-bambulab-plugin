import asyncio
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import func, select

from app.core.database import async_session_maker
from app.models.location import Location
from app.models.printer import Printer
from app.models.spool import Spool
from app.plugins.base import BaseDriver
from app.services.spool_service import SpoolService

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60
DEFAULT_RECONNECT_INTERVAL = 5


class PendingSpool:
    def __init__(
        self, spool_id: int, filament_data: dict, slot_index: str | None = None
    ):
        self.spool_id = spool_id
        self.filament_data = filament_data
        self.slot_index = slot_index
        self.started_at = datetime.utcnow()
        self.timer: asyncio.Task | None = None


class Driver(BaseDriver):
    driver_key = "bambulab"

    # NOTE: Sending filament settings (spool assignment) requires the printer to be
    # in LAN-only mode with Developer Mode enabled. Without these settings, only
    # reading (AMS status, slot data) is possible. This is a Bambu Lab restriction —
    # unsigned control commands are rejected by the printer firmware.

    def __init__(
        self,
        printer_id: int,
        config: dict[str, Any],
        emitter: Callable[[dict[str, Any]], None],
    ):
        super().__init__(printer_id, config, emitter)
        self._printer: Any = None  # bambulabs_api.Printer
        self._pending: PendingSpool | None = None
        self._timeout_seconds = (
            DEFAULT_TIMEOUT  # Can be overridden per assign_pending_spool call
        )
        self._host = config.get("host", "")
        self._serial = config.get("serial", "")
        self._access_code = config.get("access_code", "")
        self._connected = False
        self._reconnect_interval = (
            config.get("reconnect_interval_minutes", DEFAULT_RECONNECT_INTERVAL) * 60
        )
        self._current_slots: list[dict[str, Any]] = []
        self._current_ams_units: list[dict[str, Any]] = []
        self._printer_model = config.get("printer_model", "P1S")
        self._is_ams_lite = self._printer_model in ("A1", "A1_MINI")
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ams_serials: dict[str, str] = {}  # ams_id -> serial number
        self._printer_name: str | None = None  # Wird in start() aus DB geladen

    async def start(self) -> None:
        from bambulabs_api import Printer

        self._running = True
        self._loop = asyncio.get_running_loop()

        self._printer = Printer(
            ip_address=self._host,
            access_code=self._access_code,
            serial=self._serial,
        )

        # Callbacks laufen im paho-Thread
        self._printer.mqtt_client.on_connect_handler = self._on_connect
        self._printer.mqtt_client.on_message_handler = self._on_message
        self._printer.mqtt_client.on_disconnect_handler = self._on_disconnect

        # Reconnect-Backoff konfigurieren (paho auto-reconnect via loop_start)
        self._printer.mqtt_client._client.reconnect_delay_set(
            min_delay=1,
            max_delay=self._reconnect_interval,
        )

        # MQTT starten (non-blocking: connect_async + loop_start in paho-Thread)
        # pushall wird automatisch beim Connect gesendet (pushall_on_connect=True)
        self._printer.mqtt_start()
        logger.info(
            f"Bambu driver started for printer {self.printer_id} at {self._host}"
        )

        # Printer-Namen aus DB laden für Location-Generierung
        try:
            async with async_session_maker() as db:
                printer = await db.get(Printer, self.printer_id)
                self._printer_name = (
                    printer.name if printer else f"Printer {self.printer_id}"
                )
        except Exception as e:
            logger.warning(f"Failed to load printer name: {e}")
            self._printer_name = f"Printer {self.printer_id}"

    async def stop(self) -> None:
        self._running = False
        if self._pending and self._pending.timer:
            self._pending.timer.cancel()
            self._pending = None
        if self._printer:
            try:
                self._printer.mqtt_client._client.disconnect()
            except Exception:
                pass
            self._printer.mqtt_stop()
            self._printer = None
        self._connected = False

    def _generate_slot_location_name(self, ams_id: int, tray_id: int) -> str:
        """Generiert Location-Namen für AMS-Slot.

        Format:
        - AMS Slots: "{Drucker Name} - AMS {A-D}{ams_id+1}"
        - External Slots: "{Drucker Name} - ext. Slot {tray_id+1}"

        Beispiele:
        - "Bambu P1S - AMS A2" (ams_id=0, tray_id=1)
        - "Bambu X1C - ext. Slot 1" (ams_id=255, tray_id=0)
        """
        printer_name = self._printer_name or f"Printer {self.printer_id}"

        if ams_id >= 200:  # External slot (255, 254)
            return f"{printer_name} - ext. Slot {tray_id + 1}"
        else:
            # AMS slots: A1, B1, C1, D1, A2, B2, ... (tray_id 0-3)
            slot_label = chr(65 + tray_id)  # 65 = 'A' in ASCII
            return f"{printer_name} - AMS {slot_label}{ams_id + 1}"

    async def _update_spool_location(
        self, filaman_spool_id: int, ams_id: int, tray_id: int
    ) -> None:
        """Setzt Spulen-Standort auf AMS-Slot-Location.

        Erstellt die Location automatisch falls sie noch nicht existiert.
        Nutzt SpoolService.move_location() für konsistente Event-Generierung.
        """
        try:
            slot_location_name = self._generate_slot_location_name(ams_id, tray_id)

            async with async_session_maker() as db:
                # 1. Location suchen (case-insensitive)
                result = await db.execute(
                    select(Location).where(
                        func.lower(Location.name) == slot_location_name.lower()
                    )
                )
                location = result.scalar_one_or_none()

                # 2. Location erstellen falls nicht vorhanden
                if not location:
                    location = Location(
                        name=slot_location_name,
                        identifier=f"bambulab_{self.printer_id}_{ams_id}_{tray_id}",
                        custom_fields={
                            "managed_by": "bambulab_plugin",
                            "printer_id": self.printer_id,
                        },
                    )
                    db.add(location)
                    await db.flush()  # Für location.id
                    logger.info(f"Created location: {slot_location_name}")

                # 3. Spule zur Location bewegen (wenn nicht bereits dort)
                spool = await db.get(Spool, filaman_spool_id)
                if not spool:
                    logger.warning(
                        f"Spool {filaman_spool_id} not found, cannot update location"
                    )
                    return

                if spool.location_id == location.id:
                    logger.debug(
                        f"Spool {filaman_spool_id} already at location '{slot_location_name}'"
                    )
                    return

                # SpoolService für konsistente Event-Generierung nutzen
                await SpoolService(db).move_location(
                    spool,
                    location.id,
                    datetime.now(timezone.utc),
                    source="driver",
                    note=f"Assigned to {slot_location_name}",
                )

                # Einmaliger commit für beide Operationen (Location + Move)
                await db.commit()

                logger.info(
                    f"Moved spool {filaman_spool_id} to location '{slot_location_name}' "
                    f"(location_id={location.id})"
                )

        except Exception as e:
            logger.error(
                f"Failed to update location for spool {filaman_spool_id} "
                f"(slot {ams_id}-{tray_id}): {e}",
                exc_info=True,
            )

    # -- paho-Thread Callbacks ------------------------------------------------

    def _on_connect(self, mqtt_client, client, userdata, flags, rc, properties):
        """Wird im paho-Thread aufgerufen wenn MQTT verbunden ist."""
        self._connected = True
        logger.info(
            f"Bambu driver connected to printer {self.printer_id} at {self._host}"
        )
        self.log_debug("event", "mqtt", {"event": "connected", "rc": str(rc)})

    def _on_disconnect(
        self, mqtt_client, client, userdata, disconnect_flags, rc, properties
    ):
        """Wird im paho-Thread aufgerufen wenn MQTT getrennt wird."""
        self._connected = False
        self._current_slots = []  # Force full re-sync on reconnect
        logger.warning(
            f"Bambu driver disconnected from printer {self.printer_id}: {rc}"
        )
        self.log_debug("event", "mqtt", {"event": "disconnected", "rc": str(rc)})
        # paho auto-reconnect via loop_start() (reconnect_on_failure=True)

    def _on_message(self, mqtt_client, client, userdata, msg):
        """Wird im paho-Thread für jede MQTT-Nachricht aufgerufen."""
        try:
            payload = json.loads(msg.payload.decode())
            self.log_debug("in", str(msg.topic), payload)

            # push_status Nachrichten verarbeiten
            if payload.get("print", {}).get("command") == "push_status":
                self._process_slots(payload)
                return

            # get_version / push_info: AMS Seriennummern extrahieren
            info_cmd = payload.get("info", {}).get("command", "")
            if info_cmd in ("get_version", "push_info"):
                self._process_version_info(payload)
                return

        except json.JSONDecodeError as e:
            logger.error(f"Failed to decode MQTT message: {e}")
        except Exception as e:
            logger.error(f"Error handling MQTT message: {e}")

    def _process_version_info(self, payload: dict) -> None:
        """AMS Seriennummern aus get_version/push_info Antwort extrahieren."""
        modules = payload.get("info", {}).get("module", [])
        for module in modules:
            name = module.get("name", "")
            if name.startswith("ams/"):
                ams_id = name.split("/")[1]
                sn = module.get("sn", "")
                if sn:
                    self._ams_serials[ams_id] = sn
                    logger.debug(f"AMS {ams_id} serial: {sn}")

    # -- Slot-Verarbeitung (paho-Thread) --------------------------------------

    def _process_slots(self, payload: dict) -> None:
        """AMS/Tray-Daten aus push_status extrahieren und slots_update emittieren.
        Wird bei jeder push_status Nachricht aufgerufen. Emittiert nur wenn sich
        die Slot-Daten geändert haben, um unnötige DB-Writes zu vermeiden.

        Merge-Strategie: Nur Slot-Kategorien aktualisieren, die in der aktuellen
        Nachricht vorhanden sind. Fehlende Kategorien behalten ihren vorherigen Zustand.
        BambuLab sendet nicht immer alle Daten in jeder push_status Nachricht.

        Auto-assignment nutzt ausschließlich Feld-Vergleich (wie C++ Referenz):
        Erkennt Änderungen in tray_info_idx, tray_type, tray_color, cali_idx, setting_id."""

        print_data = payload.get("print", {})
        ams_section = print_data.get("ams")
        vt_tray = print_data.get("vt_tray")

        # Nur verarbeiten wenn AMS- oder vt_tray-Daten vorhanden
        if ams_section is None and vt_tray is None:
            return

        ams_data = (ams_section or {}).get("ams", [])

        # Leichtgewichtige Nachricht (nur tray_now/version) — keine Slot-Daten vorhanden
        if not ams_data and vt_tray is None:
            return

        # -- Merge-Strategie: vorherige Slots als Basis, nur vorhandene Daten aktualisieren --
        # BambuLab sendet nicht immer ams UND vt_tray in jeder push_status Nachricht.
        # Ohne Merge würde das Fehlen einer Kategorie deren Slots löschen (Flicker).
        prev_ams_slots = [
            s
            for s in self._current_slots
            if not s.get("slot_index", "").startswith("255-")
        ]
        prev_ext_slots = [
            s for s in self._current_slots if s.get("slot_index", "").startswith("255-")
        ]

        # AMS-Einheiten Metadaten — nur aktualisieren wenn AMS-Daten vorhanden
        if ams_data:
            ams_units: list[dict[str, Any]] = []
            for ams_unit in ams_data:
                ams_id = int(ams_unit.get("id", 0))
                ams_units.append(
                    {
                        "ams_id": ams_id,
                        "humidity": ams_unit.get(
                            "humidity_raw", ams_unit.get("humidity")
                        ),
                        "temp": ams_unit.get("temp"),
                        "tray_count": len(ams_unit.get("tray", [])),
                        "serial": self._ams_serials.get(str(ams_id), None),
                    }
                )
            self._current_ams_units = ams_units
        else:
            ams_units = list(self._current_ams_units)

        # AMS-Trays: nur aktualisieren wenn ams_data vorhanden, sonst vorherige beibehalten
        if ams_data:
            ams_slots: list[dict[str, Any]] = []
            for ams_unit in ams_data:
                ams_id = int(ams_unit.get("id", 0))
                trays = ams_unit.get("tray", [])

                for tray in trays:
                    tray_id = int(tray.get("id", 0))
                    slot_index = f"{ams_id}-{tray_id}"
                    tray_type = tray.get("tray_type", "")

                    if self._is_ams_lite:
                        slot_name = f"AMS Lite - Slot {tray_id + 1}"
                    else:
                        slot_name = f"AMS {ams_id + 1} - Slot {tray_id + 1}"

                    present = bool(tray_type)
                    ams_slots.append(
                        {
                            "slot_index": slot_index,
                            "slot_name": slot_name,
                            "tray_info_idx": tray.get("tray_info_idx", ""),
                            "tray_type": tray_type,
                            "tray_color": tray.get("tray_color", ""),
                            "nozzle_temp_min": tray.get("nozzle_temp_min"),
                            "nozzle_temp_max": tray.get("nozzle_temp_max"),
                            "setting_id": tray.get("setting_id", ""),
                            "cali_idx": tray.get("cali_idx"),
                            "present": present,
                        }
                    )
        else:
            ams_slots = prev_ams_slots

        # Externe Spule: nur aktualisieren wenn vt_tray vorhanden, sonst vorherige beibehalten
        if vt_tray is not None:
            ext_has_filament = bool(vt_tray.get("tray_type"))
            ext_slots = [
                {
                    "slot_index": "255-254",
                    "slot_name": "External Tray",
                    "tray_info_idx": vt_tray.get("tray_info_idx", ""),
                    "tray_type": vt_tray.get("tray_type", ""),
                    "tray_color": vt_tray.get("tray_color", ""),
                    "nozzle_temp_min": vt_tray.get("nozzle_temp_min"),
                    "nozzle_temp_max": vt_tray.get("nozzle_temp_max"),
                    "setting_id": vt_tray.get("setting_id", ""),
                    "cali_idx": vt_tray.get("cali_idx"),
                    "present": ext_has_filament,
                }
            ]
        else:
            ext_slots = prev_ext_slots

        # Zusammenführen: AMS-Slots + External Slot
        slots = ams_slots + ext_slots
        has_external = len(ext_slots) > 0

        # -- Auto-assignment: Tray-Daten-Vergleich (wie C++ Implementierung) --
        # Erkennt wenn sich Tray-Felder ändern (Spule eingelegt/gewechselt).
        # Vergleicht tray_info_idx, tray_type, tray_color, cali_idx und setting_id
        # gegen die zuletzt gespeicherten Slot-Daten.
        # Beibehaltene (unveränderte) Slots matchen ihre Vorgänger → kein false positive.
        if self._pending and self._current_slots:
            _compare_fields = ("tray_info_idx", "tray_type", "tray_color", "cali_idx")
            for new_slot in slots:
                sid = new_slot.get("slot_index", "")
                new_tray_type = new_slot.get("tray_type", "")
                if not new_tray_type:
                    continue  # Leerer Slot, kein Assignment möglich
                # Passendes altes Slot finden
                old_slot = next(
                    (s for s in self._current_slots if s.get("slot_index") == sid), None
                )
                if old_slot is None:
                    continue  # Kein Vergleich möglich (erster Sync)
                # Wenn alter Slot leer war (tray_type war leer), setting_id zurücksetzen (wie C++)
                if not old_slot.get("tray_type", ""):
                    old_slot["setting_id"] = ""
                # setting_id null → leerer String (wie C++: if (trayObj["setting_id"].isNull()) trayObj["setting_id"] = "")
                new_setting_id = new_slot.get("setting_id") or ""
                old_setting_id = old_slot.get("setting_id") or ""
                # Prüfe ob sich relevante Felder geändert haben
                has_changed = any(
                    new_slot.get(f, "") != old_slot.get(f, "") for f in _compare_fields
                )
                # setting_id: nur vergleichen wenn neuer Wert nicht leer ist (wie C++)
                if (
                    not has_changed
                    and new_setting_id
                    and new_setting_id != old_setting_id
                ):
                    has_changed = True
                if not has_changed:
                    continue
                # Slot-Filter: wenn Pending einen bestimmten Slot will
                if (
                    self._pending.slot_index is not None
                    and self._pending.slot_index != sid
                ):
                    continue
                # Parse ams_id und tray_id aus slot_index (z.B. "0-1" oder "255-254")
                try:
                    parts = sid.split("-")
                    ams_id_parsed, tray_id_parsed = int(parts[0]), int(parts[1])
                except (ValueError, IndexError):
                    continue
                logger.info(
                    f"Tray data changed at slot {sid}: "
                    f"assigning pending spool {self._pending.spool_id}"
                )
                filaman_spool_id = self._pending.spool_id
                self._send_filament_setting(
                    ams_id_parsed, tray_id_parsed, self._pending.filament_data
                )
                # Location nach erfolgreichem Auto-Assignment aktualisieren
                if self._loop and filaman_spool_id:
                    self._loop.call_soon_threadsafe(
                        lambda: asyncio.create_task(
                            self._update_spool_location(
                                filaman_spool_id, ams_id_parsed, tray_id_parsed
                            )
                        )
                    )
                if self._pending.timer and self._loop:
                    self._loop.call_soon_threadsafe(self._pending.timer.cancel)
                self._pending = None
                break  # Nur erste Änderung zuweisen

        # AMS/Slot Zusammenfassung
        total_slots = sum(u.get("tray_count", 0) for u in ams_units)
        if has_external:
            total_slots += 1
        ams_info = {
            "ams_count": len(ams_units),
            "ams_type": "AMS Lite" if self._is_ams_lite else "AMS",
            "slot_count": total_slots,
            "external_spool": has_external,
            "ams_units": ams_units,
        }

        # Nur emittieren wenn sich Slot-Daten geändert haben
        if slots == self._current_slots:
            return

        # Event an System melden (muss im asyncio-Thread passieren)
        self._current_slots = slots
        logger.info(
            f"Slot data changed for printer {self.printer_id}, emitting slots_update"
        )
        if self._loop:
            self._loop.call_soon_threadsafe(
                self.emit,
                {"event_type": "slots_update", "slots": slots, "ams_info": ams_info},
            )

    # -- Filament-Setting senden ----------------------------------------------

    def _send_filament_setting(
        self, ams_id: int, tray_id: int, filament_data: dict
    ) -> None:
        """Filament-Setting an Drucker senden. Läuft in separatem Thread,
        da set_filament_printer() blockierend ist (wait_for_publish)."""
        threading.Thread(
            target=self._do_send_filament_setting,
            args=(ams_id, tray_id, filament_data),
            daemon=True,
        ).start()

    def _do_send_filament_setting(
        self, ams_id: int, tray_id: int, filament_data: dict
    ) -> None:
        """Blockierender Filament-Setting Versand (Thread-Pool)."""
        if not self._printer:
            logger.error("Cannot send filament setting: not connected")
            return

        color = filament_data.get("color", "")
        if len(color) == 8:
            color = color[:6]  # Alpha-Kanal entfernen, bambulabs_api erwartet 6 Zeichen
        elif len(color) != 6:
            color = "FFFFFF"  # Default weiß

        try:
            from bambulabs_api import AMSFilamentSettings

            filament = AMSFilamentSettings(
                tray_info_idx=filament_data.get("tray_info_idx", "GFL99"),
                nozzle_temp_min=filament_data.get("nozzle_temp_min", 190),
                nozzle_temp_max=filament_data.get("nozzle_temp_max", 230),
                tray_type=filament_data.get("material_type", "PLA"),
            )
            result = self._printer.set_filament_printer(
                color=color,
                filament=filament,
                ams_id=ams_id if ams_id < 200 else 255,
                tray_id=tray_id if tray_id < 200 else 254,
            )
            self.log_debug(
                "out",
                f"device/{self._serial}/request",
                {
                    "command": "ams_filament_setting",
                    "ams_id": ams_id if ams_id < 200 else 255,
                    "tray_id": tray_id if tray_id < 200 else 254,
                    "tray_info_idx": filament_data.get("tray_info_idx", "GFL99"),
                    "tray_color": f"{color.upper()}FF",
                    "nozzle_temp_min": filament_data.get("nozzle_temp_min", 190),
                    "nozzle_temp_max": filament_data.get("nozzle_temp_max", 230),
                    "tray_type": filament_data.get("material_type", "PLA"),
                    "success": result,
                },
            )
            logger.info(
                f"Sent filament setting to printer {self.printer_id}: slot {ams_id}-{tray_id}"
            )
        except Exception as e:
            logger.error(f"Failed to send filament setting: {e}")

    async def reconnect(self) -> None:
        """Reconnect: MQTT stoppen und neu starten."""
        logger.info(f"Reconnecting Bambu driver for printer {self.printer_id}")
        if self._printer:
            try:
                self._printer.mqtt_client._client.disconnect()
            except Exception:
                pass
            self._printer.mqtt_stop()
            self._connected = False
            self._current_slots = []  # Force full re-sync on reconnect
            self._printer.mqtt_start()
            logger.info(f"Bambu driver reconnected for printer {self.printer_id}")

    def send_filament_to_tray(
        self, ams_id: int, tray_id: int, filament_data: dict
    ) -> None:
        """Filament-Setting direkt an einen bestimmten Tray senden (ohne Pending-Mechanismus)."""
        self._send_filament_setting(ams_id, tray_id, filament_data)

        # Location nach erfolgreichem Direkt-Assignment aktualisieren
        filaman_spool_id = filament_data.get("filaman_spool_id")
        if filaman_spool_id and self._loop:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(
                    self._update_spool_location(filaman_spool_id, ams_id, tray_id)
                )
            )

    # -- Pending-Spool API ----------------------------------------------------

    async def assign_pending_spool(
        self,
        spool_id: int,
        filament_data: dict,
        slot_index: str | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        """Spule für automatische Zuweisung vormerken."""
        if self._pending and self._pending.timer:
            self._pending.timer.cancel()

        self._pending = PendingSpool(spool_id, filament_data, slot_index)
        effective_timeout = (
            timeout_seconds if timeout_seconds is not None else self._timeout_seconds
        )
        self._pending.timer = asyncio.create_task(self._timeout_task(effective_timeout))
        logger.info(
            f"Pending spool {spool_id} for printer {self.printer_id} (slot: {slot_index}, timeout: {effective_timeout}s)"
        )

    async def _timeout_task(self, timeout: int | None = None) -> None:
        """Wartet auf Timeout, dann verwirft Pending."""
        await asyncio.sleep(timeout if timeout is not None else self._timeout_seconds)
        if self._pending:
            logger.info(f"Pending spool {self._pending.spool_id} timed out")
            self._pending = None

    # -- Health ---------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        ext_exists = any(s.get("slot_index") == "255-254" for s in self._current_slots)
        total_slots = sum(u.get("tray_count", 0) for u in self._current_ams_units)
        if ext_exists:
            total_slots += 1
        return {
            "driver_key": self.driver_key,
            "printer_id": self.printer_id,
            "running": self._running,
            "connected": self._connected,
            "pending": self._pending is not None,
            "printer_model": self._printer_model,
            "ams_type": "AMS Lite" if self._is_ams_lite else "AMS",
            "ams_count": len(self._current_ams_units),
            "slot_count": total_slots,
            "external_spool": ext_exists,
            "ams_units": self._current_ams_units,
            "slots": self._current_slots,
        }
