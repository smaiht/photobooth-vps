import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import runtime_config


class RuntimeConfigTests(unittest.TestCase):
    def test_runtime_paths_come_only_from_config(self):
        with patch.object(
            runtime_config,
            "CONFIG",
            {
                "yadisk_folder": "Кафе",
                "yadisk_control_folder": "custom/control",
                "archive_delivery": {
                    "telegram": True,
                    "vk": False,
                },
            },
        ):
            self.assertEqual(runtime_config.yadisk_folder(), "Кафе")
            self.assertEqual(runtime_config.control_folder(), "custom/control")
            self.assertEqual(
                runtime_config.archive_delivery_providers(),
                ("telegram",),
            )

        with patch.object(runtime_config, "CONFIG", {}):
            with self.assertRaisesRegex(RuntimeError, "yadisk_folder"):
                runtime_config.yadisk_folder()
            with self.assertRaisesRegex(RuntimeError, "yadisk_control_folder"):
                runtime_config.control_folder()

    def test_archive_delivery_requires_two_explicit_booleans(self):
        with patch.object(runtime_config, "CONFIG", {}):
            with self.assertRaisesRegex(RuntimeError, "archive_delivery"):
                runtime_config.archive_delivery_providers()

        for settings, missing_key in (
            ({"telegram": True}, "vk"),
            ({"telegram": "yes", "vk": True}, "telegram"),
        ):
            with self.subTest(settings=settings), patch.object(
                runtime_config,
                "CONFIG",
                {"archive_delivery": settings},
            ):
                with self.assertRaisesRegex(RuntimeError, missing_key):
                    runtime_config.archive_delivery_providers()

    def test_archive_delivery_can_enable_both_or_neither(self):
        with patch.object(
            runtime_config,
            "CONFIG",
            {"archive_delivery": {"telegram": True, "vk": True}},
        ):
            self.assertEqual(
                runtime_config.archive_delivery_providers(),
                ("telegram", "vk"),
            )
        with patch.object(
            runtime_config,
            "CONFIG",
            {"archive_delivery": {"telegram": False, "vk": False}},
        ):
            self.assertEqual(runtime_config.archive_delivery_providers(), ())

    def test_event_update_is_atomic_and_preserves_other_settings(self):
        with TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config_vps.json"
            config_path.write_text(
                json.dumps({
                    "yadisk_folder": "old-event",
                    "yadisk_control_folder": "control",
                    "archive_delivery": {
                        "telegram": True,
                        "vk": False,
                    },
                }),
                encoding="utf-8",
            )
            config = {}
            with patch.object(
                runtime_config,
                "CONFIG_PATH",
                config_path,
            ), patch.object(
                runtime_config,
                "CONFIG",
                config,
            ):
                runtime_config.save_event("2026-08-17 Свадьба")

            saved = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(saved["yadisk_folder"], "2026-08-17 Свадьба")
        self.assertEqual(saved["yadisk_control_folder"], "control")
        self.assertEqual(
            saved["archive_delivery"],
            {"telegram": True, "vk": False},
        )
        self.assertEqual(config["yadisk_folder"], "2026-08-17 Свадьба")


if __name__ == "__main__":
    unittest.main()
