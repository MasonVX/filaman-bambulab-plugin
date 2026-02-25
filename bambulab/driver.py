import asyncio
import json
import logging
import ssl
from datetime import datetime
from typing import Any, Callable

from app.plugins.base import BaseDriver

logger = logging.getLogger(__name__)

BAMBU_USERNAME = "bblp"
DEFAULT_TIMEOUT = 60


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
        self._task: asyncio.Task | None = None
        self._mqtt_client = None
        self._pending: PendingSpool | None = None
        self._timeout_seconds = config.get("timeout_seconds", DEFAULT_TIMEOUT)
        self._host = config.get("host", "")
        self._serial = config.get("serial", "")
        self._access_code = config.get("access_code", "")
        self._connected = False

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        if self._pending and self._pending.timer:
            self._pending.timer.cancel()
            self._pending = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        """Haupt-MQTT-Loop."""
        try:
            from aiomqtt import Client
            
            # SSL-Kontext ohne Zertifikatsprüfung (Bambu nutzt selbst-signierte Zerts)
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            
            async with Client(
                hostname=self._host,
                port=8883,
                username=BAMBU_USERNAME,
                password=self._access_code,
                tls_context=ssl_context,
            ) as client:
                self._mqtt_client = client
                self._connected = True
                logger.info(f"Bambu driver connected to printer {self.printer_id} at {self._host}")
                
                # Subscribe zum Report-Topic
                topic = f"device/{self._serial}/report"
                await client.subscribe(topic)
                logger.info(f"Subscribed to {topic}")
                
                # Nachrichten empfangen
                async for message in client.messages:
                    if not self._running:
                        break
                    try:
                        payload = json.loads(message.payload.decode())
                        await self._handle_mqtt_message(payload)
                    except json.JSONDecodeError as e:
                        logger.error(f"Failed to decode MQTT message: {e}")
                    except Exception as e:
                        logger.error(f"Error handling MQTT message: {e}")
                        
        except Exception as e:
            logger.error(f"MQTT connection error for printer {self.printer_id}: {e}")
            self._connected = False

    async def _handle_mqtt_message(self, payload: dict) -> None:
        """Verarbeitet eingehende MQTT-Nachrichten."""
        # Nur push_status Nachrichten verarbeiten
        if not payload.get("print", {}).get("command") == "push_status":
            return
            
        ams_data = payload.get("print", {}).get("ams", {}).get("ams", [])
        vt_tray = payload.get("print", {}).get("vt_tray", {})
        
        slots = []
        
        # Prüfe auf neue Spulen (für Pending-Match)
        for ams_unit in ams_data:
            ams_id = ams_unit.get("id", 0)
            trays = ams_unit.get("tray", [])
            
            for tray in trays:
                tray_id = tray.get("id", 0)
                slot_index = f"{ams_id}-{tray_id}"
                tray_type = tray.get("tray_type", "")
                
                if tray_type:
                    # Slot hat eine Spule
                    slots.append({
                        "slot_index": slot_index,
                        "slot_name": f"AMS {ams_id} - Slot {tray_id + 1}",
                        "tray_info_idx": tray.get("tray_info_idx", ""),
                        "tray_type": tray_type,
                        "tray_color": tray.get("tray_color", ""),
                        "nozzle_temp_min": tray.get("nozzle_temp_min"),
                        "nozzle_temp_max": tray.get("nozzle_temp_max"),
                        "present": True,
                    })
                    
                    # Prüfe auf Pending-Match
                    if self._pending and not self._pending.slot_index:
                        # Kein spezifischer Slot angegeben → match auf jeden neuen Slot
                        logger.info(f"Pending match: slot {slot_index} has spool")
                        await self._send_filament_setting(ams_id, tray_id, self._pending.filament_data)
                        self._pending.timer.cancel()
                        self._pending = None
                
                elif self._pending and self._pending.slot_index == slot_index:
                    # Slot ist leer, aber wir warten auf diesen Slot
                    pass
        
        # Externe Spule (vt_tray)
        if vt_tray.get("tray_type"):
            slots.append({
                "slot_index": "255-254",
                "slot_name": "External Tray",
                "tray_info_idx": vt_tray.get("tray_info_idx", ""),
                "tray_type": vt_tray.get("tray_type", ""),
                "tray_color": vt_tray.get("tray_color", ""),
                "nozzle_temp_min": vt_tray.get("nozzle_temp_min"),
                "nozzle_temp_max": vt_tray.get("nozzle_temp_max"),
                "present": True,
            })
            
            # Prüfe auf Pending-Match für externe Spule
            if self._pending and self._pending.slot_index == "255-254":
                logger.info("Pending match: external tray has spool")
                await self._send_filament_setting(255, 254, self._pending.filament_data)
                self._pending.timer.cancel()
                self._pending = None
        
        # Slots an System melden
        if slots:
            self.emit({
                "event_type": "slots_update",
                "slots": slots,
            })

    async def _send_filament_setting(self, ams_id: int, tray_id: int, filament_data: dict) -> None:
        """Sendet Filament-Einstellungen an den Drucker."""
        if not self._mqtt_client:
            logger.error("Cannot send filament setting: not connected")
            return
        
        # Farbe aus filament_data
        color = filament_data.get("color", "")
        if len(color) == 6:
            color = color + "FF"  # Alpha-Kanal hinzufügen
        elif len(color) != 8:
            color = "FFFFFFFF"  # Default: weiß
        
        # Temperatur
        min_temp = filament_data.get("nozzle_temp_min", 190)
        max_temp = filament_data.get("nozzle_temp_max", 230)
        
        # Filament-Typ (z.B. "PLA", "PETG")
        tray_type = filament_data.get("material_type", "PLA")
        
        # Bambu Filament-ID (mapping oder aus filament_data)
        tray_info_idx = filament_data.get("tray_info_idx", "GFL99")
        
        command = {
            "print": {
                "sequence_id": "0",
                "command": "ams_filament_setting",
                "ams_id": ams_id if ams_id < 200 else 255,
                "tray_id": tray_id if tray_id < 200 else 254,
                "tray_color": color,
                "nozzle_temp_min": min_temp,
                "nozzle_temp_max": max_temp,
                "tray_type": tray_type,
                "tray_info_idx": tray_info_idx,
            }
        }
        
        try:
            topic = f"device/{self._serial}/request"
            await self._mqtt_client.publish(topic, json.dumps(command))
            logger.info(f"Sent filament setting to printer {self.printer_id}: slot {ams_id}-{tray_id}")
        except Exception as e:
            logger.error(f"Failed to send filament setting: {e}")

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

    def health(self) -> dict[str, Any]:
        return {
            "driver_key": self.driver_key,
            "printer_id": self.printer_id,
            "running": self._running,
            "connected": self._connected,
            "pending": self._pending is not None,
        }
