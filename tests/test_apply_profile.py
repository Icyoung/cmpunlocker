#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "driver" / "apply_profile.py"
spec = importlib.util.spec_from_file_location("apply_profile", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

SOURCE = """
            if (devId == SEC2_POSTBL_TIMING_CMP_170HX_8GB_PCI_DEVICE_ID)
            {
                cfg1Value = 0x02779000U;
                lmrValue  = 0x0000020BU;
            }
            else
            {
                cfg1Value = 0x02669000U;
                lmrValue  = 0x0000028AU;
            }

            NvU64 targetFbBytes = (devId == SEC2_POSTBL_TIMING_CMP_170HX_8GB_PCI_DEVICE_ID)
                                    ? 0x0000001000000000ULL
                                    : 0x0000000A00000000ULL;
"""


class ApplyProfileTests(unittest.TestCase):
    def apply(self, profile: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "kernel_gsp.c"
            path.write_text(SOURCE, encoding="utf-8")
            module.apply_profile(path, profile)
            return path.read_text(encoding="utf-8")

    def test_stable_10gb_profile_remains_40gb(self) -> None:
        text = self.apply("10gb")
        self.assertEqual(text, SOURCE)
        self.assertIn("cfg1Value = 0x02669000U;", text)
        self.assertIn("lmrValue  = 0x0000028AU;", text)
        self.assertIn(": 0x0000000A00000000ULL;", text)

    def test_experimental_profile_rewrites_all_compiled_2082_values(self) -> None:
        text = self.apply("10gb80")
        self.assertIn("cfg1Value = 0x02779000U;", text)
        self.assertIn("lmrValue  = 0x0000028BU;", text)
        self.assertIn(": 0x0000001400000000ULL;", text)
        self.assertNotIn("0x0000028AU", text)
        self.assertNotIn("0x0000000A00000000ULL;", text)

    def test_8gb_geometry_is_unchanged_in_experimental_build(self) -> None:
        text = self.apply("mixed80")
        self.assertIn("? 0x0000001000000000ULL", text)
        self.assertIn("lmrValue  = 0x0000020BU;", text)

    def test_experimental_rewrite_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "kernel_gsp.c"
            path.write_text(SOURCE, encoding="utf-8")
            module.apply_profile(path, "10gb80")
            first = path.read_bytes()
            module.apply_profile(path, "10gb80")
            self.assertEqual(path.read_bytes(), first)

    def test_missing_markers_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "kernel_gsp.c"
            path.write_text("/* wrong source */\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                module.apply_profile(path, "10gb80")

    def test_duplicate_geometry_blocks_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "kernel_gsp.c"
            path.write_text(SOURCE + SOURCE, encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "found 2"):
                module.apply_profile(path, "10gb80")


if __name__ == "__main__":
    unittest.main()
