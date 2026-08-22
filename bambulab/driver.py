import asyncio
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import flag_modified

from app.core.database import async_session_maker
from app.core.event_bus import event_bus
from app.models.filament import Color, Filament, FilamentColor, Manufacturer
from app.models.location import Location
from app.models.printer import Printer
from app.models.printer_params import FilamentPrinterParam
from app.models.spool import Spool, SpoolStatus
from app.models.system_extra_field import SystemExtraField
from app.plugins.base import BaseDriver
from app.services.spool_service import SpoolService

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60
DEFAULT_RECONNECT_INTERVAL = 5
AUTO_IMPORT_RETRY_SECONDS = 60
SHOP_IMAGE_CACHE_DAYS = 30
SHOP_IMAGE_ERROR_RETRY_SECONDS = 24 * 60 * 60
SHOP_IMAGE_SCHEDULE_SECONDS = 60
SHOP_PAGE_MEMORY_CACHE_SECONDS = 6 * 60 * 60
STORE_SEARCH_API_URL = (
    "https://eu-store-api.bambulab.com/mall-goods/product/globalSearchV2"
)
STORE_SEARCH_REGION = "EU"
STORE_SEARCH_RESOLVER_VERSION = 2
STORE_SEARCH_MAX_BYTES = 5 * 1024 * 1024
INVENTORY_IMAGE_SCAN_SECONDS = 6 * 60 * 60
INVENTORY_IMAGE_EVENT_DEBOUNCE_SECONDS = 0.5
INVENTORY_IMAGE_EVENTS = frozenset({"spools_changed", "filaments_changed"})
SHOP_PAGE_MAX_BYTES = 5 * 1024 * 1024
BAMBU_RFID_TAG_1_FIELD = "bambu_rfid_tag_1"
BAMBU_RFID_TAG_2_FIELD = "bambu_rfid_tag_2"
BAMBU_SHOP_IMAGE_URL_FIELD = "bambu_shop_image_url"
BAMBU_SHOP_SOURCE_URL_FIELD = "bambu_shop_source_url"
BAMBU_SHOP_IMAGE_CHECKED_AT_FIELD = "bambu_shop_image_checked_at"
BAMBU_PRODUCT_CODE_FIELD = "bambu_product_code"
BAMBU_IMAGE_RESOLVER_VERSION_FIELD = "bambu_image_resolver_version"
_BAMBU_PRODUCT_URLS_BY_MATERIAL_ID = {
    "GFA19": "https://eu.store.bambulab.com/products/pla-pure",
}
_BAMBU_PRODUCT_URLS_BY_FAMILY = {
    "pla pure": "https://eu.store.bambulab.com/products/pla-pure",
}
_BAMBU_PRODUCT_IMAGES_BY_CODE = {
    "17100": (
        "https://store.bblcdn.eu/s8/default/"
        "5939cb6c15514a7bb404738123d817c2/Pure_White.jpg"
    ),
}
FILAMENT_IMAGE_URL_FIELD = "filament_image_url"
FILAMENT_IMAGE_SOURCE_URL_FIELD = "filament_image_source_url"
FILAMENT_IMAGE_PROVIDER_FIELD = "filament_image_provider"
FILAMENT_IMAGE_CHECKED_AT_FIELD = "filament_image_checked_at"
BAMBU_EXTERNAL_ID_PREFIX = "bambulab:"
_HEX_IDENTIFIER_RE = re.compile(r"^[0-9A-F]+$")
_BAMBU_PRODUCT_CODE_RE = re.compile(r"(?<!\d)(\d{5})(?!\d)")
_BAMBU_SUBTYPE_ALIASES = (
    ("silk-plus", "silk-plus"),
    ("tough-plus", "tough-plus"),
    ("pla-aero", "aero"),
    ("petg-hf", "hf"),
    ("high-flow", "hf"),
    ("matte", "matte"),
    ("basic", "basic"),
    ("silk", "silk"),
    ("aero", "aero"),
    ("tough", "tough"),
    ("support", "support"),
    ("metal", "metal"),
    ("wood", "wood"),
    ("lite", "lite"),
    ("hf", "hf"),
    ("cf", "cf"),
    ("gf", "gf"),
)


class _JsonLdScriptParser(HTMLParser):
    """Collect JSON-LD script bodies without depending on an HTML package."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._collecting = False
        self._buffer: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attr_map = {str(key).lower(): value for key, value in attrs}
        if str(attr_map.get("type") or "").lower() == "application/ld+json":
            self._collecting = True
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._collecting:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._collecting:
            self.scripts.append("".join(self._buffer))
            self._collecting = False
            self._buffer = []


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

    # Image enrichment belongs to the Bambu integration as a whole, not to one
    # physical printer. The first running image-enabled driver owns each scan;
    # further drivers share these process-wide listener and worker tasks.
    _inventory_enrichment_instances: set["Driver"] = set()
    _inventory_enrichment_event: asyncio.Event | None = None
    _inventory_enrichment_listener_task: asyncio.Task | None = None
    _inventory_enrichment_worker_task: asyncio.Task | None = None

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
        self._read_only = bool(config.get("read_only", False))
        self._auto_import_spools = bool(config.get("auto_import_spools", False))
        self._resolve_shop_images = bool(config.get("resolve_shop_images", False))
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
        self._auto_import_lock: asyncio.Lock | None = None
        self._shop_image_lock: asyncio.Lock | None = None
        self._auto_import_last_attempt: dict[str, float] = {}
        self._shop_image_last_attempt: dict[int, float] = {}
        self._shop_slot_last_scheduled: dict[str, float] = {}
        self._shop_page_cache: dict[str, tuple[float, dict[str, str]]] = {}
        self._store_search_cache: dict[
            str, tuple[float, dict[str, str] | None]
        ] = {}
        self._slot_display_metadata: dict[str, dict[str, Any]] = {}
        self._inventory_enrichment_registered = False
        self._spool_ids_by_tray_uuid: dict[str, int] = {}
        self._slot_spool_ids: dict[str, int] = {}
        self._bambu_filaments = self._load_bambu_filaments()

    async def start(self) -> None:
        from bambulabs_api import Printer

        self._running = True
        self._loop = asyncio.get_running_loop()
        self._auto_import_lock = asyncio.Lock()
        self._shop_image_lock = asyncio.Lock()

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

        await self._ensure_rfid_extra_fields()
        if self._resolve_shop_images:
            await self._register_inventory_enrichment()

    async def stop(self) -> None:
        self._running = False
        if self._pending and self._pending.timer:
            self._pending.timer.cancel()
            self._pending = None
        await self._unregister_inventory_enrichment()
        if self._printer:
            try:
                self._printer.mqtt_client._client.disconnect()
            except Exception:
                pass
            self._printer.mqtt_stop()
            self._printer = None
        self._connected = False

    @staticmethod
    def _normalize_hex_identifier(value: Any, expected_length: int) -> str | None:
        """Normalize a Bambu identifier and reject empty/all-zero placeholders."""
        if value is None:
            return None
        normalized = re.sub(r"[^0-9A-Fa-f]", "", str(value)).upper()
        if (
            len(normalized) != expected_length
            or not _HEX_IDENTIFIER_RE.fullmatch(normalized)
            or set(normalized) == {"0"}
        ):
            return None
        return normalized

    @staticmethod
    def _normalize_tray_color(value: Any) -> str | None:
        normalized = re.sub(r"[^0-9A-Fa-f]", "", str(value or "")).upper()
        if len(normalized) not in (6, 8):
            return None
        return normalized[:6]

    @staticmethod
    def _normalize_product_text(value: Any) -> str:
        text = str(value or "").strip().lower().replace("+", "-plus")
        return re.sub(r"[^a-z0-9]+", "-", text).strip("-")

    @classmethod
    def _bambu_subtype(cls, value: Any) -> str | None:
        normalized = cls._normalize_product_text(value)
        if not normalized:
            return None
        for token, subtype in _BAMBU_SUBTYPE_ALIASES:
            if token in normalized:
                return subtype
        return None

    @staticmethod
    def _filamentdb_id(filament: Filament) -> int | None:
        custom_fields = filament.custom_fields or {}
        try:
            return int(custom_fields.get("filamentdb_id"))
        except (TypeError, ValueError):
            return None

    @classmethod
    def _canonical_candidate(cls, candidates: list[Filament]) -> Filament:
        """Choose deterministically among records known to describe one product."""
        return min(
            candidates,
            key=lambda item: (
                cls._filamentdb_id(item) is None,
                cls._filamentdb_id(item) or item.id,
                item.id,
            ),
        )

    @classmethod
    def _select_bambu_candidate(
        cls, candidates: list[Filament], slot: dict[str, Any]
    ) -> Filament | None:
        """Resolve duplicate FilamentDB records without guessing across products."""
        if len(candidates) <= 1:
            return candidates[0] if candidates else None

        slot_subtype = cls._bambu_subtype(slot.get("tray_sub_brands"))
        if slot_subtype:
            subtype_matches = [
                candidate
                for candidate in candidates
                if cls._bambu_subtype(candidate.material_subgroup) == slot_subtype
            ]
            if subtype_matches:
                candidates = subtype_matches
                if len(candidates) == 1:
                    return candidates[0]

        # FilamentDB currently contains both legacy, product-code-bearing Bambu
        # records and newer duplicates without the code.  Prefer the coded record
        # only when every coded candidate points to the same Bambu product.
        candidates_by_code: dict[str, list[Filament]] = {}
        for candidate in candidates:
            searchable = " ".join(
                filter(
                    None,
                    (candidate.designation, candidate.manufacturer_color_name),
                )
            )
            for code in set(_BAMBU_PRODUCT_CODE_RE.findall(searchable)):
                candidates_by_code.setdefault(code, []).append(candidate)
        if len(candidates_by_code) == 1:
            return cls._canonical_candidate(next(iter(candidates_by_code.values())))

        # Exact duplicate names without a product code are also safe to collapse.
        identities = {
            (
                cls._normalize_product_text(candidate.designation),
                cls._normalize_product_text(candidate.manufacturer_color_name),
                cls._bambu_subtype(candidate.material_subgroup),
            )
            for candidate in candidates
        }
        if len(identities) == 1:
            return cls._canonical_candidate(candidates)
        return None

    @staticmethod
    def _parse_slot_index(slot_index: str) -> tuple[int, int] | None:
        try:
            ams_id, tray_id = slot_index.split("-", 1)
            return int(ams_id), int(tray_id)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_positive_float(value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _parse_percentage(value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if 0 <= parsed <= 100 else None

    @classmethod
    def _estimated_remaining_weight(cls, slot: dict[str, Any]) -> float | None:
        tray_weight = cls._parse_positive_float(slot.get("tray_weight"))
        remain = cls._parse_percentage(slot.get("remain"))
        if tray_weight is None or remain is None:
            return None
        return tray_weight * remain / 100

    @staticmethod
    def _load_bambu_filaments() -> dict[str, str]:
        try:
            path = Path(__file__).with_name("bambu_filaments.json")
            data = json.loads(path.read_text(encoding="utf-8"))
            return {
                str(key).upper(): str(value)
                for key, value in data.items()
                if not str(key).startswith("_")
            }
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Could not load Bambu filament mapping: {e}")
            return {}

    @staticmethod
    def _bambu_product_code(*values: Any) -> str | None:
        for value in values:
            match = _BAMBU_PRODUCT_CODE_RE.search(str(value or ""))
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _allowed_shop_url(value: Any) -> str | None:
        """Accept only public Bambu store product pages used by FilamentDB."""
        try:
            parsed = urlparse(str(value or "").strip())
        except ValueError:
            return None
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (
            hostname == "store.bambulab.com"
            or hostname.endswith(".store.bambulab.com")
        ):
            return None
        # Variant IDs differ between regions and are unnecessary for resolving
        # the color code.  Dropping the query also improves page-cache reuse.
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

    @staticmethod
    def _allowed_shop_image_url(value: Any) -> str | None:
        try:
            parsed = urlparse(str(value or "").strip())
        except ValueError:
            return None
        hostname = (parsed.hostname or "").lower()
        allowed = (
            hostname in {"store.bblcdn.com", "store.bblcdn.eu", "cdn.shopify.com"}
            or hostname.endswith(".bblcdn.com")
            or hostname.endswith(".bblcdn.eu")
        )
        if parsed.scheme != "https" or not allowed:
            return None
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, ""))

    @classmethod
    def _optimized_shop_image_url(cls, value: Any) -> str | None:
        image_url = cls._allowed_shop_image_url(value)
        if not image_url:
            return None
        parsed = urlparse(image_url)
        hostname = (parsed.hostname or "").lower()
        if "__op__" in parsed.path or "bblcdn" not in hostname:
            return image_url
        optimized_path = (
            f"{parsed.path}__op__resize,m_lfit,w_640"
            "__op__format,f_auto__op__quality,q_80"
        )
        return urlunparse(
            (parsed.scheme, parsed.netloc, optimized_path, "", parsed.query, "")
        )

    @classmethod
    def _extract_shop_product_images(cls, html: str) -> dict[str, str]:
        """Return Bambu five-digit product code -> color image URL."""
        parser = _JsonLdScriptParser()
        parser.feed(html)
        images: dict[str, str] = {}

        # The current Bambu storefront exposes the actual color image in its
        # swatch markup and embedded product data. Prefer it over the generic
        # product image that some JSON-LD variants carry.
        swatch_pattern = re.compile(
            r'<li\b[^>]*\bvalue=["\'][^"\']*?(?P<code>\d{5})[^"\']*["\']'
            r'(?:(?!</li>).){0,2000}?<img\b[^>]*\bsrc=["\'](?P<url>https://[^"\']+)',
            re.IGNORECASE | re.DOTALL,
        )
        for match in swatch_pattern.finditer(html):
            image_url = cls._optimized_shop_image_url(match.group("url"))
            if image_url:
                images.setdefault(match.group("code"), image_url)

        embedded_product_data = html.replace(r'\"', '"')
        embedded_pattern = re.compile(
            r'"value"\s*:\s*"[^"]*?(?P<code>\d{5})[^"]*"'
            r'.{0,2000}?"colorUrl"\s*:\s*"(?P<url>https://[^"]+)',
            re.IGNORECASE | re.DOTALL,
        )
        for match in embedded_pattern.finditer(embedded_product_data):
            image_url = cls._optimized_shop_image_url(match.group("url"))
            if image_url:
                images.setdefault(match.group("code"), image_url)

        def visit(value: Any) -> None:
            if isinstance(value, list):
                for child in value:
                    visit(child)
                return
            if not isinstance(value, dict):
                return

            name = value.get("name")
            product_code = cls._bambu_product_code(name)
            image_value = value.get("image")
            if isinstance(image_value, list):
                image_value = image_value[0] if image_value else None
            elif isinstance(image_value, dict):
                image_value = image_value.get("url") or image_value.get("contentUrl")
            image_url = cls._optimized_shop_image_url(image_value)
            if product_code and image_url:
                images.setdefault(product_code, image_url)

            for key in ("hasVariant", "@graph", "itemListElement"):
                if key in value:
                    visit(value[key])

        for script in parser.scripts:
            try:
                visit(json.loads(script))
            except (json.JSONDecodeError, TypeError):
                continue
        return images

    @classmethod
    def _parse_store_search_product(
        cls,
        payload: Any,
        expected_product_url: str | None = None,
    ) -> dict[str, str] | None:
        """Return the unique highlighted product image from Bambu store search."""
        if not isinstance(payload, dict) or payload.get("code") != 1:
            return None
        page = (payload.get("data") or {}).get("page")
        if not isinstance(page, dict) or str(page.get("total")) != "1":
            return None
        records = page.get("records")
        if not isinstance(records, list) or len(records) != 1:
            return None
        record = records[0]
        if not isinstance(record, dict):
            return None
        sku_id = str(record.get("highlightProductSkuId") or "").strip()
        if not re.fullmatch(r"\d+", sku_id):
            return None

        seo_code = str(record.get("seoCode") or "").strip().casefold()
        if not re.fullmatch(r"[a-z0-9-]+", seo_code):
            return None
        product_family_url = cls._allowed_shop_url(
            f"https://eu.store.bambulab.com/de/products/{seo_code}"
        )
        if not product_family_url:
            return None
        expected_url = cls._allowed_shop_url(expected_product_url)
        if (
            expected_url
            and urlparse(expected_url).path.rstrip("/").split("/")[-1]
            != seo_code
        ):
            return None

        media_files = record.get("mediaFiles")
        if not isinstance(media_files, list):
            return None
        image_url = next(
            (
                optimized
                for value in media_files
                if (optimized := cls._optimized_shop_image_url(value))
            ),
            None,
        )
        if not image_url:
            return None
        return {
            "shop_image_url": image_url,
            "shop_source_url": f"{product_family_url}?id={sku_id}",
        }

    async def _fetch_store_search_image(
        self,
        product_code: str,
        expected_product_url: str | None = None,
    ) -> dict[str, str] | None:
        expected_url = self._allowed_shop_url(expected_product_url)
        expected_slug = (
            urlparse(expected_url).path.rstrip("/").split("/")[-1]
            if expected_url
            else ""
        )
        cache_key = f"{product_code}:{expected_slug}"
        cached = self._store_search_cache.get(cache_key)
        now = time.monotonic()
        if cached and now - cached[0] < SHOP_PAGE_MEMORY_CACHE_SECONDS:
            return dict(cached[1]) if cached[1] else None

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-BBL-STORE-REGION": STORE_SEARCH_REGION,
            "User-Agent": "FilaMan-BambuLab-Plugin/2.6 (store-search-image)",
        }
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(15.0),
            headers=headers,
        ) as client:
            response = await client.post(
                STORE_SEARCH_API_URL,
                json={"content": product_code, "current": 1, "size": 4},
            )
            response.raise_for_status()
            if len(response.content) > STORE_SEARCH_MAX_BYTES:
                raise ValueError("Bambu store search response exceeds the safety limit")
            result = self._parse_store_search_product(
                response.json(), expected_url
            )

        self._store_search_cache[cache_key] = (now, result)
        return dict(result) if result else None

    @classmethod
    def _catalog_product_url(
        cls, filament: Filament, custom_fields: dict[str, Any]
    ) -> str | None:
        material_id = str(custom_fields.get("bambu_material_id") or "").upper()
        if material_id in _BAMBU_PRODUCT_URLS_BY_MATERIAL_ID:
            return cls._allowed_shop_url(
                _BAMBU_PRODUCT_URLS_BY_MATERIAL_ID[material_id]
            )

        family_values = (
            custom_fields.get("bambu_detailed_filament_type"),
            f"{filament.material_type or ''} {filament.material_subgroup or ''}",
        )
        for value in family_values:
            normalized = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold())
            normalized = " ".join(normalized.split())
            if normalized in _BAMBU_PRODUCT_URLS_BY_FAMILY:
                return cls._allowed_shop_url(
                    _BAMBU_PRODUCT_URLS_BY_FAMILY[normalized]
                )
        return None

    @classmethod
    def _normalize_generated_bambu_profile(
        cls,
        filament: Filament,
        custom_fields: dict[str, Any],
        product_code: str | None,
    ) -> bool:
        """Repair only the exact technical profile emitted by FilaScan.

        FilaScan and this driver remain independent.  The shared Bambu
        metadata provides enough evidence to safely recognize the generated
        fallback without touching catalog or user-named filaments.
        """
        material_id = str(custom_fields.get("bambu_material_id") or "").upper()
        variant_id = str(custom_fields.get("bambu_variant_id") or "").upper()
        detailed_type = str(
            custom_fields.get("bambu_detailed_filament_type") or ""
        ).strip()
        color_name = str(filament.manufacturer_color_name or "").strip()
        if not all((material_id, variant_id, detailed_type, color_name, product_code)):
            return False

        legacy_designations = {
            f"{detailed_type} · {color_name} [{material_id}-{variant_id}]".casefold(),
            (
                f"Bambu {detailed_type} · {color_name} "
                f"[{material_id}-{variant_id}]"
            ).casefold(),
        }
        if filament.designation.casefold() not in legacy_designations:
            return False

        family = re.sub(
            r"^Bambu(?:\s+Lab)?\s+", "", detailed_type, flags=re.IGNORECASE
        )
        family = re.sub(
            rf"^{re.escape(str(filament.material_type or ''))}(?:\s+|-)?",
            "",
            family,
            flags=re.IGNORECASE,
        ).strip(" -")
        family_names = {
            "hf": "Hf",
            "silk+": "Silk Plus",
            "silk plus": "Silk Plus",
        }
        family = family_names.get(family.casefold(), family)
        color_label = (
            color_name
            if cls._bambu_product_code(color_name) == product_code
            else f"{color_name} ({product_code})"
        )
        filament.designation = (
            f"{family} - {color_label}" if family.casefold() != "basic" else color_label
        )
        filament.manufacturer_color_name = color_label
        filament.material_subgroup = (
            re.sub(r"[^a-z0-9]+", "-", family.casefold()).strip("-") or None
        )
        return True

    @staticmethod
    def _parse_cache_timestamp(value: Any) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _slot_identity(slot: dict[str, Any]) -> str:
        tray_uuid = Driver._normalize_hex_identifier(slot.get("tray_uuid"), 32)
        if tray_uuid:
            return f"uuid:{tray_uuid}"
        return "|".join(
            str(slot.get(key) or "")
            for key in (
                "slot_index",
                "tray_info_idx",
                "tray_type",
                "tray_color",
                "tray_sub_brands",
            )
        )

    async def _fetch_shop_product_images(self, product_url: str) -> dict[str, str]:
        cached = self._shop_page_cache.get(product_url)
        now = time.monotonic()
        if cached and now - cached[0] < SHOP_PAGE_MEMORY_CACHE_SECONDS:
            return dict(cached[1])

        headers = {
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "FilaMan-BambuLab-Plugin/2.6 (shop-image-metadata)",
        }
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(15.0),
            headers=headers,
        ) as client:
            response = await client.get(product_url)
            response.raise_for_status()
            final_url = self._allowed_shop_url(str(response.url))
            if not final_url:
                raise ValueError("Bambu shop request redirected to an unsupported host")
            if len(response.content) > SHOP_PAGE_MAX_BYTES:
                raise ValueError("Bambu shop product page exceeds the safety limit")
            images = self._extract_shop_product_images(response.text)

        self._shop_page_cache[product_url] = (now, images)
        return dict(images)

    async def _cache_shop_image_for_filament(
        self, filament_id: int
    ) -> dict[str, Any]:
        """Resolve and persist image metadata on the shared filament record."""
        now = datetime.now(timezone.utc)
        async with async_session_maker() as db:
            filament = await db.get(Filament, filament_id)
            if filament is None:
                return {}
            custom_fields = dict(filament.custom_fields or {})
            product_code = self._bambu_product_code(
                custom_fields.get(BAMBU_PRODUCT_CODE_FIELD),
                custom_fields.get("bambu_color_code"),
                filament.designation,
                filament.manufacturer_color_name,
            )
            profile_changed = self._normalize_generated_bambu_profile(
                filament, custom_fields, product_code
            )
            if profile_changed:
                await db.commit()
            product_url = self._allowed_shop_url(
                filament.shop_url
                or custom_fields.get(FILAMENT_IMAGE_SOURCE_URL_FIELD)
                or custom_fields.get(BAMBU_SHOP_SOURCE_URL_FIELD)
            ) or self._catalog_product_url(filament, custom_fields)
            image_url = self._allowed_shop_image_url(
                custom_fields.get(FILAMENT_IMAGE_URL_FIELD)
                or custom_fields.get(BAMBU_SHOP_IMAGE_URL_FIELD)
            )
            checked_at = self._parse_cache_timestamp(
                custom_fields.get(FILAMENT_IMAGE_CHECKED_AT_FIELD)
                or custom_fields.get(BAMBU_SHOP_IMAGE_CHECKED_AT_FIELD)
            )
            resolver_is_current = (
                str(custom_fields.get(BAMBU_IMAGE_RESOLVER_VERSION_FIELD) or "")
                == str(STORE_SEARCH_RESOLVER_VERSION)
            )
            base_metadata = {
                "filament_id": filament.id,
                "filament_name": filament.designation,
                "manufacturer_color_name": filament.manufacturer_color_name,
                "material_type": filament.material_type,
                "bambu_product_code": product_code,
                "shop_source_url": product_url,
                "shop_image_url": image_url,
                "image_provider": custom_fields.get(
                    FILAMENT_IMAGE_PROVIDER_FIELD
                )
                or ("bambulab" if image_url else None),
            }

        if not product_code:
            return base_metadata
        if (
            checked_at
            and now - checked_at < timedelta(days=SHOP_IMAGE_CACHE_DAYS)
            and image_url
            and resolver_is_current
        ):
            return base_metadata

        last_attempt = self._shop_image_last_attempt.get(filament_id, 0)
        if (
            last_attempt
            and time.monotonic() - last_attempt < SHOP_IMAGE_ERROR_RETRY_SECONDS
        ):
            return base_metadata
        self._shop_image_last_attempt[filament_id] = time.monotonic()

        search_result: dict[str, str] | None = None
        try:
            search_result = await self._fetch_store_search_image(
                product_code, product_url
            )
        except Exception as exc:
            logger.warning(
                "Could not resolve Bambu store search image for product %s: %s",
                product_code,
                exc,
            )

        search_resolved = bool(search_result)
        resolved_image = (
            search_result.get("shop_image_url") if search_result else None
        )
        if search_result:
            product_url = search_result.get("shop_source_url") or product_url

        if not resolved_image:
            resolved_image = self._optimized_shop_image_url(
                _BAMBU_PRODUCT_IMAGES_BY_CODE.get(product_code)
            )
        if not resolved_image and product_url:
            try:
                product_images = await self._fetch_shop_product_images(product_url)
            except Exception as exc:
                logger.warning(
                    "Could not refresh Bambu shop image for filament %s: %s",
                    filament_id,
                    exc,
                )
                return base_metadata
            resolved_image = product_images.get(product_code)
        if not product_url:
            return base_metadata

        base_metadata["shop_source_url"] = product_url
        checked_at_value = now.isoformat()
        async with async_session_maker() as db:
            filament = await db.get(Filament, filament_id)
            if filament is None:
                return base_metadata
            custom_fields = dict(filament.custom_fields or {})
            custom_fields[BAMBU_PRODUCT_CODE_FIELD] = product_code
            custom_fields[BAMBU_SHOP_SOURCE_URL_FIELD] = product_url
            custom_fields[BAMBU_SHOP_IMAGE_CHECKED_AT_FIELD] = checked_at_value
            custom_fields[FILAMENT_IMAGE_SOURCE_URL_FIELD] = product_url
            custom_fields[FILAMENT_IMAGE_PROVIDER_FIELD] = "bambulab"
            custom_fields[FILAMENT_IMAGE_CHECKED_AT_FIELD] = checked_at_value
            if search_resolved:
                custom_fields[BAMBU_IMAGE_RESOLVER_VERSION_FIELD] = (
                    STORE_SEARCH_RESOLVER_VERSION
                )
            if resolved_image:
                custom_fields[BAMBU_SHOP_IMAGE_URL_FIELD] = resolved_image
                custom_fields[FILAMENT_IMAGE_URL_FIELD] = resolved_image
            filament.custom_fields = custom_fields
            if not filament.shop_url:
                filament.shop_url = product_url
            flag_modified(filament, "custom_fields")
            await db.commit()

        base_metadata["shop_image_url"] = resolved_image or image_url
        base_metadata["image_provider"] = "bambulab"
        logger.info(
            "%s Bambu shop image metadata for filament %s (product %s)",
            "Cached" if resolved_image else "Checked",
            filament_id,
            product_code,
        )
        await event_bus.publish({"event": "filaments_changed"})
        return base_metadata

    def _schedule_shop_image_refresh(self, slots: list[dict[str, Any]]) -> None:
        if not self._resolve_shop_images or not self._loop or not self._running:
            return
        candidates = []
        now = time.monotonic()
        for slot in slots:
            if not slot.get("present"):
                continue
            # Only genuine Bambu RFID trays are allowed to trigger an external
            # shop lookup. Custom filament with matching color must not do so.
            if not self._normalize_hex_identifier(slot.get("tray_uuid"), 32):
                continue
            if not self._normalize_hex_identifier(slot.get("tag_uid"), 16):
                continue
            identity = self._slot_identity(slot)
            last_scheduled = self._shop_slot_last_scheduled.get(identity, 0)
            if now - last_scheduled < SHOP_IMAGE_SCHEDULE_SECONDS:
                continue
            self._shop_slot_last_scheduled[identity] = now
            candidates.append(dict(slot))
        if not candidates:
            return
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(
                self._refresh_shop_images_for_slots(candidates)
            )
        )

    async def _refresh_shop_images_for_slots(
        self, slots: list[dict[str, Any]]
    ) -> None:
        if not self._shop_image_lock:
            return
        async with self._shop_image_lock:
            for slot in slots:
                slot_index = str(slot.get("slot_index") or "")
                filament_id: int | None = None
                async with async_session_maker() as db:
                    spool_id = self._slot_spool_ids.get(slot_index)
                    if spool_id:
                        spool = await db.get(Spool, spool_id)
                        filament_id = spool.filament_id if spool else None
                    if filament_id is None:
                        filament = await self._find_matching_filament(db, slot)
                        filament_id = filament.id if filament else None
                if filament_id is None:
                    continue
                metadata = await self._cache_shop_image_for_filament(filament_id)
                if metadata:
                    self._slot_display_metadata[slot_index] = {
                        "_slot_identity": self._slot_identity(slot),
                        **metadata,
                    }

    async def _refresh_inventory_shop_images(self) -> dict[str, int]:
        """Resolve images for every Bambu filament that has a physical spool."""
        if not self._shop_image_lock:
            return {"filaments": 0, "images": 0}
        async with self._shop_image_lock:
            async with async_session_maker() as db:
                result = await db.execute(
                    select(Filament.id)
                    .join(Manufacturer, Filament.manufacturer_id == Manufacturer.id)
                    .join(Spool, Spool.filament_id == Filament.id)
                    .where(func.lower(Manufacturer.name).in_(("bambu", "bambu lab")))
                    .distinct()
                    .order_by(Filament.id)
                )
                filament_ids = list(result.scalars().all())

            image_count = 0
            for filament_id in filament_ids:
                metadata = await self._cache_shop_image_for_filament(filament_id)
                if metadata.get("shop_image_url"):
                    image_count += 1
            return {"filaments": len(filament_ids), "images": image_count}

    async def refresh_status(self) -> dict[str, Any]:
        """Reconcile catalog images when FilaMan requests an explicit refresh.

        FilaMan may run multiple web workers, while its event bus is local to a
        worker process. The explicit health refresh is proxied to the primary
        worker and therefore provides a reliable reconciliation path for
        inventory changes emitted by another plugin such as FilaScan.
        """
        if not self._running or not self._resolve_shop_images:
            return {}
        try:
            stats = await self._refresh_inventory_shop_images()
        except Exception as exc:
            # Driver health and the inventory gallery must remain available
            # when the printer or an external image service is offline.
            logger.warning("Bambu inventory image refresh failed: %s", exc)
            return {}
        logger.info(
            "Bambu inventory image refresh checked %s filaments; %s have images",
            stats["filaments"],
            stats["images"],
        )
        return {"catalog_images": stats}

    @classmethod
    def _inventory_enrichment_owner(cls) -> "Driver | None":
        candidates = [
            driver
            for driver in cls._inventory_enrichment_instances
            if driver._running and driver._resolve_shop_images
        ]
        return min(candidates, key=lambda driver: driver.printer_id) if candidates else None

    async def _register_inventory_enrichment(self) -> None:
        if self._inventory_enrichment_registered:
            return
        cls = type(self)
        cls._inventory_enrichment_instances.add(self)
        self._inventory_enrichment_registered = True

        listener_running = (
            cls._inventory_enrichment_listener_task is not None
            and not cls._inventory_enrichment_listener_task.done()
        )
        worker_running = (
            cls._inventory_enrichment_worker_task is not None
            and not cls._inventory_enrichment_worker_task.done()
        )
        if not listener_running or not worker_running:
            stale_tasks = []
            for task in (
                cls._inventory_enrichment_listener_task,
                cls._inventory_enrichment_worker_task,
            ):
                if task is not None and not task.done():
                    task.cancel()
                    stale_tasks.append(task)
            if stale_tasks:
                await asyncio.gather(*stale_tasks, return_exceptions=True)
            cls._inventory_enrichment_event = asyncio.Event()
            cls._inventory_enrichment_listener_task = asyncio.create_task(
                cls._inventory_enrichment_event_loop()
            )
            cls._inventory_enrichment_worker_task = asyncio.create_task(
                cls._inventory_enrichment_worker_loop()
            )

        # A newly started driver must reconcile inventory immediately, even if
        # relevant events were emitted while the Bambu integration was offline.
        if cls._inventory_enrichment_event is not None:
            cls._inventory_enrichment_event.set()

    async def _unregister_inventory_enrichment(self) -> None:
        if not self._inventory_enrichment_registered:
            return
        cls = type(self)
        cls._inventory_enrichment_instances.discard(self)
        self._inventory_enrichment_registered = False
        if cls._inventory_enrichment_instances:
            if cls._inventory_enrichment_event is not None:
                cls._inventory_enrichment_event.set()
            return

        tasks = [
            task
            for task in (
                cls._inventory_enrichment_listener_task,
                cls._inventory_enrichment_worker_task,
            )
            if task is not None
        ]
        cls._inventory_enrichment_listener_task = None
        cls._inventory_enrichment_worker_task = None
        cls._inventory_enrichment_event = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @classmethod
    async def _inventory_enrichment_event_loop(cls) -> None:
        """Wake the shared worker when any FilaMan source changes inventory."""
        try:
            async for raw_event in event_bus.subscribe():
                if not cls._inventory_enrichment_instances:
                    return
                try:
                    event_name = json.loads(raw_event).get("event")
                except (json.JSONDecodeError, AttributeError, TypeError):
                    continue
                if (
                    event_name in INVENTORY_IMAGE_EVENTS
                    and cls._inventory_enrichment_event is not None
                ):
                    cls._inventory_enrichment_event.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The periodic worker remains a fallback if event subscription fails.
            logger.warning("Bambu inventory event listener failed: %s", exc)

    @classmethod
    async def _inventory_enrichment_worker_loop(cls) -> None:
        """Run one debounced image enrichment scan for all Bambu drivers."""
        while cls._inventory_enrichment_instances:
            event = cls._inventory_enrichment_event
            if event is None:
                return
            triggered_by_event = False
            try:
                await asyncio.wait_for(
                    event.wait(), timeout=INVENTORY_IMAGE_SCAN_SECONDS
                )
                triggered_by_event = True
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise

            event.clear()
            if triggered_by_event:
                await asyncio.sleep(INVENTORY_IMAGE_EVENT_DEBOUNCE_SECONDS)
                # Coalesce the paired spools/filaments events and any burst of
                # bulk-import notifications into this single reconciliation.
                event.clear()

            owner = cls._inventory_enrichment_owner()
            if owner is None:
                continue
            try:
                stats = await owner._refresh_inventory_shop_images()
                logger.info(
                    "Bambu inventory image scan checked %s filaments; %s have images",
                    stats["filaments"],
                    stats["images"],
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Bambu inventory image scan failed: %s", exc)

    async def _ensure_rfid_extra_fields(self) -> None:
        """Create the two spool fields used for the physical Bambu RFID tags."""
        field_defs = (
            (BAMBU_RFID_TAG_1_FIELD, "Bambu RFID Tag 1"),
            (BAMBU_RFID_TAG_2_FIELD, "Bambu RFID Tag 2"),
        )
        try:
            async with async_session_maker() as db:
                for key, label in field_defs:
                    result = await db.execute(
                        select(SystemExtraField).where(
                            SystemExtraField.target_type == "spool",
                            SystemExtraField.key == key,
                        )
                    )
                    field = result.scalar_one_or_none()
                    if field is None:
                        db.add(
                            SystemExtraField(
                                target_type="spool",
                                key=key,
                                label=label,
                                field_type="text",
                                source=self.driver_key,
                                config={"max_length": 32},
                            )
                        )
                await db.commit()
        except Exception as e:
            logger.error(f"Failed to ensure Bambu RFID extra fields: {e}")

    async def _find_matching_filament(self, db, slot: dict[str, Any]) -> Filament | None:
        """Find one existing filament; never create catalog data implicitly."""
        tray_info_idx = str(slot.get("tray_info_idx") or "").strip().upper()
        tray_index_candidate_ids: set[int] = set()
        if tray_info_idx:
            result = await db.execute(
                select(Filament)
                .join(
                    FilamentPrinterParam,
                    FilamentPrinterParam.filament_id == Filament.id,
                )
                .where(
                    FilamentPrinterParam.printer_id == self.printer_id,
                    FilamentPrinterParam.param_key == "bambu_tray_idx",
                    func.lower(FilamentPrinterParam.param_value)
                    == tray_info_idx.lower(),
                )
            )
            matches = list(result.scalars().unique().all())
            tray_index_candidate_ids = {item.id for item in matches}
            if len(matches) > 1:
                logger.warning(
                    f"Tray index {tray_info_idx} is not unique; continuing with "
                    "Bambu material/subtype/color matching"
                )

        tray_type = str(slot.get("tray_type") or "").strip()
        tray_color = self._normalize_tray_color(slot.get("tray_color"))
        if not tray_type or not tray_color:
            return None

        result = await db.execute(
            select(Filament)
            .join(Manufacturer, Filament.manufacturer_id == Manufacturer.id)
            .join(FilamentColor, FilamentColor.filament_id == Filament.id)
            .join(Color, Color.id == FilamentColor.color_id)
            .where(
                func.lower(Manufacturer.name).in_(("bambu", "bambu lab")),
                func.lower(Filament.material_type) == tray_type.lower(),
                func.lower(Color.hex_code) == f"#{tray_color}".lower(),
            )
        )
        candidates = list(result.scalars().unique().all())
        if tray_index_candidate_ids:
            indexed_candidates = [
                item for item in candidates if item.id in tray_index_candidate_ids
            ]
            selected = self._select_bambu_candidate(indexed_candidates, slot)
            if selected is not None:
                return selected
        mapped_name = self._bambu_filaments.get(tray_info_idx)
        if mapped_name:
            exact = [
                item
                for item in candidates
                if item.designation.strip().lower() == mapped_name.lower()
                or item.designation.strip().lower()
                == mapped_name.removeprefix("Bambu ").lower()
            ]
            if len(exact) == 1:
                return exact[0]
        selected = self._select_bambu_candidate(candidates, slot)
        if selected is None and candidates:
            logger.warning(
                "Bambu filament match remains ambiguous after subtype/color "
                f"filtering: tray_info_idx={tray_info_idx}, "
                f"tray_sub_brands={slot.get('tray_sub_brands', '')}, "
                f"candidate_ids={[item.id for item in candidates]}"
            )
        return selected

    async def _find_existing_bambu_spool(
        self, db, external_id: str
    ) -> Spool | None:
        result = await db.execute(
            select(Spool).where(Spool.external_id == external_id)
        )
        return result.scalar_one_or_none()

    def _schedule_auto_import(self, slots: list[dict[str, Any]]) -> None:
        if not self._auto_import_spools or not self._loop or not self._running:
            return

        now = time.monotonic()
        candidates: list[dict[str, Any]] = []
        for slot in slots:
            tray_uuid = self._normalize_hex_identifier(slot.get("tray_uuid"), 32)
            if not tray_uuid:
                continue
            last_attempt = self._auto_import_last_attempt.get(tray_uuid, 0)
            if now - last_attempt < AUTO_IMPORT_RETRY_SECONDS:
                continue
            self._auto_import_last_attempt[tray_uuid] = now
            candidates.append(dict(slot))

        if not candidates:
            return

        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self._auto_import_rfid_spools(candidates))
        )

    async def _auto_import_rfid_spools(
        self, slots: list[dict[str, Any]]
    ) -> None:
        if not self._auto_import_lock:
            return

        changed = False
        async with self._auto_import_lock:
            for slot in slots:
                tray_uuid = self._normalize_hex_identifier(slot.get("tray_uuid"), 32)
                tag_uid = self._normalize_hex_identifier(slot.get("tag_uid"), 16)
                if not tray_uuid:
                    continue

                external_id = f"{BAMBU_EXTERNAL_ID_PREFIX}{tray_uuid}"
                spool: Spool | None = None
                created = False
                dirty = False
                weight_changed = False
                estimated_weight = self._estimated_remaining_weight(slot)

                async with async_session_maker() as db:
                    spool = await self._find_existing_bambu_spool(db, external_id)

                    if spool is None:
                        filament = await self._find_matching_filament(db, slot)
                        if filament is None:
                            logger.warning(
                                "Bambu RFID spool not imported because no unique matching "
                                "FilaMan filament exists: "
                                f"tray_uuid={tray_uuid}, "
                                f"tray_info_idx={slot.get('tray_info_idx', '')}, "
                                f"tray_type={slot.get('tray_type', '')}, "
                                f"tray_color={slot.get('tray_color', '')}"
                            )
                            continue

                        status_result = await db.execute(
                            select(SpoolStatus).where(SpoolStatus.key == "opened")
                        )
                        status_obj = status_result.scalar_one_or_none()
                        if status_obj is None:
                            status_result = await db.execute(
                                select(SpoolStatus).where(SpoolStatus.key == "new")
                            )
                            status_obj = status_result.scalar_one_or_none()
                        if status_obj is None:
                            logger.error(
                                "Bambu RFID spool not imported: neither 'opened' nor "
                                "'new' spool status exists"
                            )
                            continue

                        custom_fields = {}
                        if tag_uid:
                            custom_fields[BAMBU_RFID_TAG_1_FIELD] = tag_uid
                        spool = Spool(
                            filament_id=filament.id,
                            status_id=status_obj.id,
                            external_id=external_id,
                            stocked_in_at=datetime.now(timezone.utc),
                            remaining_weight_g=estimated_weight,
                            custom_fields=custom_fields,
                        )
                        db.add(spool)
                        try:
                            await db.commit()
                            await db.refresh(spool)
                            created = True
                        except IntegrityError:
                            await db.rollback()
                            result = await db.execute(
                                select(Spool).where(Spool.external_id == external_id)
                            )
                            spool = result.scalar_one_or_none()
                            if spool is None:
                                raise
                    else:
                        custom_fields = dict(spool.custom_fields or {})
                        if tag_uid and not custom_fields.get(BAMBU_RFID_TAG_1_FIELD):
                            custom_fields[BAMBU_RFID_TAG_1_FIELD] = tag_uid
                            spool.custom_fields = custom_fields
                            flag_modified(spool, "custom_fields")
                            dirty = True
                        if not spool.external_id:
                            spool.external_id = external_id
                            dirty = True
                        if estimated_weight is not None and (
                            spool.remaining_weight_g is None
                            or abs(spool.remaining_weight_g - estimated_weight) >= 0.01
                        ):
                            previous_weight = spool.remaining_weight_g
                            spool.remaining_weight_g = estimated_weight
                            dirty = True
                            weight_changed = True
                            logger.info(
                                "Updated Bambu RFID spool %s estimated remaining "
                                "weight from %s g to %.1f g (tray_weight=%s, remain=%s%%)",
                                spool.id,
                                previous_weight,
                                estimated_weight,
                                slot.get("tray_weight"),
                                slot.get("remain"),
                            )
                        if dirty:
                            await db.commit()

                if spool is None:
                    continue

                tray_mapping_changed = (
                    self._spool_ids_by_tray_uuid.get(tray_uuid) != spool.id
                )
                self._spool_ids_by_tray_uuid[tray_uuid] = spool.id
                slot_index = str(slot.get("slot_index") or "")
                slot_mapping_changed = bool(slot_index) and (
                    self._slot_spool_ids.get(slot_index) != spool.id
                )
                if slot_index:
                    self._slot_spool_ids[slot_index] = spool.id
                parsed_slot = self._parse_slot_index(slot_index)
                if parsed_slot and slot_mapping_changed:
                    await self._update_spool_location(
                        spool.id, parsed_slot[0], parsed_slot[1]
                    )
                changed = (
                    changed
                    or created
                    or dirty
                    or tray_mapping_changed
                    or slot_mapping_changed
                )
                if created or dirty or tray_mapping_changed or slot_mapping_changed:
                    action = (
                        "Created"
                        if created
                        else "Updated"
                        if weight_changed
                        else "Matched"
                    )
                    logger.info(
                        f"{action} Bambu RFID spool {spool.id} "
                        f"for tray_uuid={tray_uuid} at slot {slot_index}"
                    )

            if changed:
                await event_bus.publish({"event": "spools_changed"})
                self._emit_slots_with_spool_ids()

    def _emit_slots_with_spool_ids(self) -> None:
        updated_slots: list[dict[str, Any]] = []
        for current in self._current_slots:
            slot = dict(current)
            tray_uuid = self._normalize_hex_identifier(slot.get("tray_uuid"), 32)
            if tray_uuid and tray_uuid in self._spool_ids_by_tray_uuid:
                spool_id = self._spool_ids_by_tray_uuid[tray_uuid]
                slot["spool_id"] = spool_id
                self._slot_spool_ids[str(slot.get("slot_index") or "")] = spool_id
            updated_slots.append(slot)
        self._current_slots = updated_slots
        self.emit(
            {
                "event_type": "slots_update",
                "slots": updated_slots,
                "ams_info": self._build_ams_info(updated_slots),
            }
        )

    def _build_ams_info(self, slots: list[dict[str, Any]]) -> dict[str, Any]:
        external_slots = [
            slot
            for slot in slots
            if str(slot.get("slot_index") or "").startswith("255-")
        ]
        has_external = bool(external_slots)
        total_slots = sum(
            unit.get("tray_count", 0) for unit in self._current_ams_units
        )
        total_slots += len(external_slots)
        return {
            "ams_count": len(self._current_ams_units),
            "ams_type": "AMS Lite" if self._is_ams_lite else "AMS",
            "slot_count": total_slots,
            "external_spool": has_external,
            "ams_units": self._current_ams_units,
        }

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

        if ams_id >= 200:  # Virtual/external slots use tray ids 254 and 255
            external_slot = tray_id - 253 if tray_id >= 254 else tray_id + 1
            return f"{printer_name} - ext. Slot {external_slot}"
        elif ams_id >= 128:  # AMS HT units are numbered from protocol id 128
            return f"{printer_name} - AMS HT {ams_id - 127} - Slot {tray_id + 1}"
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
        vir_slot = print_data.get("vir_slot")

        # vir_slot is used by newer printers for one or more external trays.
        if ams_section is None and vt_tray is None and vir_slot is None:
            return

        ams_data = (ams_section or {}).get("ams", [])

        # Leichtgewichtige Nachricht (nur tray_now/version) — keine Slot-Daten vorhanden
        if not ams_data and vt_tray is None and vir_slot is None:
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

                    if ams_id >= 128:
                        slot_name = (
                            f"AMS HT {ams_id - 127} - Slot {tray_id + 1}"
                        )
                    elif self._is_ams_lite:
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
                            "tag_uid": tray.get("tag_uid", ""),
                            "tray_uuid": tray.get("tray_uuid", ""),
                            "tray_sub_brands": tray.get("tray_sub_brands", ""),
                            "tray_weight": tray.get("tray_weight"),
                            "remain": tray.get("remain"),
                            "present": present,
                        }
                    )
        else:
            ams_slots = prev_ams_slots

        # Newer printer generations use vir_slot (list); older ones use vt_tray.
        external_trays: list[dict[str, Any]] | None = None
        if vir_slot is not None:
            if isinstance(vir_slot, list):
                external_trays = [item for item in vir_slot if isinstance(item, dict)]
            elif isinstance(vir_slot, dict):
                external_trays = [vir_slot]
            else:
                external_trays = []
        elif isinstance(vt_tray, dict):
            external_trays = [vt_tray]

        if external_trays is not None:
            ext_slots = []
            multiple_external = len(external_trays) > 1
            for external_index, tray in enumerate(external_trays):
                try:
                    tray_id = int(tray.get("id", 254 + external_index))
                except (TypeError, ValueError):
                    tray_id = 254 + external_index
                external_number = (
                    tray_id - 253 if tray_id >= 254 else external_index + 1
                )
                ext_slots.append(
                    {
                        "slot_index": f"255-{tray_id}",
                        "slot_name": (
                            f"External Tray {external_number}"
                            if multiple_external
                            else "External Tray"
                        ),
                        "tray_info_idx": tray.get("tray_info_idx", ""),
                        "tray_type": tray.get("tray_type", ""),
                        "tray_color": tray.get("tray_color", ""),
                        "nozzle_temp_min": tray.get("nozzle_temp_min"),
                        "nozzle_temp_max": tray.get("nozzle_temp_max"),
                        "setting_id": tray.get("setting_id", ""),
                        "cali_idx": tray.get("cali_idx"),
                        "tag_uid": tray.get("tag_uid", ""),
                        "tray_uuid": tray.get("tray_uuid", ""),
                        "tray_sub_brands": tray.get("tray_sub_brands", ""),
                        "tray_weight": tray.get("tray_weight"),
                        "remain": tray.get("remain"),
                        "present": bool(tray.get("tray_type")),
                    }
                )
        else:
            ext_slots = prev_ext_slots

        # Zusammenführen: AMS-Slots + External Slot
        slots = ams_slots + ext_slots
        for slot in slots:
            slot_index = str(slot.get("slot_index") or "")
            tray_uuid = self._normalize_hex_identifier(slot.get("tray_uuid"), 32)
            if tray_uuid and tray_uuid in self._spool_ids_by_tray_uuid:
                spool_id = self._spool_ids_by_tray_uuid[tray_uuid]
                slot["spool_id"] = spool_id
                self._slot_spool_ids[slot_index] = spool_id
            elif not slot.get("present") and slot_index in self._slot_spool_ids:
                slot["spool_id"] = None
                self._slot_spool_ids.pop(slot_index, None)

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
                dispatched = self._send_filament_setting(
                    ams_id_parsed, tray_id_parsed, self._pending.filament_data
                )
                # Location nach erfolgreichem Auto-Assignment aktualisieren
                if dispatched and self._loop and filaman_spool_id:
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

        ams_info = self._build_ams_info(slots)

        # Auto-import is independent from MQTT read-only mode: it only writes
        # to FilaMan's local database.
        self._schedule_auto_import(slots)
        self._schedule_shop_image_refresh(slots)

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
    ) -> bool:
        """Filament-Setting an Drucker senden. Läuft in separatem Thread,
        da set_filament_printer() blockierend ist (wait_for_publish)."""
        if self._read_only:
            logger.info(
                f"Read-only mode: skipped filament setting for slot {ams_id}-{tray_id}"
            )
            return False
        threading.Thread(
            target=self._do_send_filament_setting,
            args=(ams_id, tray_id, filament_data),
            daemon=True,
        ).start()
        return True

    def _do_send_filament_setting(
        self, ams_id: int, tray_id: int, filament_data: dict
    ) -> None:
        """Blockierender Filament-Setting Versand (Thread-Pool)."""
        if self._read_only:
            logger.warning(
                f"Read-only mode: blocked filament setting for slot {ams_id}-{tray_id}"
            )
            return
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
            mqtt_ams_id = ams_id if ams_id < 200 else 255
            mqtt_tray_id = tray_id
            if mqtt_ams_id == 255 and tray_id < 254:
                mqtt_tray_id = 254
            result = self._printer.set_filament_printer(
                color=color,
                filament=filament,
                ams_id=mqtt_ams_id,
                tray_id=mqtt_tray_id,
            )
            self.log_debug(
                "out",
                f"device/{self._serial}/request",
                {
                    "command": "ams_filament_setting",
                    "ams_id": mqtt_ams_id,
                    "tray_id": mqtt_tray_id,
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
        dispatched = self._send_filament_setting(ams_id, tray_id, filament_data)

        # Location nach erfolgreichem Direkt-Assignment aktualisieren
        filaman_spool_id = filament_data.get("filaman_spool_id")
        if dispatched and filaman_spool_id and self._loop:
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
        if self._read_only:
            logger.info(
                f"Read-only mode: ignored pending spool {spool_id} for printer "
                f"{self.printer_id}"
            )
            return
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
        display_slots: list[dict[str, Any]] = []
        for current in self._current_slots:
            slot = dict(current)
            slot_index = str(slot.get("slot_index") or "")
            metadata = self._slot_display_metadata.get(slot_index)
            if metadata and metadata.get("_slot_identity") == self._slot_identity(slot):
                slot.update(
                    {
                        key: value
                        for key, value in metadata.items()
                        if not key.startswith("_")
                    }
                )
            display_slots.append(slot)
        return {
            "driver_key": self.driver_key,
            "printer_id": self.printer_id,
            "running": self._running,
            "connected": self._connected,
            "pending": self._pending is not None,
            "read_only": self._read_only,
            "auto_import_spools": self._auto_import_spools,
            "resolve_shop_images": self._resolve_shop_images,
            "printer_model": self._printer_model,
            "ams_type": "AMS Lite" if self._is_ams_lite else "AMS",
            "ams_count": len(self._current_ams_units),
            "slot_count": total_slots,
            "external_spool": ext_exists,
            "ams_units": self._current_ams_units,
            "shop_image_count": sum(
                bool(slot.get("shop_image_url")) for slot in display_slots
            ),
            "slots": display_slots,
        }
