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

    // CMP_MEM_EARLY_WRITE marker for the P1a early-write block
    if (devId == SEC2_POSTBL_TIMING_CMP_170HX_8GB_PCI_DEVICE_ID)
    {
        cfg1Target = 0x02779000U;
        lmrTarget  = 0x0000020BU;
    }
    else
    {
        cfg1Target = 0x02669000U;
        lmrTarget  = 0x0000028AU;
    }
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
        self.assertIn("cfg1Target = 0x02669000U;", text)
        self.assertIn("lmrTarget  = 0x0000028AU;", text)

    def test_experimental_profile_rewrites_all_compiled_2082_values(self) -> None:
        text = self.apply("10gb80")
        self.assertIn("cfg1Value = 0x02779000U;", text)
        self.assertIn("lmrValue  = 0x0000028BU;", text)
        self.assertIn(": 0x0000001400000000ULL;", text)
        self.assertIn("cfg1Target = 0x02779000U;", text)
        self.assertIn("lmrTarget  = 0x0000028BU;", text)
        self.assertNotIn("0x0000028AU", text)
        self.assertNotIn("0x0000000A00000000ULL;", text)

    def test_8gb_geometry_is_unchanged_in_experimental_build(self) -> None:
        text = self.apply("mixed80")
        self.assertIn("? 0x0000001000000000ULL", text)
        self.assertIn("lmrValue  = 0x0000020BU;", text)
        self.assertIn("lmrTarget  = 0x0000020BU;", text)

    def test_early_write_block_without_marker_is_left_alone(self) -> None:
        source_no_p1a = SOURCE.replace(
            "    // CMP_MEM_EARLY_WRITE marker for the P1a early-write block\n", ""
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "kernel_gsp.c"
            path.write_text(source_no_p1a, encoding="utf-8")
            module.apply_profile(path, "10gb80")
            text = path.read_text(encoding="utf-8")
            # no P1a marker -> early-write constants untouched
            self.assertIn("cfg1Target = 0x02669000U;", text)
            self.assertIn("lmrTarget  = 0x0000028AU;", text)

    def test_mismatched_early_write_block_fails_closed(self) -> None:
        # break the fixed 8 GB branch of the P1a block; the else branch accepts
        # any hex value by design (it is the rewritten slot)
        broken = SOURCE.replace("cfg1Target = 0x02779000U;", "cfg1Target = 0x02779010U;")
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "kernel_gsp.c"
            path.write_text(broken, encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "found 0"):
                module.apply_profile(path, "10gb80")

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


    def test_real_sec2_patch_new_side_rewrites(self) -> None:
        patch_path = ROOT / "driver" / "patches" / "sec2-postbl-plm-ss-cfg.patch"
        lines = patch_path.read_text(encoding="utf-8").splitlines()
        in_kernel_gsp = False
        new_side: list[str] = []
        for line in lines:
            if line.startswith("+++ b/"):
                in_kernel_gsp = line.startswith(
                    "+++ b/src/nvidia/src/kernel/gpu/gsp/kernel_gsp.c"
                )
                continue
            if line.startswith("diff -Naur ") or line.startswith("--- a/"):
                if line.startswith("diff -Naur "):
                    in_kernel_gsp = False
                continue
            if in_kernel_gsp and line and line[0] in " +":
                new_side.append(line[1:])

        reconstructed = "\n".join(new_side) + "\n"
        self.assertIn("cfg1Value = 0x02669000U;", reconstructed)
        self.assertIn("lmrValue  = 0x0000028AU;", reconstructed)
        self.assertIn(": 0x0000000A00000000ULL;", reconstructed)

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "kernel_gsp.c"
            path.write_text(reconstructed, encoding="utf-8")
            module.apply_profile(path, "10gb80")
            rewritten = path.read_text(encoding="utf-8")
            self.assertIn("lmrValue  = 0x0000028BU;", rewritten)
            self.assertIn(": 0x0000001400000000ULL;", rewritten)

    def test_real_p1a_patch_ships_stable_defaults_and_rewrites(self) -> None:
        def reconstruct(patch_name: str) -> str:
            patch_path = ROOT / "driver" / "patches" / patch_name
            lines = patch_path.read_text(encoding="utf-8").splitlines()
            in_kernel_gsp = False
            new_side: list[str] = []
            for line in lines:
                if line.startswith("+++ b/"):
                    in_kernel_gsp = line.startswith(
                        "+++ b/src/nvidia/src/kernel/gpu/gsp/kernel_gsp.c"
                    )
                    continue
                if line.startswith("diff -Naur ") or line.startswith("--- a/"):
                    if line.startswith("diff -Naur "):
                        in_kernel_gsp = False
                    continue
                if in_kernel_gsp and line and line[0] in " +":
                    new_side.append(line[1:])
            return "\n".join(new_side) + "\n"

        p1a = reconstruct("early-lmr-write-p1a.patch")
        # shipped defaults must be the stable 40 GiB geometry (never the
        # experimental 80 GiB values: an unprofiled build must fail safe)
        self.assertIn("CMP_MEM_EARLY_WRITE", p1a)
        self.assertIn("cfg1Target = 0x02669000U;", p1a)
        self.assertIn("lmrTarget  = 0x0000028AU;", p1a)
        self.assertNotIn("0x0000028BU", p1a)

        combined = reconstruct("sec2-postbl-plm-ss-cfg.patch") + p1a
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "kernel_gsp.c"

            path.write_text(combined, encoding="utf-8")
            module.apply_profile(path, "10gb")
            stable = path.read_text(encoding="utf-8")
            self.assertIn("cfg1Target = 0x02669000U;", stable)
            self.assertIn("lmrTarget  = 0x0000028AU;", stable)
            self.assertNotIn("0x0000028BU", stable)
            self.assertNotIn("0x0000001400000000ULL", stable)

            path.write_text(combined, encoding="utf-8")
            module.apply_profile(path, "10gb80")
            experimental = path.read_text(encoding="utf-8")
            self.assertIn("cfg1Target = 0x02779000U;", experimental)
            self.assertIn("lmrTarget  = 0x0000028BU;", experimental)
            self.assertIn("lmrValue  = 0x0000028BU;", experimental)
            self.assertNotIn("0x0000028AU", experimental)

    def test_duplicate_geometry_blocks_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "kernel_gsp.c"
            path.write_text(SOURCE + SOURCE, encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "found 2"):
                module.apply_profile(path, "10gb80")


if __name__ == "__main__":
    unittest.main()
