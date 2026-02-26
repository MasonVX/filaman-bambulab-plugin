# FilaMan Bambu Lab Plugin

This repository contains the official Bambu Lab printer driver plugin for FilaMan. It also serves as the reference implementation for developers who want to create their own printer driver plugins for the FilaMan system.

## 1. Introduction

FilaMan is a comprehensive filament management system for 3D printing. It helps users track their spools, usage, and printer status in a centralized dashboard.

Plugins in FilaMan provide the integration layer between the core system and various 3D printer hardware. This Bambu Lab plugin enables FilaMan to communicate with Bambu Lab printers via MQTT, retrieve AMS (Automatic Material System) status, and manage filament assignments.

For more information about the core system, visit the [FilaMan System repository](https://github.com/Fire-Devils/filaman-system).

## 2. Plugin Architecture Overview

Plugins are modular Python packages that live in the `backend/app/plugins/{driver_key}/` directory of the FilaMan installation. The `PluginManager` in the core system is responsible for discovering, loading, and managing these plugins.

Key architectural points:
- Each plugin is a standalone Python package.
- Plugins are separate from the core repository and can be deployed independently.
- The system uses a manifest file (`plugin.json`) to understand plugin capabilities and configuration requirements.
- Drivers communicate with the core system using an event-based system.

## 3. File Structure

A typical FilaMan printer plugin follows this structure:

```
bambulab/
├── __init__.py          # Package initialization, exports the Driver class
├── driver.py            # Main driver implementation (inherits from BaseDriver)
├── plugin.json          # Plugin manifest (metadata, config schema, params)
└── bambu_filaments.json # Optional data file (e.g., for dropdown options)
```

## 4. plugin.json — Full Reference

The `plugin.json` file defines the plugin's identity, its configuration requirements, and any printer-specific parameters it needs.

### 4.1 Core Metadata

| Field | Type | Required | Description |
|---|---|---|---|
| `plugin_key` | string | yes | Unique identifier for the plugin. Must match the directory name. |
| `name` | string | yes | Human-readable name shown in the UI. |
| `driver_key` | string | yes | Identifier used for database references. |
| `plugin_type` | string | yes | Must be `"driver"` for printer plugins. |
| `version` | string | yes | Semantic version of the plugin. |
| `description` | string | yes | Brief description of what the plugin does. |
| `author` | string | yes | Name of the plugin author. |
| `homepage` | string | no | URL to the plugin's project page. |
| `dependencies` | string[] | no | List of Python package dependencies (pip format). |

### 4.2 config_schema

The `config_schema` uses the JSON Schema format to define the fields required to configure a printer instance. These fields are rendered as a form in the FilaMan admin UI.

Example from the Bambu Lab plugin:
```json
"config_schema": {
  "type": "object",
  "properties": {
    "printer_model": {
      "type": "string",
      "title": "Printer Model",
      "enum": ["P1S", "P1P", "X1C", "X1E", "A1", "A1_MINI"],
      "default": "P1S"
    },
    "host": {
      "type": "string",
      "title": "IP/Hostname"
    },
    "serial": {
      "type": "string",
      "title": "Serial Number"
    },
    "access_code": {
      "type": "string",
      "title": "Access Code"
    }
  },
  "required": ["printer_model", "host", "serial", "access_code"]
}
```

### 4.3 capabilities

Capabilities inform the core system about what features the printer supports.

| Field | Type | Description |
|---|---|---|
| `has_slots` | bool | Whether the printer has material slots (e.g., AMS, MMU). |
| `has_rfid` | bool | Whether the printer supports RFID spool identification. |
| `has_ams` | bool | Whether the printer has an automatic material system. |
| `supports_auto_match` | bool | Whether the driver can automatically assign spools to slots. |

### 4.4 printer_params — Printer-Specific Fields

This is the most important section for driver developers. Plugins can define per-printer calibration and configuration fields that are stored independently for each printer instance. This allows users to maintain different calibration values for the same filament across multiple printers.

#### Concept

When a plugin defines `printer_params`, the system automatically creates `SystemExtraField` entries in the database. These fields appear in the filament/spool detail pages, grouped by printer. The `source` column on each `SystemExtraField` is set to the plugin's `driver_key`, establishing ownership. Field names, labels, and types are **not editable by users** — they are always controlled by the plugin definition.

#### target_types

An array specifying where parameters are stored:

| Target Type | Description |
|---|---|
| `filament_printer_param` | Parameters at the filament level, shared across all spools of that filament |
| `spool_printer_param` | Parameters at the spool level, can override filament-level values per individual spool |

**Fallback logic**: When the system needs a parameter value, it checks the spool-level first. If the spool-level value is empty or not set, it falls through to the filament-level value. This allows users to set defaults at the filament level and override them for specific spools when needed.

#### fields

Array of field definitions:

| Property | Type | Required | Description |
|---|---|---|---|
| `key` | string | yes | Unique parameter key. **Must** be prefixed with your driver name (e.g., `bambu_`, `klipper_`). |
| `label` | string | yes | Human-readable label displayed in the UI. |
| `field_type` | string | yes | One of: `text`, `number`, `dropdown`, `checkbox`. |
| `options_file` | string | no | Path to a JSON file in the plugin directory for dropdown options. |
| `options` | string[] | no | Static list of dropdown options (alternative to `options_file`). |

#### options_file Format

The JSON file must be an object where keys are the internal values and values are the display labels. The system renders them as `"KEY \u2014 Label"` strings in the dropdown.

```json
{
  "GFL99": "PLA",
  "GFA01": "Bambu PLA Matte",
  "GFG99": "PETG"
}
```

This produces dropdown options like: `"GFA01 \u2014 Bambu PLA Matte"`, `"GFG99 \u2014 PETG"`, `"GFL99 \u2014 PLA"` (sorted alphabetically).

Keys starting with `_` are treated as comments and skipped. The value can also be a dict with a `name` property:

```json
{
  "_comment": "This is skipped",
  "GFL99": { "name": "PLA", "category": "basic" }
}
```

#### migration

The `migration` object supports renaming field keys when updating a plugin. Use `legacy_renames` to map old keys to new ones:

```json
"migration": {
  "legacy_renames": {
    "bambu_cali_id": "bambu_cali_idx",
    "bambu_k": "bambu_k_value",
    "bambu_max_volspeed": "bambu_max_volumetric_speed"
  }
}
```

On startup, the system will:
1. Rename existing `param_key` values in the `filament_printer_params` and `spool_printer_params` tables.
2. Remove legacy `SystemExtraField` definitions that no longer match current field keys.
3. Clean up fields with outdated `target_type` values.

#### Full Example

```json
"printer_params": {
  "target_types": ["filament_printer_param", "spool_printer_param"],
  "fields": [
    {"key": "bambu_idx", "label": "Bambu Material Index", "field_type": "dropdown", "options_file": "bambu_filaments.json"},
    {"key": "bambu_tray_idx", "label": "Tray Info Index", "field_type": "text"},
    {"key": "bambu_setting_id", "label": "Setting ID", "field_type": "text"},
    {"key": "bambu_k_value", "label": "K Value", "field_type": "number"},
    {"key": "bambu_flow_ratio", "label": "Flow Ratio", "field_type": "number"},
    {"key": "bambu_bed_temp", "label": "Bed Temperature", "field_type": "number"},
    {"key": "bambu_nozzle_temp_min", "label": "Nozzle Temp Min", "field_type": "number"},
    {"key": "bambu_nozzle_temp_max", "label": "Nozzle Temp Max", "field_type": "number"}
  ],
  "migration": {
    "legacy_renames": {
      "bambu_cali_id": "bambu_cali_idx",
      "bambu_k": "bambu_k_value"
    }
  }
}
```

## 5. BaseDriver — The Driver Interface

All drivers must inherit from `app.plugins.base.BaseDriver`. The base class provides instance variables, abstract methods, and a built-in debug logging system.

```python
from abc import ABC, abstractmethod
from collections import deque
from typing import Any, Callable

class BaseDriver(ABC):
    driver_key: str = ""  # Class variable — must match your plugin_key

    def __init__(self, printer_id: int, config: dict[str, Any], emitter: Callable):
        self.printer_id = printer_id   # Database ID of the printer
        self.config = config            # User-provided config (from config_schema)
        self.emit = emitter             # Callback to send events to FilaMan core
        self._running = False
        self._debug_log = deque(maxlen=500)  # Ring buffer for protocol debugging

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    def health(self) -> dict[str, Any]: ...
    def validate_config(self) -> None: ...
    def log_debug(self, direction: str, topic: str, payload: Any) -> None: ...
    def get_debug_log(self, since_ts: str | None = None) -> list[dict]: ...
    def clear_debug_log(self) -> None: ...
```

### Method Reference

| Method | Required | Description |
|---|---|---|
| `start()` | yes | Called when a printer is activated. Initialize connections, start background tasks. Set `self._running = True`. Must be async. |
| `stop()` | yes | Called on deactivation or shutdown. Disconnect, cancel tasks, clean up. Set `self._running = False`. Must be async. |
| `health()` | recommended | Returns a status dict shown in the admin dashboard. Must include at minimum `driver_key`, `printer_id`, `running`. Add any driver-specific info (connection state, slot count, etc.). |
| `validate_config()` | optional | Called before `start()`. Raise `ValueError` with a descriptive message if the config is invalid. |
| `log_debug(direction, topic, payload)` | optional | Stores a protocol message in the debug ring buffer (500 entries max). Use `direction` values like `"in"`, `"out"`, `"event"`. Entries are viewable in the admin UI. |
| `get_debug_log(since_ts)` | built-in | Returns debug log entries, optionally filtered by ISO timestamp. |
| `clear_debug_log()` | built-in | Clears the debug ring buffer. |

## 6. Events System

Drivers communicate state changes to the core system by calling `self.emit(event_dict)`.

### 6.1 slots_update Event

This is the primary event type. Sent when material slot or tray information changes.

```python
self.emit({
    "event_type": "slots_update",
    "slots": [
        {
            "slot_index": "0-0",           # Unique slot ID: "{unit}-{tray}"
            "slot_name": "AMS 0 - Slot 1",  # Human-readable display name
            "tray_info_idx": "GFL99",       # Material index code (if applicable)
            "tray_type": "PLA",             # Material type string
            "tray_color": "FF0000",         # Hex color without # (6 chars)
            "nozzle_temp_min": 190,          # Min nozzle temperature (optional)
            "nozzle_temp_max": 230,          # Max nozzle temperature (optional)
            "present": True,                 # Whether a spool is physically present
        }
    ],
    "ams_info": {                            # Optional summary metadata
        "ams_count": 1,
        "ams_type": "AMS",
        "slot_count": 4,
        "external_spool": True,
        "ams_units": [
            {"ams_id": 0, "humidity": 25, "temp": 30.5, "tray_count": 4}
        ]
    }
})
```

**slot_index convention**: Use the `"{unit}-{tray}"` string format. The system converts this to a numeric `slot_no` using the following formula:
- Normal slots: `unit * 4 + tray` (e.g., `"0-0"` = 0, `"0-3"` = 3, `"1-0"` = 4)
- External/special slots (unit >= 200): `1000 + tray` (e.g., `"255-254"` = 1254)

The `ams_info` object is stored in the printer's `custom_fields.slot_summary` for dashboard display.

## 7. Printer Params API

When a plugin defines `printer_params`, the system automatically provides these REST API endpoints:

### CRUD Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/v1/filaments/{id}/printer-params?printer_id={pid}` | Get filament-level params (optionally filtered by printer) |
| `PUT` | `/api/v1/filaments/{id}/printer-params/{pid}` | Bulk upsert filament-level params |
| `DELETE` | `/api/v1/filaments/{id}/printer-params/{pid}?param_key={key}` | Delete params (all or single key) |
| `GET` | `/api/v1/spools/{id}/printer-params?printer_id={pid}` | Get spool-level params |
| `PUT` | `/api/v1/spools/{id}/printer-params/{pid}` | Bulk upsert spool-level params |
| `DELETE` | `/api/v1/spools/{id}/printer-params/{pid}?param_key={key}` | Delete params (all or single key) |

### Import/Export Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/v1/printers/{id}/params/export` | Export all printer params as JSON |
| `POST` | `/api/v1/printers/{id}/params/import` | Import printer params from JSON |

### Key Validation

The `PUT` endpoints validate all `param_key` values against the plugin's field definitions in `plugin.json`. If an unknown key is submitted, the API returns HTTP 400 with:

```json
{
  "code": "invalid_param_keys",
  "message": "Unknown param_key(s): invalid_key",
  "valid_keys": ["bambu_bed_temp", "bambu_flow_ratio", "bambu_idx", "..."]
}
```

## 8. Data Enrichment

When FilaMan needs to send filament data to a printer (e.g., assigning a spool to a tray), it enriches the base filament data with printer-specific parameters using the `PluginManager.enrich_filament_data()` method.

**Merge order** (later values override earlier ones):
1. Base filament data (material type, color, etc.)
2. Filament-level printer params for this specific printer
3. Spool-level printer params for this specific printer (highest priority)

Only non-empty values are applied. The enriched dict is passed to your driver's methods (e.g., `assign_pending_spool()`).

## 9. Spoolman Migration

If users migrated from Spoolman, legacy filament data may exist in `custom_fields.spoolman_extra` (e.g., `spoolman_extra.bambu_idx`, `spoolman_extra.nozzle_temperature`). The `PluginManager` handles this automatically:

1. Extracts `bambu_*` fields from `custom_fields` and `spoolman_extra`.
2. Applies `legacy_renames` from `plugin.json` to map old field names to new ones.
3. Creates `FilamentPrinterParam` and `SpoolPrinterParam` entries for all printers of the same driver.
4. Removes the migrated keys from `custom_fields` to avoid duplication.

The migration is **idempotent** — it runs on every printer start but skips entities that already have printer params.

## 10. Lifecycle & Hooks

### Plugin Discovery

The system loads `plugin.json` from `backend/app/plugins/{driver_key}/` and dynamically imports the `Driver` class from `driver.py`.

### Printer Start Sequence (`start_printer()`)

When a printer is activated, the following steps run in order:

1. **Load driver class** from `{driver_key}/driver.py`.
2. **`_ensure_plugin_extra_fields()`** — Creates or updates `SystemExtraField` entries from the `printer_params` section. Idempotent, safe on every start.
3. **`_migrate_spoolman_bambu_fields()`** — One-time migration of legacy Spoolman data into `printer_params` tables.
4. **`_copy_params_to_new_printer()`** — If this is a new printer with no params yet, copies all existing params from another printer of the same driver (including soft-deleted printers as fallback source).
5. **`driver.validate_config()`** — Validates the user-provided configuration.
6. **`driver.start()`** — Initializes the connection.

### Runtime

The driver emits events via `self.emit()`. The `PluginManager` processes these events asynchronously (e.g., upserting `PrinterSlot` and `PrinterSlotAssignment` records).

### Printer Stop (`stop_printer()`)

Calls `driver.stop()` to clean up connections and background tasks.

### Printer Deletion

When a user deletes a printer, a confirmation dialog asks whether to also delete the printer-specific parameter data. If the user chooses to keep the data, it remains in the database and can be copied to a new printer of the same driver type.

## 11. Creating a New Plugin — Step by Step

### 1. Create the plugin directory

```bash
mkdir -p backend/app/plugins/myprinter
```

### 2. Create `__init__.py`

```python
from app.plugins.myprinter.driver import Driver

__all__ = ["Driver"]
```

### 3. Create `plugin.json`

```json
{
  "plugin_key": "myprinter",
  "name": "My Printer",
  "driver_key": "myprinter",
  "plugin_type": "driver",
  "version": "1.0.0",
  "description": "Driver for My Printer via REST API.",
  "author": "Your Name",
  "dependencies": ["httpx>=0.25.0"],
  "config_schema": {
    "type": "object",
    "properties": {
      "host": {
        "type": "string",
        "title": "Printer IP Address"
      },
      "api_key": {
        "type": "string",
        "title": "API Key"
      }
    },
    "required": ["host", "api_key"]
  },
  "capabilities": {
    "has_slots": false,
    "has_rfid": false,
    "has_ams": false,
    "supports_auto_match": false
  },
  "printer_params": {
    "target_types": ["filament_printer_param"],
    "fields": [
      {"key": "myprinter_profile_id", "label": "Filament Profile", "field_type": "text"},
      {"key": "myprinter_flow_rate", "label": "Flow Rate %", "field_type": "number"}
    ]
  }
}
```

### 4. Implement `driver.py`

```python
import asyncio
import logging
from typing import Any, Callable

from app.plugins.base import BaseDriver

logger = logging.getLogger(__name__)


class Driver(BaseDriver):
    driver_key = "myprinter"

    def __init__(self, printer_id: int, config: dict[str, Any], emitter: Callable):
        super().__init__(printer_id, config, emitter)
        self._host = config.get("host", "")
        self._api_key = config.get("api_key", "")
        self._poll_task: asyncio.Task | None = None

    def validate_config(self) -> None:
        if not self._host:
            raise ValueError("Printer IP address is required")
        if not self._api_key:
            raise ValueError("API key is required")

    async def start(self) -> None:
        self._running = True
        # Start a background polling loop
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(f"MyPrinter driver started for printer {self.printer_id}")

    async def stop(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
        logger.info(f"MyPrinter driver stopped for printer {self.printer_id}")

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                # Poll your printer API here
                # data = await self._fetch_status()
                # self.emit({"event_type": "slots_update", "slots": [...]})
                pass
            except Exception as e:
                logger.error(f"Poll error: {e}")
            await asyncio.sleep(30)

    def health(self) -> dict[str, Any]:
        return {
            "driver_key": self.driver_key,
            "printer_id": self.printer_id,
            "running": self._running,
            "host": self._host,
        }
```

### 5. Test

Start FilaMan, create a new printer in the admin UI, select your `driver_key`, and fill in the config fields. The system will instantiate your driver and call `start()`.

## 12. Best Practices

- **Key prefixing**: Always prefix parameter keys with your driver name (e.g., `klipper_`, `octo_`, `bambu_`) to avoid collisions with other plugins.
- **Async I/O**: Use asynchronous operations for all network calls. If a library is blocking (like paho-mqtt), run it in a separate thread and bridge back to asyncio (see the Bambu driver for an example).
- **Debug logging**: Call `self.log_debug(direction, topic, payload)` for every protocol message. Use `"in"` for incoming, `"out"` for outgoing, and `"event"` for internal events. This enables protocol-level debugging in the admin UI.
- **Reconnection**: Implement automatic reconnection with exponential backoff. Printers go offline — your driver should recover gracefully without user intervention.
- **Config validation**: Use `validate_config()` to catch missing or invalid settings early, before attempting a connection.
- **Health reporting**: Return detailed status in `health()` — connection state, slot count, error messages. This is the primary diagnostic tool for users.
- **Options files**: Use `options_file` for large dropdown lists (50+ items), static `options` for small ones.
- **Running flag**: Always set `self._running = True` at the start of `start()` and `self._running = False` at the start of `stop()`.
- **Thread safety**: If your driver uses background threads (e.g., for blocking libraries), use `loop.call_soon_threadsafe()` to bridge events back to the asyncio event loop.

## 13. Example: Bambu Lab Plugin

The Bambu Lab plugin included in this repository provides a complete, production-ready reference implementation. It demonstrates:

- **MQTT communication** via the `bambulabs_api` library with paho-mqtt running in a background thread.
- **AMS tray detection** with complex payload parsing for both standard AMS and AMS Lite models.
- **Dynamic filament identification** using a JSON-based material code lookup table (`bambu_filaments.json`).
- **10 printer-specific parameters** covering material index, calibration values, temperatures, and flow settings.
- **Automatic spool-to-tray matching** with configurable timeout for pending assignments.
- **Thread-safe event bridging** from paho-mqtt callbacks to asyncio using `loop.call_soon_threadsafe()`.
- **Reconnection handling** via paho's built-in reconnect with configurable backoff intervals.
