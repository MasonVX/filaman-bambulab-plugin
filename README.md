# Bambu Lab Plugin for FilaMan

A FilaMan printer driver plugin that connects to Bambu Lab printers via MQTT, reads AMS slot data in real-time and enables automatic filament-to-tray assignment.

## Features

- MQTT communication via `bambulabs_api`
- AMS and AMS Lite support (auto-detection by printer model)
- External tray support
- RFID spool identification
- Optional automatic import and estimated-weight synchronization of Bambu RFID spools
- Dedicated responsive AMS and spool overview page
- Optional color-specific Bambu product images loaded directly from Bambu's CDN, with persistent URL metadata
- Read-only mode for status and inventory synchronization without printer changes
- Automatic spool-to-tray matching with a 60-second default timeout (caller-overridable)
- Automatic FilaMan location updates after successful AMS assignments
- 10 printer-specific parameters (material index, calibration, temperatures, flow)
- Protocol-level debug logging (viewable in admin UI)
- Auto-reconnect with configurable interval
- Automatic migration of legacy Bambu printer-parameter keys

## Supported Printers

P1S, P1P, X1 Carbon, X1E, A1, A1 Mini, H2C, H2D, H2S, P2S

## Installation

The recommended installation method is to upload the release ZIP through FilaMan's plugin management page. Restart FilaMan if the interface asks you to do so.

For a manual installation, copy the complete `bambulab/` folder into the FilaMan plugins directory and restart FilaMan. Do not copy only the individual files from inside that folder.

## Code Structure

The Python implementation is split by responsibility while keeping `driver.py` as FilaMan's required entry point:

- `driver.py` coordinates lifecycle, MQTT callbacks, printer commands and the public driver API.
- `slots.py` normalizes identifiers and parses AMS, AMS Lite, AMS HT and external tray payloads.
- `spool_sync.py` owns safe filament matching, RFID spool upserts, remaining weight and managed locations.
- `catalog.py` resolves and stores Bambu product image metadata without downloading image binaries into FilaMan.
- `catalog_enrichment.py` coordinates event-driven and periodic inventory image scans across driver instances.
- `state.py` contains the small runtime state objects shared by the driver modules.

Every module, class and function contains a short docstring explaining its role. The separation is intentionally internal: FilaMan still loads `bambulab.driver.Driver`, and the plugin manifest and runtime API remain unchanged.

## Configuration

Create a new printer in the FilaMan admin panel and select **Bambu Lab** as driver. The following fields are required:

| Field | Description |
|-------|-------------|
| Printer Model | Your Bambu Lab model (determines AMS data handling) |
| IP/Hostname | IP address or hostname of the printer |
| Serial Number | Printer serial number |
| Access Code | Printer access code |
| Read-only Mode | Prevents all state-changing MQTT commands (default: off) |
| Automatically Import Bambu RFID Spools | Adds unknown RFID spools and synchronizes their estimated remaining weight (default: off) |
| Catalog Images (Bambu Lab) | Resolves color-specific images through Bambu's EU Store Search API, with a product-page fallback, and stores only their URLs (default: off) |
| Reconnect Interval | Minutes between reconnection attempts (default: 5) |

> **Important:** Sending filament settings (spool assignment) requires the printer to be in **LAN-only mode** with **Developer Mode** enabled. Without these settings, only reading AMS status is possible. This is a Bambu Lab firmware restriction.

## Bambu RFID Spool Import

Bambu RFID trays report two different identifiers:

- `tray_uuid` identifies the logical Bambu spool and is its sole Bambu identity. It is normalized to 32 uppercase hexadecimal characters and stored as `external_id=bambulab:<tray_uuid>` for duplicate prevention.
- `tag_uid` identifies only one physical RFID chip. When present, the first UID seen via MQTT is retained as optional metadata in **Bambu RFID Tag 1**. It is neither required for importing nor used to match a spool.

FilaMan's built-in `rfid_uid` field is never used or overwritten by this plugin and remains available for custom RFID tags.

At driver startup, the plugin ensures that the system spool extra fields **Bambu RFID Tag 1** (`bambu_rfid_tag_1`) and **Bambu RFID Tag 2** (`bambu_rfid_tag_2`) exist. The first field can receive the MQTT `tag_uid`; the second remains available for another reader or integration.

Automatic import never creates manufacturers, colors, or filaments. A new spool is only created when the plugin finds one safe existing filament match, first by its `bambu_tray_idx` printer parameter and then by Bambu manufacturer, material, subtype, and color. Duplicate FilamentDB records for the same Bambu product are collapsed deterministically, preferring the canonical record carrying a five-digit Bambu product code such as `(11101)`. Matches across genuinely different product codes remain blocked and are logged. Repeated MQTT messages for the same `tray_uuid` do not create duplicate spools.

For valid Bambu RFID spools, MQTT reports the nominal filament weight in `tray_weight` and the estimated percentage in `remain`. The plugin calculates `tray_weight × remain / 100` and updates `remaining_weight_g` for both newly created and already existing spools. Missing or invalid estimates (`tray_weight = 0`, `remain = -1`) never overwrite an existing weight.

An all-zero `tray_uuid` placeholder from a custom or non-RFID spool is ignored. A missing or all-zero `tag_uid` does not prevent importing a spool with a valid `tray_uuid`. The slot parser supports both the legacy `vt_tray` object and the newer `vir_slot` list used for multiple external trays, as well as AMS HT unit IDs.

Read-only mode and automatic import are independent: enabling both lets the plugin read the AMS and update FilaMan while preventing changes to the printer.

After a successful writable spool-to-tray assignment, the plugin creates or reuses a FilaMan location for that printer and AMS slot and moves the assigned spool there. Read-only mode does not perform an assignment and therefore does not change the spool location.

## Spool Gallery, AMS Overview and Catalog Images

The plugin registers **Bambu Lab** as a navigation page at `/plugin-page/bambulab` with two views:

- **All Spools** displays every FilaMan spool, regardless of manufacturer, with search, manufacturer/material filters, archived-spool visibility, remaining weight, status and RFID state. Spools without a catalog image use a colorized local illustration.
- **AMS Live** keeps the existing printer-centric view of active Bambu printers, AMS slots, linked FilaMan spools, RFID state, estimated remaining percentage and synchronized remaining weight.

FilaMan serves third-party `page.html` files directly rather than through its
built-in Astro layout. The page therefore remains standalone, uses FilaMan's
saved brand/light/dark theme and provides a translated back button that returns
to the previous FilaMan page (or the dashboard when no internal referrer is
available). It reads the signed-in user's language from `/api/v1/me` (with
FilaMan's `localStorage.lang` as the early-load fallback) and includes complete
English and German UI dictionaries following FilaMan's `data-i18n` convention.

When **Catalog Images (Bambu Lab)** is enabled, genuine Bambu RFID trays can trigger a low-frequency metadata lookup. The plugin also listens for FilaMan's standard `spools_changed` and `filaments_changed` events, so Bambu inventory created by independent integrations such as FilaScan or Bambu Cloud Connect is enriched immediately. Bursts of events are debounced into one scan, and multiple Bambu printer drivers share one process-wide enrichment worker. A startup scan and a scan every six hours remain as a fallback for events emitted while the plugin was offline. Opening **All Spools** or pressing **Refresh** now also requests an explicit scan through FilaMan's primary driver worker. This provides immediate reconciliation when an import event was emitted in another web-worker process. The scan is best-effort: an offline printer, unavailable primary driver or image-service failure never blocks the all-spool inventory, and AMS refresh falls back to the cached offline health state. Only Bambu/Bambu Lab filaments with at least one physical FilaMan spool are scanned, deliberately excluding unused FilamentDB catalog entries. The resolver searches Bambu's EU Store API by the filament's five-digit product code (for example `11101`) and accepts exactly one highlighted product result. It uses the result's `mediaFiles[0]` product image rather than the `colorPalette` swatch, and links directly to the selected `highlightProductSkuId`. Product-page color data remains a fallback if the search API is unavailable. A custom/non-RFID AMS tray never triggers an AMS-based lookup.

Only URL metadata is persisted in the matching shared **filament's** `custom_fields`: `filament_image_url`, `filament_image_source_url`, `filament_image_provider` and `filament_image_checked_at`. Compatibility copies remain in the existing `bambu_shop_*` fields. This generic field scheme lets later manufacturer resolvers feed the same gallery without changing its UI. Every physical spool made from that filament automatically reuses the metadata. For known new Bambu families such as `GFA19` / PLA Pure, the plugin can supply the official product-family URL when an independently created filament does not yet have a `shop_url`. Current storefront color swatches are preferred over generic JSON-LD product images.

FilaScan's raw `bambu_color_code` field and Spoolman imports' `article_number`
field are accepted as the same product-code identity as the driver's
`bambu_product_code`. Only a standalone five-digit code is used. If an independently imported
filament still has FilaScan's exact technical fallback name, the Bambu plugin
safely normalizes it to the FilamentDB-style display name during enrichment.
For PLA Pure White `17100`, the previous palette image is automatically
re-resolved through the Store API. A verified Bambu CDN swatch URL remains only
as an offline fallback.

Image binaries are not downloaded into FilaMan. Each browser loads an enabled image directly from the manufacturer's CDN when it is displayed. A browser may reuse its own HTTP cache, but the plugin does not provide or guarantee a local binary-image cache. Saved URL metadata is refreshed after 30 days. Failed lookup attempts are suppressed for up to one day in the running plugin process. If an image is unavailable, both views fall back to the filament or MQTT tray color.

## A Note to Bambu Lab

Dear Bambu Lab,

This independent, non-commercial community plugin optionally retrieves official product-image URLs from the Bambu Lab EU Store service and displays the corresponding images directly from Bambu Lab's CDN. Its purpose is to help users visually identify physical Bambu Lab filament spools in their own FilaMan inventory.

When an exact product match is available, the plugin also links directly to the official Bambu Lab product page and selected SKU, allowing the user to view the product or order another spool. The plugin adds no affiliate, referral, advertising, or tracking parameters to these links, and its authors receive no commission or other commercial benefit from them.

The image feature is disabled by default. FilaMan stores only the image and product-page URLs; the plugin package does not contain or redistribute Bambu Lab image files. Each user's browser retrieves enabled images directly from Bambu Lab's CDN. This project is not affiliated with or endorsed by Bambu Lab, and this note does not claim ownership of or a license to Bambu Lab content. If Bambu Lab prefers a different integration method or requests removal of this optional feature, please contact the project maintainers.

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

## AI-Assisted Development

This plugin has been developed with the assistance of generative AI tools, including OpenAI Codex. AI assistance has been used for parts of the implementation, refactoring, testing, debugging and documentation. The project is not presented as exclusively human-written; its human maintainers remain responsible for reviewing, accepting and publishing all changes.

## License

See the [FilaMan](https://github.com/Fire-Devils/FilaMan) project for license information.
