#!/usr/bin/env python3

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent / "tools"
ADAPTER_PATH = TOOLS / "antigravity-pty-broker.py"
ADAPTER_SPEC = importlib.util.spec_from_file_location(
    "antigravity_pty_broker_tested", ADAPTER_PATH
)
adapter = importlib.util.module_from_spec(ADAPTER_SPEC)
ADAPTER_SPEC.loader.exec_module(adapter)
RUNTIMES_SPEC = importlib.util.spec_from_file_location(
    "runtimes_tested", TOOLS / "runtimes.py"
)
runtimes = importlib.util.module_from_spec(RUNTIMES_SPEC)
sys.modules[RUNTIMES_SPEC.name] = runtimes
RUNTIMES_SPEC.loader.exec_module(runtimes)


class AntigravityAdapterTest(unittest.TestCase):
    def test_discovers_conversation_id_from_process_open_file_targets(self):
        conversation_id = "658e78b0-9cff-48ec-b427-b4885bdfcd50"
        with tempfile.TemporaryDirectory() as directory:
            proc_root = Path(directory)
            fd_dir = proc_root / "42" / "fd"
            fd_dir.mkdir(parents=True)
            os.symlink(
                adapter.PRESENCE_DIR / f"{conversation_id}.lock",
                fd_dir / "7",
            )
            os.symlink(
                "/home/test/.gemini/antigravity-cli/brain/8eecaad3-86c4-468d-9210-15f5381a7c61/transcript.jsonl",
                fd_dir / "8",
            )
            self.assertEqual(
                adapter.discover_conversation_id(42, proc_root), conversation_id
            )
            os.symlink(
                adapter.PRESENCE_DIR / "8eecaad3-86c4-468d-9210-15f5381a7c61.lock",
                fd_dir / "9",
            )
            with self.assertRaises(RuntimeError):
                adapter.discover_conversation_id(42, proc_root)

    def test_fixed_settings_require_the_gemini_provider(self):
        self.assertEqual(
            adapter.SETTINGS_PATH,
            Path.home() / ".gemini/antigravity-cli/settings.json",
        )
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            with mock.patch.object(adapter, "SETTINGS_PATH", settings):
                for content in (None, "not json", json.dumps({"modelProvider": "other"})):
                    with self.subTest(content=content):
                        settings.unlink(missing_ok=True)
                        if content is not None:
                            settings.write_text(content)
                        with self.assertRaises(RuntimeError):
                            adapter.validate_settings()
                settings.write_text(json.dumps({"modelProvider": "gemini"}))
                self.assertIsNone(adapter.validate_settings())
        self.assertEqual(adapter.PROFILE.required_command, str(Path.home() / ".local/bin/agy"))

    def test_runtime_entry_loads_antigravity_adapter_contract(self):
        runtime = runtimes.RUNTIMES["antigravity"]
        self.assertEqual(runtime.adapter_file, "antigravity-pty-broker.py")
        self.assertIsNone(runtime.config_env_var)
        self.assertEqual(runtime.session_discovery_mode, "pty_advertisement")
        self.assertEqual(runtime.cli_command, (str(Path.home() / ".local/bin/agy"),))


if __name__ == "__main__":
    unittest.main()
