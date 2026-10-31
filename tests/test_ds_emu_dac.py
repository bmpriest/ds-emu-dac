#!/usr/bin/env python3
"""Focused tests for ds_emu_dac, run with: python3 -m unittest discover tests"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import ds_emu_dac as dac

ROOT = Path(__file__).parent.parent
METROID = ROOT / "metroid"


class BuildTests(unittest.TestCase):
    def build(self, inputs, **kw):
        # Drive the same code path the CLI does, minus argparse.
        files = dac.collect_inputs(inputs)
        return dac.build_dat(files, **kw)

    def test_metroid_folder_builds_six_roms(self):
        _dat, games, skipped, _layouts = self.build([str(METROID)])
        self.assertEqual(skipped, [])
        self.assertEqual(len(games), 6)
        for rom_id, codes in games.items():
            self.assertTrue(rom_id.startswith(("AMHE", "AMHJ", "AMHP")))
            self.assertEqual([c[0] for c in codes], [dac.CODE_NAME_PLAIN])

    def test_metroid_main_section_is_reguarded_off_arm7(self):
        _dat, games, _skipped, _layouts = self.build([str(METROID)])
        words = games["AMHE-E18D1857"][0][1]
        # Guard is the != test on the hook destination, not the ARM7
        # RTC-init address the upstream code used.
        self.assertEqual(words[0] >> 24, 0x62)          # 6xxxxxxx, addr 0x02001000
        self.assertEqual(words[0] & 0x0FFFFFFF, 0x02001000)

    def test_no_arm7_writes_survive(self):
        _dat, games, _skipped, _layouts = self.build([str(METROID)])
        for codes in games.values():
            for _name, words, _src in codes:
                pairs = list(zip(words[::2], words[1::2]))
                for i, _span in dac.opcode_positions(pairs):
                    a, _b = pairs[i]
                    if (a >> 28) in (0x0, 0x1, 0x2, 0xE):
                        self.assertFalse(dac.is_arm7_addr(a), f"{a:08X}")

    def test_single_file_input(self):
        one = sorted(METROID.glob("*.txt"))[0]
        _dat, games, _skipped, _layouts = self.build([str(one)])
        self.assertEqual(len(games), 1)

    def test_upstream_dat_beside_codes_is_ignored(self):
        # metroid/ contains an upstream usrcheat.dat; collect_inputs must
        # not try to parse it as a code file.
        files = dac.collect_inputs([str(METROID)])
        self.assertTrue(all(f.suffix == ".txt" for f, _root in files))
        self.assertEqual(len(files), 6)

    def test_duplicate_rom_and_name_keeps_both_discretely(self):
        # Two revisions of the same ROM's code (a local copy beside a
        # fresh download) are two control schemes: both are kept,
        # under discrete names, so the player can choose.
        one = sorted(METROID.glob("*.txt"))[0]
        text = one.read_text()
        lines = text.splitlines()
        a, b = lines[-2].split()   # a branch write in the kept ARM9 tail
        lines[-2] = f"{a} {b[:-1]}{'0' if b[-1] != '0' else '1'}"
        with tempfile.TemporaryDirectory() as td:
            other = Path(td) / one.name
            other.write_text("\n".join(lines) + "\n")
            _dat, games, skipped, _layouts = self.build([str(one), str(other)])
        codes = [c for cs in games.values() for c in cs]
        self.assertEqual(len(codes), 2)
        self.assertNotEqual(codes[0][0], codes[1][0])
        self.assertTrue(all(c[0].startswith("CPAD") for c in codes))
        self.assertEqual(skipped, [])

    def test_multiple_schemes_ship_in_one_exclusive_category(self):
        # Alternative schemes for one ROM must land in a single
        # exclusive R4 category, so the frontend treats them as radio
        # buttons instead of running two hooks over the same addresses.
        import struct
        one = sorted(METROID.glob("*.txt"))[0]
        lines = one.read_text().splitlines()
        a, b = lines[-2].split()
        lines[-2] = f"{a} {b[:-1]}{'0' if b[-1] != '0' else '1'}"
        with tempfile.TemporaryDirectory() as td:
            other = Path(td) / one.name
            other.write_text("\n".join(lines) + "\n")
            dat, _games, _skipped, _layouts = self.build([str(one), str(other)])
        cat = dac.make_category_record(
            "Analog control schemes", 2,
            "Exclusive: enable one scheme. It takes over when you leave this menu.")
        self.assertIn(cat, dat)
        # Single-scheme ROMs stay ungrouped.
        single, _g, _s, _l = self.build([str(one)])
        self.assertNotIn(struct.pack("<I", 1 | (1 << 28) | (1 << 24)), single)

    def test_control_map_extracts_hooks(self):
        _dat, games, _skipped, _layouts = self.build([str(METROID)])
        words = games["AMHE-E18D1857"][0][1]
        c = dac.extract_controls(words)
        self.assertTrue(c["cpad_analog"])
        self.assertIn("0x02024314", c["sub_hooks"])   # camera-turning hook
        self.assertTrue(any(h["dest"] == "0x02001000" for h in c["hook_installs"]))

    def test_code_record_pads_on_word_boundary(self):
        # A name whose name+desc bytes land exactly on a word boundary
        # needs no pad word; an extra one shifts the reader's word
        # count and corrupts the record (DSperate rejects it).
        import struct
        rec = dac.make_code_record("CPAD (metroid)", [0xD2000000, 0])
        total, = struct.unpack_from("<I", rec, 0)
        self.assertEqual(total, len(rec) // 4 - 1)  # total counts everything after itself
        words_at = rec.index(struct.pack("<I", 2))
        self.assertEqual(struct.unpack_from("<I", rec, words_at)[0], 2)

    def test_layout_detection_and_override(self):
        _dat, _games, _skipped, layouts = self.build([str(METROID)])
        self.assertEqual(layouts["AMHE-E18D1857"], "packed")   # no relay source in the fixture
        _dat, _games, _skipped, layouts = self.build(
            [str(METROID)], layout_overrides={"AMHE": "extended"})
        self.assertEqual(layouts["AMHE-E18D1857"], "extended")

    def test_exclude_drops_a_source_revision(self):
        # A revision known not to work on an emulator can be kept out
        # of a build without deleting it from disk.
        files = dac.collect_inputs([str(METROID)])
        kept = [(f, r) for f, r in files if "AMHE-E18D1857" not in str(f)]
        self.assertEqual(len(kept), 5)

    def test_forced_full_gamecode(self):
        self.assertEqual(dac.code_name_for("AMHE-E18D1857 (USA v1.0).txt", {"AMHE"}), dac.CODE_NAME_FULL)
        self.assertEqual(dac.code_name_for("AMHE-E18D1857 (USA v1.0).txt"), dac.CODE_NAME_PLAIN)

    def test_variant_names_are_canonical(self):
        self.assertEqual(dac.code_name_for("X (USA) [with CStick support].txt"), dac.CODE_NAME_FULL)
        self.assertEqual(dac.code_name_for("X (USA) [with Nub stick, ZL and ZR].txt"), dac.CODE_NAME_FULL)
        self.assertEqual(dac.code_name_for("X (USA).txt"), dac.CODE_NAME_PLAIN)


if __name__ == "__main__":
    unittest.main()
