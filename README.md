# Bambu Lab Plugin for FilaMan

A FilaMan printer driver plugin that connects to Bambu Lab printers via MQTT, reads AMS slot data in real-time and enables automatic filament-to-tray assignment.

## Features

- MQTT communication via `bambulabs_api`
- AMS and AMS Lite support (auto-detection by printer model)
- External tray support
- RFID spool identification
- Automatic spool-to-tray matching with configurable timeout
- 10 printer-specific parameters (material index, calibration, temperatures, flow)
- Protocol-level debug logging (viewable in admin UI)
- Auto-reconnect with configurable interval
- Spoolman legacy data migration (automatic)

## Supported Printers

P1S, P1P, X1 Carbon, X1E, A1, A1 Mini, H2C, H2D, H2S, P2S

## Installation

Copy the `bambulab/` folder into your FilaMan plugins directory and restart FilaMan.

## Configuration

Create a new printer in the FilaMan admin panel and select **Bambu Lab** as driver. The following fields are required:

| Field | Description |
|-------|-------------|
| Printer Model | Your Bambu Lab model (determines AMS data handling) |
| IP/Hostname | IP address or hostname of the printer |
| Serial Number | Printer serial number |
| Access Code | Printer access code |
| Reconnect Interval | Minutes between reconnection attempts (default: 5) |

> **Important:** Sending filament settings (spool assignment) requires the printer to be in **LAN-only mode** with **Developer Mode** enabled. Without these settings, only reading AMS status is possible. This is a Bambu Lab firmware restriction.

## Printer Parameters

The plugin registers the following per-printer parameters for filaments and spools:

| Parameter | Type | Description |
|-----------|------|-------------|
| Bambu Material Index | Dropdown | Material code from `bambu_filaments.json` |
| Tray Info Index | Text | Tray info index string |
| Setting ID | Text | Bambu setting identifier |
| Calibration Index | Text | Calibration index |
| K Value | Number | Pressure advance K value |
| Flow Ratio | Number | Flow ratio multiplier |
| Bed Temperature | Number | Bed temperature (°C) |
| Nozzle Temp Min | Number | Minimum nozzle temperature (°C) |
| Nozzle Temp Max | Number | Maximum nozzle temperature (°C) |
| Max Volumetric Speed | Number | Maximum volumetric speed (mm³/s) |

Parameters can be set at filament level (shared across spools) or overridden per individual spool.

## License

See the [FilaMan](https://github.com/Fire-Devils/FilaMan) project for license information.
