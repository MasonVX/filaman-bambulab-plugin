import importlib
import asyncio
import json
import unittest
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Color, Filament, FilamentColor, Manufacturer, Printer
from app.models.base import Base
from app.models.printer_params import FilamentPrinterParam
from app.models.spool import Spool, SpoolStatus


DRIVER_MODULE = importlib.import_module("bambulab.driver")
CATALOG_MODULE = importlib.import_module("bambulab.catalog")
ENRICHMENT_MODULE = importlib.import_module("bambulab.catalog_enrichment")
SPOOL_SYNC_MODULE = importlib.import_module("bambulab.spool_sync")
Driver = DRIVER_MODULE.Driver


def make_driver(printer_id=1, **config):
    events = []
    driver = Driver(
        printer_id=printer_id,
        config={"printer_model": "P1S", **config},
        emitter=events.append,
    )
    driver._running = True
    return driver, events


class IdentifierTests(unittest.TestCase):
    def test_normalizes_valid_tag_uid(self):
        self.assertEqual(
            Driver._normalize_hex_identifier("a1:b2:c3:d4:e5:f6:01:02", 16),
            "A1B2C3D4E5F60102",
        )

    def test_rejects_zero_placeholder(self):
        self.assertIsNone(
            Driver._normalize_hex_identifier("0000000000000000", 16)
        )
        self.assertIsNone(
            Driver._normalize_hex_identifier("0" * 32, 32)
        )

    def test_rejects_wrong_length(self):
        self.assertIsNone(Driver._normalize_hex_identifier("A1B2C3D4", 16))

    def test_zero_is_valid_remaining_percentage(self):
        self.assertEqual(Driver._parse_percentage(0), 0)

    def test_calculates_bambu_estimated_remaining_weight(self):
        self.assertEqual(
            Driver._estimated_remaining_weight(
                {"tray_weight": "1000", "remain": 49}
            ),
            490,
        )
        self.assertEqual(
            Driver._estimated_remaining_weight(
                {"tray_weight": "1000", "remain": 0}
            ),
            0,
        )

    def test_rejects_unknown_bambu_weight_estimate(self):
        self.assertIsNone(
            Driver._estimated_remaining_weight(
                {"tray_weight": "0", "remain": -1}
            )
        )


class ShopImageParsingTests(unittest.TestCase):
    def test_store_search_uses_highlighted_product_media_not_color_palette(self):
        payload = {
            "code": 1,
            "data": {
                "page": {
                    "total": "1",
                    "records": [
                        {
                            "seoCode": "pla-pure",
                            "highlightProductSkuId": "738891519803023418",
                            "mediaFiles": [
                                "https://store.bblcdn.eu/product/pure-white-spool.jpg"
                            ],
                            "colorList": [
                                {
                                    "colorPalette": (
                                        "https://store.bblcdn.eu/palette/"
                                        "Pure_White.jpg"
                                    )
                                }
                            ],
                        }
                    ],
                }
            },
        }

        result = Driver._parse_store_search_product(
            payload,
            "https://eu.store.bambulab.com/products/pla-pure",
        )

        self.assertIn("pure-white-spool.jpg", result["shop_image_url"])
        self.assertNotIn("Pure_White.jpg", result["shop_image_url"])
        self.assertEqual(
            result["shop_source_url"],
            "https://eu.store.bambulab.com/de/products/pla-pure"
            "?id=738891519803023418",
        )

    def test_store_search_requires_one_result_for_the_expected_product(self):
        payload = {
            "code": 1,
            "data": {
                "page": {
                    "total": "2",
                    "records": [
                        {
                            "seoCode": "pla-pure",
                            "highlightProductSkuId": "123",
                            "mediaFiles": ["https://store.bblcdn.eu/image.jpg"],
                        }
                    ],
                }
            },
        }

        self.assertIsNone(Driver._parse_store_search_product(payload))

    def test_extracts_and_optimizes_product_image_from_json_ld(self):
        html = """
        <html><head><script type="application/ld+json">
        {
          "@type": "ProductGroup",
          "hasVariant": [
            {
              "@type": "Product",
              "name": "PLA Matte - Charcoal (11101) / Refill / 1kg",
              "image": "https://store.bblcdn.eu/s8/default/hash/charcoal.png"
            }
          ]
        }
        </script></head></html>
        """

        images = Driver._extract_shop_product_images(html)

        self.assertIn("11101", images)
        self.assertEqual(
            images["11101"],
            "https://store.bblcdn.eu/s8/default/hash/charcoal.png"
            "__op__resize,m_lfit,w_640__op__format,f_auto__op__quality,q_80",
        )

    def test_prefers_current_storefront_color_swatch(self):
        html = """
        <li value="Pure White (17100)" class="color-option">
          <img src="https://store.bblcdn.eu/s8/default/hash/Pure_White.jpg">
        </li>
        <script type="application/ld+json">
        {
          "@type": "Product",
          "name": "PLA Pure - Pure White (17100) / Refill / 1kg",
          "image": "https://store.bblcdn.eu/s8/default/hash/generic.jpg"
        }
        </script>
        """

        images = Driver._extract_shop_product_images(html)

        self.assertEqual(
            images["17100"],
            "https://store.bblcdn.eu/s8/default/hash/Pure_White.jpg"
            "__op__resize,m_lfit,w_640__op__format,f_auto__op__quality,q_80",
        )

    def test_rejects_non_bambu_product_and_image_hosts(self):
        self.assertIsNone(Driver._allowed_shop_url("https://example.com/pla"))
        self.assertIsNone(
            Driver._allowed_shop_image_url("https://example.com/charcoal.png")
        )

    def test_health_only_exposes_metadata_for_current_slot_identity(self):
        driver, _ = make_driver(resolve_shop_images=True)
        slot = {
            "slot_index": "0-0",
            "tray_type": "PLA",
            "tray_color": "000000FF",
            "tray_uuid": "AABBCCDDEEFF0011AABBCCDDEEFF0011",
        }
        driver._current_slots = [slot]
        driver._slot_display_metadata["0-0"] = {
            "_slot_identity": driver._slot_identity(slot),
            "shop_image_url": "https://store.bblcdn.eu/image.png",
        }

        self.assertEqual(
            driver.health()["slots"][0]["shop_image_url"],
            "https://store.bblcdn.eu/image.png",
        )

        driver._current_slots[0]["tray_uuid"] = "11223344556677881122334455667788"
        self.assertNotIn("shop_image_url", driver.health()["slots"][0])


class PluginPageTests(unittest.TestCase):
    def test_manifest_registers_custom_navigation_page(self):
        plugin_dir = Path(__file__).resolve().parents[1] / "bambulab"
        manifest = json.loads((plugin_dir / "plugin.json").read_text())

        self.assertEqual(manifest["version"], "2.7.3")
        self.assertEqual(manifest["page_url"], "/plugin-page/bambulab")
        self.assertTrue(manifest["show_in_nav"])
        self.assertTrue((plugin_dir / "page.html").is_file())

    def test_spool_gallery_requests_primary_driver_image_refresh(self):
        plugin_dir = Path(__file__).resolve().parents[1] / "bambulab"
        page = (plugin_dir / "page.html").read_text()

        self.assertIn("const loadInventory = async (refreshImages = false)", page)
        self.assertIn("/driver/health?refresh=1", page)
        self.assertIn("const loadDriverHealth = async", page)
        self.assertIn("catch { return requestJson(url); }", page)
        self.assertIn("Promise.allSettled", page)
        self.assertIn("else loadInventory(true);", page)
        self.assertIn("loadInventory(true));", page)

    def test_plugin_page_has_back_navigation_and_user_language(self):
        plugin_dir = Path(__file__).resolve().parents[1] / "bambulab"
        page = (plugin_dir / "page.html").read_text()

        self.assertNotIn('class="fm-sidebar"', page)
        self.assertNotIn('class="plugin-main"', page)
        self.assertIn('id="back-button"', page)
        self.assertIn("window.history.back()", page)
        self.assertIn("window.location.href = '/';", page)
        self.assertIn("/api/v1/me", page)
        self.assertIn("localStorage.getItem('lang')", page)
        self.assertIn("const translations = {", page)
        self.assertIn("en: {", page)
        self.assertIn("de: {", page)
        self.assertIn("'page.back': 'Zurück zu FilaMan'", page)
        self.assertIn('data-i18n="page.title"', page)


class ModuleLayoutTests(unittest.TestCase):
    """Protect the responsibility boundaries of the split driver package."""

    def test_driver_delegates_feature_areas_to_focused_modules(self):
        self.assertEqual(Driver.__module__, "bambulab.driver")
        self.assertEqual(Driver._process_slots.__module__, "bambulab.slots")
        self.assertEqual(
            Driver._auto_import_rfid_spools.__module__, "bambulab.spool_sync"
        )
        self.assertEqual(
            Driver._cache_shop_image_for_filament.__module__, "bambulab.catalog"
        )
        self.assertEqual(
            Driver._register_inventory_enrichment.__module__,
            "bambulab.catalog_enrichment",
        )


class InventoryEnrichmentCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_debounce = (
            ENRICHMENT_MODULE.INVENTORY_IMAGE_EVENT_DEBOUNCE_SECONDS
        )
        self.original_interval = ENRICHMENT_MODULE.INVENTORY_IMAGE_SCAN_SECONDS
        ENRICHMENT_MODULE.INVENTORY_IMAGE_EVENT_DEBOUNCE_SECONDS = 0.01
        ENRICHMENT_MODULE.INVENTORY_IMAGE_SCAN_SECONDS = 60
        self.drivers = []

    async def asyncTearDown(self):
        for driver in list(Driver._inventory_enrichment_instances):
            await driver._unregister_inventory_enrichment()
        ENRICHMENT_MODULE.INVENTORY_IMAGE_EVENT_DEBOUNCE_SECONDS = self.original_debounce
        ENRICHMENT_MODULE.INVENTORY_IMAGE_SCAN_SECONDS = self.original_interval

    async def _wait_for(self, predicate, timeout=1.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("Timed out waiting for inventory enrichment")
            await asyncio.sleep(0.01)

    async def test_inventory_events_are_debounced_and_shared_by_all_drivers(self):
        first, _ = make_driver(printer_id=1, resolve_shop_images=True)
        second, _ = make_driver(printer_id=2, resolve_shop_images=True)
        self.drivers = [first, second]
        first._shop_image_lock = asyncio.Lock()
        second._shop_image_lock = asyncio.Lock()
        calls = []

        async def refresh_first():
            calls.append(first.printer_id)
            return {"filaments": 1, "images": 1}

        async def refresh_second():
            calls.append(second.printer_id)
            return {"filaments": 1, "images": 1}

        first._refresh_inventory_shop_images = refresh_first
        second._refresh_inventory_shop_images = refresh_second

        await first._register_inventory_enrichment()
        await second._register_inventory_enrichment()
        await self._wait_for(lambda: len(calls) == 1)
        self.assertEqual(calls, [1])
        self.assertEqual(len(Driver._inventory_enrichment_instances), 2)

        calls.clear()
        await ENRICHMENT_MODULE.event_bus.publish({"event": "locations_changed"})
        await asyncio.sleep(0.05)
        self.assertEqual(calls, [])

        await ENRICHMENT_MODULE.event_bus.publish({"event": "spools_changed"})
        await ENRICHMENT_MODULE.event_bus.publish({"event": "filaments_changed"})
        await self._wait_for(lambda: len(calls) == 1)
        await asyncio.sleep(0.05)
        self.assertEqual(calls, [1])

        calls.clear()
        await first._unregister_inventory_enrichment()
        await self._wait_for(lambda: len(calls) == 1)
        self.assertEqual(calls, [2])

        await second._unregister_inventory_enrichment()
        self.assertEqual(Driver._inventory_enrichment_instances, set())
        self.assertIsNone(Driver._inventory_enrichment_listener_task)
        self.assertIsNone(Driver._inventory_enrichment_worker_task)

    async def test_explicit_driver_refresh_reconciles_inventory_images(self):
        driver, _ = make_driver(printer_id=1, resolve_shop_images=True)
        calls = []

        async def refresh_images():
            calls.append(True)
            return {"filaments": 14, "images": 12}

        driver._refresh_inventory_shop_images = refresh_images

        result = await driver.refresh_status()

        self.assertEqual(calls, [True])
        self.assertEqual(
            result,
            {"catalog_images": {"filaments": 14, "images": 12}},
        )

    async def test_explicit_driver_refresh_is_inert_when_images_are_disabled(self):
        driver, _ = make_driver(printer_id=1, resolve_shop_images=False)

        async def unexpected_refresh():
            self.fail("disabled catalog images must not scan inventory")

        driver._refresh_inventory_shop_images = unexpected_refresh

        self.assertEqual(await driver.refresh_status(), {})

    async def test_explicit_driver_refresh_failure_does_not_break_health(self):
        driver, _ = make_driver(printer_id=1, resolve_shop_images=True)

        async def failed_refresh():
            raise RuntimeError("image service offline")

        driver._refresh_inventory_shop_images = failed_refresh

        with self.assertLogs(level="WARNING"):
            self.assertEqual(await driver.refresh_status(), {})


class SlotProcessingTests(unittest.TestCase):
    def test_retains_bambu_rfid_identifiers_in_slot_state(self):
        driver, _ = make_driver(auto_import_spools=False)
        driver._process_slots(
            {
                "print": {
                    "command": "push_status",
                    "ams": {
                        "ams": [
                            {
                                "id": "0",
                                "tray": [
                                    {
                                        "id": "0",
                                        "tray_type": "PLA",
                                        "tray_color": "FF0000FF",
                                        "tray_info_idx": "GFA00",
                                        "tag_uid": "A1B2C3D4E5F60102",
                                        "tray_uuid": "AABBCCDDEEFF0011AABBCCDDEEFF0011",
                                        "tray_weight": "1000",
                                        "remain": 75,
                                    }
                                ],
                            }
                        ]
                    },
                }
            }
        )

        slot = driver._current_slots[0]
        self.assertEqual(slot["tag_uid"], "A1B2C3D4E5F60102")
        self.assertEqual(
            slot["tray_uuid"], "AABBCCDDEEFF0011AABBCCDDEEFF0011"
        )
        self.assertEqual(slot["tray_weight"], "1000")
        self.assertEqual(slot["remain"], 75)

    def test_external_location_has_human_slot_number(self):
        driver, _ = make_driver()
        driver._printer_name = "Test Printer"
        self.assertEqual(
            driver._generate_slot_location_name(255, 254),
            "Test Printer - ext. Slot 1",
        )
        self.assertEqual(
            driver._generate_slot_location_name(255, 255),
            "Test Printer - ext. Slot 2",
        )

    def test_processes_new_vir_slot_list_and_ams_ht_ids(self):
        driver, _ = make_driver(auto_import_spools=False)
        driver._process_slots(
            {
                "print": {
                    "ams": {
                        "ams": [
                            {
                                "id": "128",
                                "tray": [
                                    {"id": "0", "tray_type": "PLA"}
                                ],
                            }
                        ]
                    },
                    "vir_slot": [
                        {"id": "254", "tray_type": "PETG"},
                        {"id": "255", "tray_type": "TPU"},
                    ],
                }
            }
        )

        self.assertEqual(
            [slot["slot_index"] for slot in driver._current_slots],
            ["128-0", "255-254", "255-255"],
        )
        self.assertEqual(
            [slot["slot_name"] for slot in driver._current_slots],
            ["AMS HT 1 - Slot 1", "External Tray 1", "External Tray 2"],
        )
        ams_info = driver._build_ams_info(driver._current_slots)
        self.assertEqual(ams_info["slot_count"], 3)
        self.assertTrue(ams_info["external_spool"])


class ReadOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_blocks_direct_setting(self):
        driver, _ = make_driver(read_only=True)
        driver._printer = object()
        dispatched = driver._send_filament_setting(0, 0, {"material_type": "PLA"})
        self.assertFalse(dispatched)

    async def test_read_only_ignores_pending_assignment(self):
        driver, _ = make_driver(read_only=True)
        await driver.assign_pending_spool(42, {"material_type": "PLA"})
        self.assertIsNone(driver._pending)


class AutoImportDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.original_session_makers = {
            DRIVER_MODULE: DRIVER_MODULE.async_session_maker,
            CATALOG_MODULE: CATALOG_MODULE.async_session_maker,
            ENRICHMENT_MODULE: ENRICHMENT_MODULE.async_session_maker,
            SPOOL_SYNC_MODULE: SPOOL_SYNC_MODULE.async_session_maker,
        }
        for module in self.original_session_makers:
            module.async_session_maker = self.sessions

        self.driver, self.events = make_driver(auto_import_spools=True)
        self.driver._loop = asyncio.get_running_loop()
        self.driver._auto_import_lock = asyncio.Lock()
        self.driver._shop_image_lock = asyncio.Lock()
        self.driver._printer_name = "Test Printer"

        async def no_store_search(_product_code, _product_url=None):
            return None

        self.driver._fetch_store_search_image = no_store_search
        self.slot = {
            "slot_index": "0-0",
            "slot_name": "AMS 1 - Slot 1",
            "tray_type": "PLA",
            "tray_color": "FF0000FF",
            "tray_info_idx": "GFA00",
            "tag_uid": "A1B2C3D4E5F60102",
            "tray_uuid": "AABBCCDDEEFF0011AABBCCDDEEFF0011",
            "tray_weight": "1000",
            "remain": 75,
            "present": True,
        }
        self.driver._current_slots = [dict(self.slot)]

        async with self.sessions() as db:
            manufacturer = Manufacturer(name="Bambu Lab")
            color = Color(name="Bambu Red", hex_code="#FF0000")
            db.add_all(
                [
                    manufacturer,
                    color,
                    Printer(
                        id=1,
                        name="Test Printer",
                        driver_key="bambulab",
                        driver_config={},
                    ),
                    SpoolStatus(key="new", label="New", is_system=True),
                    SpoolStatus(key="opened", label="Opened", is_system=True),
                ]
            )
            await db.flush()
            filament = Filament(
                manufacturer_id=manufacturer.id,
                designation="Bambu PLA Basic",
                material_type="PLA",
                diameter_mm=1.75,
            )
            db.add(filament)
            await db.flush()
            self.filament_id = filament.id
            db.add_all(
                [
                    FilamentColor(
                        filament_id=filament.id,
                        color_id=color.id,
                        position=1,
                    ),
                    FilamentPrinterParam(
                        filament_id=filament.id,
                        printer_id=1,
                        param_key="bambu_tray_idx",
                        param_value="GFA00",
                    ),
                ]
            )
            await db.commit()

    async def asyncTearDown(self):
        for module, session_maker in self.original_session_makers.items():
            module.async_session_maker = session_maker
        await self.engine.dispose()

    async def test_import_is_idempotent_and_keeps_custom_rfid_free(self):
        await self.driver._auto_import_rfid_spools([self.slot])
        await self.driver._auto_import_rfid_spools(
            [{**self.slot, "tag_uid": "1020304050607080"}]
        )

        async with self.sessions() as db:
            count = await db.scalar(select(func.count()).select_from(Spool))
            spool = (await db.execute(select(Spool))).scalar_one()

        self.assertEqual(count, 1)
        self.assertEqual(
            spool.external_id,
            "bambulab:AABBCCDDEEFF0011AABBCCDDEEFF0011",
        )
        self.assertEqual(
            spool.custom_fields[SPOOL_SYNC_MODULE.BAMBU_RFID_TAG_1_FIELD],
            "A1B2C3D4E5F60102",
        )
        self.assertNotIn(
            SPOOL_SYNC_MODULE.BAMBU_RFID_TAG_2_FIELD, spool.custom_fields
        )
        self.assertIsNone(spool.rfid_uid)
        self.assertEqual(spool.remaining_weight_g, 750)

    async def test_valid_tray_uuid_imports_without_physical_tag_uid(self):
        slot = {**self.slot, "tag_uid": "0000000000000000"}

        await self.driver._auto_import_rfid_spools([slot])

        async with self.sessions() as db:
            count = await db.scalar(select(func.count()).select_from(Spool))
            spool = (await db.execute(select(Spool))).scalar_one()

        self.assertEqual(count, 1)
        self.assertEqual(
            spool.external_id,
            "bambulab:AABBCCDDEEFF0011AABBCCDDEEFF0011",
        )
        self.assertNotIn(
            SPOOL_SYNC_MODULE.BAMBU_RFID_TAG_1_FIELD, spool.custom_fields
        )

    async def test_physical_tag_uid_is_not_used_as_spool_identity(self):
        await self.driver._auto_import_rfid_spools([self.slot])
        await self.driver._auto_import_rfid_spools(
            [
                {
                    **self.slot,
                    "tray_uuid": "11223344556677881122334455667788",
                }
            ]
        )

        async with self.sessions() as db:
            spools = list(
                (await db.execute(select(Spool).order_by(Spool.id))).scalars()
            )

        self.assertEqual(len(spools), 2)
        self.assertEqual(
            {spool.external_id for spool in spools},
            {
                "bambulab:AABBCCDDEEFF0011AABBCCDDEEFF0011",
                "bambulab:11223344556677881122334455667788",
            },
        )

    async def test_updates_existing_spool_from_bambu_estimate(self):
        await self.driver._auto_import_rfid_spools([self.slot])
        await self.driver._auto_import_rfid_spools(
            [{**self.slot, "remain": 42}]
        )

        async with self.sessions() as db:
            count = await db.scalar(select(func.count()).select_from(Spool))
            spool = (await db.execute(select(Spool))).scalar_one()

        self.assertEqual(count, 1)
        self.assertEqual(spool.remaining_weight_g, 420)

    async def test_invalid_estimate_does_not_overwrite_existing_weight(self):
        await self.driver._auto_import_rfid_spools([self.slot])
        await self.driver._auto_import_rfid_spools(
            [{**self.slot, "tray_weight": "0", "remain": -1}]
        )

        async with self.sessions() as db:
            spool = (await db.execute(select(Spool))).scalar_one()

        self.assertEqual(spool.remaining_weight_g, 750)

    async def test_shop_image_url_is_cached_on_filament_not_physical_spool(self):
        async with self.sessions() as db:
            filament = await db.get(Filament, self.filament_id)
            filament.designation = "Matte - Charcoal (11101)"
            filament.manufacturer_color_name = "Charcoal (11101)"
            filament.shop_url = "https://eu.store.bambulab.com/products/pla-matte"
            await db.commit()

        expected_image = (
            "https://store.bblcdn.eu/s8/default/hash/charcoal.png"
            "__op__resize,m_lfit,w_640__op__format,f_auto__op__quality,q_80"
        )

        async def fake_fetch(_product_url):
            return {"11101": expected_image}

        self.driver._fetch_shop_product_images = fake_fetch
        metadata = await self.driver._cache_shop_image_for_filament(
            self.filament_id
        )

        async with self.sessions() as db:
            filament = await db.get(Filament, self.filament_id)
            spool_custom_fields = [
                spool.custom_fields
                for spool in (await db.execute(select(Spool))).scalars().all()
            ]

        self.assertEqual(metadata["shop_image_url"], expected_image)
        self.assertEqual(
            filament.custom_fields[CATALOG_MODULE.BAMBU_SHOP_IMAGE_URL_FIELD],
            expected_image,
        )
        self.assertEqual(
            filament.custom_fields[CATALOG_MODULE.FILAMENT_IMAGE_URL_FIELD],
            expected_image,
        )
        self.assertEqual(
            filament.custom_fields[CATALOG_MODULE.FILAMENT_IMAGE_PROVIDER_FIELD],
            "bambulab",
        )
        self.assertTrue(
            all(
                CATALOG_MODULE.BAMBU_SHOP_IMAGE_URL_FIELD not in (fields or {})
                for fields in spool_custom_fields
            )
        )

    async def test_pla_pure_without_shop_url_uses_material_family_source(self):
        async with self.sessions() as db:
            filament = await db.get(Filament, self.filament_id)
            filament.designation = "Pure - Absolute Black (17101)"
            filament.manufacturer_color_name = "Absolute Black (17101)"
            filament.material_type = "PLA"
            filament.material_subgroup = "pure"
            filament.custom_fields = {
                "bambu_material_id": "GFA19",
                "bambu_detailed_filament_type": "PLA Pure",
            }
            await db.commit()

        expected_source = "https://eu.store.bambulab.com/products/pla-pure"
        expected_image = "https://store.bblcdn.eu/pure-white.jpg"

        async def fake_fetch(product_url):
            self.assertEqual(product_url, expected_source)
            return {"17101": expected_image}

        self.driver._fetch_shop_product_images = fake_fetch
        metadata = await self.driver._cache_shop_image_for_filament(
            self.filament_id
        )

        async with self.sessions() as db:
            filament = await db.get(Filament, self.filament_id)

        self.assertEqual(metadata["shop_image_url"], expected_image)
        self.assertEqual(filament.shop_url, expected_source)
        self.assertEqual(
            filament.custom_fields[CATALOG_MODULE.FILAMENT_IMAGE_URL_FIELD],
            expected_image,
        )

    async def test_filascan_pla_pure_migrates_palette_to_store_search_media(self):
        palette_image = (
            "https://store.bblcdn.eu/s8/default/"
            "5939cb6c15514a7bb404738123d817c2/Pure_White.jpg"
        )
        async with self.sessions() as db:
            filament = await db.get(Filament, self.filament_id)
            filament.designation = "PLA Pure · Pure White [GFA19-A19-W00]"
            filament.manufacturer_color_name = "Pure White"
            filament.material_type = "PLA"
            filament.material_subgroup = "PLA Pure"
            filament.custom_fields = {
                "bambu_material_id": "GFA19",
                "bambu_variant_id": "A19-W00",
                "bambu_color_code": "17100",
                "bambu_detailed_filament_type": "PLA Pure",
                CATALOG_MODULE.FILAMENT_IMAGE_URL_FIELD: palette_image,
                CATALOG_MODULE.BAMBU_SHOP_IMAGE_URL_FIELD: palette_image,
                CATALOG_MODULE.FILAMENT_IMAGE_CHECKED_AT_FIELD: (
                    "2026-08-09T10:00:00+00:00"
                ),
            }
            await db.commit()

        expected_image = "https://store.bblcdn.eu/product/pure-white-spool.jpg"
        expected_source = (
            "https://eu.store.bambulab.com/de/products/pla-pure"
            "?id=738891519803023418"
        )

        async def search_image(product_code, product_url):
            self.assertEqual(product_code, "17100")
            self.assertEqual(
                product_url,
                "https://eu.store.bambulab.com/products/pla-pure",
            )
            return {
                "shop_image_url": expected_image,
                "shop_source_url": expected_source,
            }

        async def unexpected_fetch(_product_url):
            self.fail("successful store search must not scrape the product page")

        self.driver._fetch_store_search_image = search_image
        self.driver._fetch_shop_product_images = unexpected_fetch
        metadata = await self.driver._cache_shop_image_for_filament(
            self.filament_id
        )

        async with self.sessions() as db:
            filament = await db.get(Filament, self.filament_id)

        self.assertEqual(filament.designation, "Pure - Pure White (17100)")
        self.assertEqual(filament.manufacturer_color_name, "Pure White (17100)")
        self.assertEqual(filament.material_subgroup, "pure")
        self.assertEqual(metadata["bambu_product_code"], "17100")
        self.assertEqual(metadata["shop_image_url"], expected_image)
        self.assertEqual(
            filament.custom_fields[CATALOG_MODULE.FILAMENT_IMAGE_URL_FIELD],
            expected_image,
        )
        self.assertEqual(
            filament.custom_fields[CATALOG_MODULE.FILAMENT_IMAGE_SOURCE_URL_FIELD],
            expected_source,
        )
        self.assertEqual(
            filament.custom_fields[
                CATALOG_MODULE.BAMBU_IMAGE_RESOLVER_VERSION_FIELD
            ],
            CATALOG_MODULE.STORE_SEARCH_RESOLVER_VERSION,
        )

    async def test_inventory_scan_resolves_only_bambu_filaments_with_spools(self):
        async with self.sessions() as db:
            manufacturer = (
                await db.execute(
                    select(Manufacturer).where(Manufacturer.name == "Bambu Lab")
                )
            ).scalar_one()
            filament = await db.get(Filament, self.filament_id)
            filament.designation = "Matte - Charcoal (11101)"
            filament.shop_url = "https://eu.store.bambulab.com/products/pla-matte"
            unopened = Filament(
                manufacturer_id=manufacturer.id,
                designation="PLA Basic - Red (10200)",
                material_type="PLA",
                diameter_mm=1.75,
                shop_url="https://eu.store.bambulab.com/products/pla-basic-filament",
            )
            db.add(unopened)
            await db.flush()
            opened_status = (
                await db.execute(
                    select(SpoolStatus).where(SpoolStatus.key == "opened")
                )
            ).scalar_one()
            db.add(Spool(filament_id=filament.id, status_id=opened_status.id))
            await db.commit()
            unopened_id = unopened.id

        async def fake_fetch(_product_url):
            return {
                "11101": "https://store.bblcdn.eu/charcoal.png",
                "10200": "https://store.bblcdn.eu/red.png",
            }

        self.driver._fetch_shop_product_images = fake_fetch
        stats = await self.driver._refresh_inventory_shop_images()

        async with self.sessions() as db:
            cached = await db.get(Filament, self.filament_id)
            unopened = await db.get(Filament, unopened_id)

        self.assertEqual(stats, {"filaments": 1, "images": 1})
        self.assertEqual(
            cached.custom_fields[CATALOG_MODULE.FILAMENT_IMAGE_PROVIDER_FIELD],
            "bambulab",
        )
        self.assertIsNone(unopened.custom_fields)

    async def test_skips_spool_when_no_matching_filament_exists(self):
        unknown = {
            **self.slot,
            "tray_info_idx": "UNKNOWN",
            "tray_type": "ABS",
            "tray_color": "00FF00FF",
            "tag_uid": "8899AABBCCDDEEFF",
            "tray_uuid": "11223344556677881122334455667788",
        }
        await self.driver._auto_import_rfid_spools([unknown])

        async with self.sessions() as db:
            count = await db.scalar(select(func.count()).select_from(Spool))
        self.assertEqual(count, 0)

    async def test_prefers_coded_filamentdb_record_for_charcoal(self):
        async with self.sessions() as db:
            manufacturer = (
                await db.execute(
                    select(Manufacturer).where(Manufacturer.name == "Bambu Lab")
                )
            ).scalar_one()
            black = Color(name="Black", hex_code="#000000")
            db.add(black)
            await db.flush()
            legacy = Filament(
                manufacturer_id=manufacturer.id,
                designation="Matte - Charcoal (11101)",
                manufacturer_color_name="Charcoal (11101)",
                material_type="PLA",
                material_subgroup="matte",
                diameter_mm=1.75,
                custom_fields={"filamentdb_id": 149},
            )
            duplicate = Filament(
                manufacturer_id=manufacturer.id,
                designation="PLA Matte Charcoal",
                manufacturer_color_name="Matte Charcoal",
                material_type="PLA",
                material_subgroup="matte",
                diameter_mm=1.75,
                custom_fields={"filamentdb_id": 24540},
            )
            db.add_all([legacy, duplicate])
            await db.flush()
            db.add_all(
                [
                    FilamentColor(
                        filament_id=legacy.id, color_id=black.id, position=1
                    ),
                    FilamentColor(
                        filament_id=duplicate.id, color_id=black.id, position=1
                    ),
                ]
            )
            await db.commit()

            match = await self.driver._find_matching_filament(
                db,
                {
                    "tray_info_idx": "GFA01",
                    "tray_type": "PLA",
                    "tray_sub_brands": "PLA Matte",
                    "tray_color": "000000FF",
                },
            )

        self.assertIsNotNone(match)
        self.assertEqual(match.custom_fields["filamentdb_id"], 149)

    async def test_collapses_same_silk_plus_product_code(self):
        async with self.sessions() as db:
            manufacturer = (
                await db.execute(
                    select(Manufacturer).where(Manufacturer.name == "Bambu Lab")
                )
            ).scalar_one()
            silver = Color(name="Silver", hex_code="#C8C8C8")
            db.add(silver)
            await db.flush()
            filaments = []
            for fdb_id, designation in (
                (22173, "Silk Plus - Silver"),
                (2850, "Silk Plus - Silver (13109)"),
                (22174, "Silk Plus - Silver (13109)"),
            ):
                filament = Filament(
                    manufacturer_id=manufacturer.id,
                    designation=designation,
                    manufacturer_color_name=designation.removeprefix("Silk Plus - "),
                    material_type="PLA",
                    material_subgroup="silk-plus",
                    diameter_mm=1.75,
                    custom_fields={"filamentdb_id": fdb_id},
                )
                db.add(filament)
                filaments.append(filament)
            await db.flush()
            db.add_all(
                [
                    FilamentColor(
                        filament_id=filament.id,
                        color_id=silver.id,
                        position=1,
                    )
                    for filament in filaments
                ]
            )
            await db.commit()

            match = await self.driver._find_matching_filament(
                db,
                {
                    "tray_info_idx": "GFA06",
                    "tray_type": "PLA",
                    "tray_sub_brands": "PLA Silk+",
                    "tray_color": "C8C8C8FF",
                },
            )

        self.assertIsNotNone(match)
        self.assertEqual(match.custom_fields["filamentdb_id"], 2850)

    async def test_different_product_codes_remain_ambiguous(self):
        async with self.sessions() as db:
            manufacturer = (
                await db.execute(
                    select(Manufacturer).where(Manufacturer.name == "Bambu Lab")
                )
            ).scalar_one()
            white = Color(name="White", hex_code="#FFFFFF")
            db.add(white)
            await db.flush()
            filaments = []
            for fdb_id, designation in (
                (76, "Jade White (10100)"),
                (999, "Other White (10105)"),
            ):
                filament = Filament(
                    manufacturer_id=manufacturer.id,
                    designation=designation,
                    manufacturer_color_name=designation,
                    material_type="PLA",
                    material_subgroup="basic",
                    diameter_mm=1.75,
                    custom_fields={"filamentdb_id": fdb_id},
                )
                db.add(filament)
                filaments.append(filament)
            await db.flush()
            db.add_all(
                [
                    FilamentColor(
                        filament_id=filament.id,
                        color_id=white.id,
                        position=1,
                    )
                    for filament in filaments
                ]
            )
            await db.commit()

            match = await self.driver._find_matching_filament(
                db,
                {
                    "tray_info_idx": "GFA00",
                    "tray_type": "PLA",
                    "tray_sub_brands": "PLA Basic",
                    "tray_color": "FFFFFFFF",
                },
            )

        self.assertIsNone(match)


if __name__ == "__main__":
    unittest.main()
