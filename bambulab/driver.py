import asyncio
import json
import logging
import threading
from datetime import datetime
from typing import Any, Callable

from app.plugins.base import BaseDriver

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60
DEFAULT_RECONNECT_INTERVAL = 5


class PendingSpool:
    def __init__(self, spool_id: int, filament_data: dict, slot_index: str | None = None):
        self.spool_id = spool_id
        self.filament_data = filament_data
        self.slot_index = slot_index
        self.started_at = datetime.utcnow()
        self.timer: asyncio.Task | None = None


class Driver(BaseDriver):
    driver_key = "bambulab"

    def __init__(
        self,
        printer_id: int,
        config: dict[str, Any],
        emitter: Callable[[dict[str, Any]], None],
    ):
        super().__init__(printer_id, config, emitter)
        self._printer: Any = None  # bambulabs_api.Printer
        self._pending: PendingSpool | None = None
        self._timeout_seconds = config.get("timeout_seconds", DEFAULT_TIMEOUT)
        self._host = config.get("host", "")
        self._serial = config.get("serial", "")
        self._access_code = config.get("access_code", "")
        self._connected = False
        self._reconnect_interval = config.get("reconnect_interval_minutes", DEFAULT_RECONNECT_INTERVAL) * 60
        self._current_slots: list[dict[str, Any]] = []
        self._current_ams_units: list[dict[str, Any]] = []
        self._printer_model = config.get("printer_model", "P1S")
        self._is_ams_lite = self._printer_model in ("A1", "A1_MINI")
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ams_serials: dict[str, str] = {}  # ams_id -> serial number
        self._slots_processed = False  # Nur einmal pro Verbindung verarbeiten

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
        logger.info(f"Bambu driver started for printer {self.printer_id} at {self._host}")

    async def stop(self) -> None:
        self._running = False
        if self._pending and self._pending.timer:
            self._pending.timer.cancel()
            self._pending = None
        if self._printer:
            self._printer.mqtt_stop()
            self._printer = None
        self._connected = False

    # -- paho-Thread Callbacks ------------------------------------------------

    def _on_connect(self, mqtt_client, client, userdata, flags, rc, properties):
        """Wird im paho-Thread aufgerufen wenn MQTT verbunden ist."""
        self._connected = True
        logger.info(f"Bambu driver connected to printer {self.printer_id} at {self._host}")
        self.log_debug("event", "mqtt", {"event": "connected", "rc": str(rc)})

    def _on_disconnect(self, mqtt_client, client, userdata, disconnect_flags, rc, properties):
        """Wird im paho-Thread aufgerufen wenn MQTT getrennt wird."""
        self._connected = False
        self._slots_processed = False
        logger.warning(f"Bambu driver disconnected from printer {self.printer_id}: {rc}")
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
        Wird nur einmal pro Verbindung verarbeitet — AMS-Konfiguration ändert sich
        im laufenden Betrieb nicht."""
        if self._slots_processed:
            return

        print_data = payload.get("print", {})
        ams_section = print_data.get("ams")
        vt_tray = print_data.get("vt_tray")

        # Nur verarbeiten wenn AMS- oder vt_tray-Daten vorhanden
        if ams_section is None and vt_tray is None:
            return

        ams_data = (ams_section or {}).get("ams", [])

        slots: list[dict[str, Any]] = []

        # AMS-Einheiten Metadaten
        ams_units: list[dict[str, Any]] = []
        for ams_unit in ams_data:
            ams_id = int(ams_unit.get("id", 0))
            ams_units.append({
                "ams_id": ams_id,
                "humidity": ams_unit.get("humidity"),
                "temp": ams_unit.get("temp"),
                "tray_count": len(ams_unit.get("tray", [])),
                "serial": self._ams_serials.get(str(ams_id), None),
            })
        self._current_ams_units = ams_units

        # AMS-Trays verarbeiten
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
                    slot_name = f"AMS {ams_id} - Slot {tray_id + 1}"

                present = bool(tray_type)
                slots.append({
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
                })

                # Pending-Spool Match (nur bei belegtem Tray)
                if present and self._pending and not self._pending.slot_index:
                    logger.info(f"Pending match: slot {slot_index} has spool")
                    self._send_filament_setting(ams_id, tray_id, self._pending.filament_data)
                    if self._pending.timer and self._loop:
                        self._loop.call_soon_threadsafe(self._pending.timer.cancel)
                    self._pending = None

                if not present and self._pending and self._pending.slot_index == slot_index:
                    pass

        # Externe Spule (vt_tray) — immer auswerten wenn vorhanden
        has_external = vt_tray is not None
        if has_external:
            ext_has_filament = bool(vt_tray.get("tray_type"))
            slots.append({
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
            })

            if ext_has_filament and self._pending and self._pending.slot_index == "255-254":
                logger.info("Pending match: external tray has spool")
                self._send_filament_setting(255, 254, self._pending.filament_data)
                if self._pending.timer and self._loop:
                    self._loop.call_soon_threadsafe(self._pending.timer.cancel)
                self._pending = None

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

        # Event an System melden (muss im asyncio-Thread passieren)
        self._current_slots = slots
        self._slots_processed = True
        if self._loop:
            self._loop.call_soon_threadsafe(
                self.emit,
                {"event_type": "slots_update", "slots": slots, "ams_info": ams_info},
            )

    # -- Filament-Setting senden ----------------------------------------------

    def _send_filament_setting(self, ams_id: int, tray_id: int, filament_data: dict) -> None:
        """Filament-Setting an Drucker senden. Läuft in separatem Thread,
        da set_filament_printer() blockierend ist (wait_for_publish)."""
        threading.Thread(
            target=self._do_send_filament_setting,
            args=(ams_id, tray_id, filament_data),
            daemon=True,
        ).start()

    def _do_send_filament_setting(self, ams_id: int, tray_id: int, filament_data: dict) -> None:
        """Blockierender Filament-Setting Versand (Thread-Pool)."""
        if not self._printer:
            logger.error("Cannot send filament setting: not connected")
            return

        color = filament_data.get("color", "")
        if len(color) == 8:
            color = color[:6]  # Alpha-Kanal entfernen
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
            self.log_debug("out", f"device/{self._serial}/request",
                           {"command": "ams_filament_setting", "success": result})
            logger.info(f"Sent filament setting to printer {self.printer_id}: slot {ams_id}-{tray_id}")
        except Exception as e:
            logger.error(f"Failed to send filament setting: {e}")

    # -- Pending-Spool API ----------------------------------------------------

    async def assign_pending_spool(
        self,
        spool_id: int,
        filament_data: dict,
        slot_index: str | None = None,
    ) -> None:
        """Spule für automatische Zuweisung vormerken."""
        if self._pending and self._pending.timer:
            self._pending.timer.cancel()

        self._pending = PendingSpool(spool_id, filament_data, slot_index)
        self._pending.timer = asyncio.create_task(self._timeout_task())
        logger.info(f"Pending spool {spool_id} for printer {self.printer_id} (slot: {slot_index})")

    async def _timeout_task(self) -> None:
        """Wartet auf Timeout, dann verwirft Pending."""
        await asyncio.sleep(self._timeout_seconds)
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
        }
