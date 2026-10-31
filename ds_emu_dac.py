#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""ds-emu-dac: DS analog-controls cheat codes for emulators.

Turns shocoman's Circle Pad (CPAD) Action Replay codes -- written for a
real 3DS, where an ARM7/RTCom relay uploads code to the ARM11 and copies
stick state into guest RAM at 0x027FFDF0 -- into codes an emulator can
run directly. The emulator feeds 0x027FFDF0 itself (DSperate:
src/core/input/input_log.cpp), so the ARM7/RTCom half of each code is
stripped and only the ARM9 game hook ships. See README.md.

Commands:
  download [--out DIR] [--repo URL] [--ref REF]
      Fetch shocoman's repo and collect every action_replay_codes
      folder into DIR/<game>/ (the repo root's folder becomes "sm64ds").
  build INPUT... --out OUT.dat [--manifest MANIFEST.json]
      Build a usrcheat.dat from .txt code files. Each INPUT may be a
      single .txt file, a folder (searched recursively for .txt), or a
      folder of such folders. Non-.txt files (e.g. an upstream
      usrcheat.dat sitting next to the codes) are ignored with a note.
      A manifest JSON listing every game, ROM id and variant written
      goes next to the .dat unless --manifest says otherwise.
"""
import argparse
import json
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from collections import OrderedDict
from pathlib import Path

ARM7_LO, ARM7_HI = 0x03000000, 0x03FFFFFF
REPO_DEFAULT = "https://github.com/shocoman/Analog-Controls-for-NDS-Games-on-3DS"
CODE_NAME_PLAIN = "CPAD"
CODE_NAME_FULL = "CPAD [with Nub stick, ZL and ZR]"


# ---------------------------------------------------------------- codes

def parse_pairs(text):
    pairs = []
    for line in text.splitlines():
        cols = line.split("#")[0].split()
        if len(cols) != 2:
            continue
        pairs.append((int(cols[0], 16), int(cols[1], 16)))
    return pairs


def split_sections(pairs):
    """Split on D2000000 block terminators. Some upstream files are a
    single block with no terminator at all (it ends in D0000000, the
    conditional-stack pop); that trailing block is kept and the build
    re-terminates it with D2000000, which is a no-op at the end."""
    sections, cur = [], []
    i = 0
    while i < len(pairs):
        a, b = pairs[i]
        cur.append((a, b))
        i += 1
        if a == 0xD2000000:
            sections.append(cur)
            cur = []
        elif (a >> 28) == 0xE:   # step over the bulk copy's data words,
            span = (b + 7) // 8  # which can themselves look like D2000000
            cur.extend(pairs[i:i + span])
            i += span
    if cur:
        sections.append(cur + [(0xD2000000, 0)])
    return sections


def opcode_positions(body):
    """Yield (index, span) for each opcode pair, stepping over E data."""
    i = 0
    while i < len(body):
        a, b = body[i]
        span = 1 + ((b + 7) // 8 if (a >> 28) == 0xE else 0)
        yield i, span
        i += span


def is_arm7_addr(a):
    addr = a & 0x0FFFFFFF
    # ARM7 code lives in its WRAM (0x03000000-0x03FFFFFF, including the
    # shared-WRAM mirrors at 0x037C0000+) and, seen from the ARM9 side,
    # in shared WRAM at 0x027C0000-0x027FFFFF. A bulk copy or write into
    # either is the RTCom relay being installed, never game hook code
    # (hooks sit in low main RAM). Runtime data at 0x027FFDF0 is written
    # by the hook itself, not by cheat pairs, so it is safe to strip on
    # this rule.
    return (ARM7_LO <= addr <= ARM7_HI) or (0x027C0000 <= addr <= 0x027FFFFF)


def strip_section(body, name):
    """Remove ARM7 chunks/writes from one section body (no D2 pair).

    Returns the kept body, or None if nothing ARM9-side remains. A
    section guarded on an ARM7 address gets its guard rewritten to
    "install while the hook branch is not yet written" (a != test on
    the section's first branch write), so the ARM9 install still runs
    exactly once without the RTC-init condition it no longer needs.
    """
    drop = set()
    for i, span in opcode_positions(body):
        a, b = body[i]
        hi = a >> 28
        if hi == 0xE and is_arm7_addr(a):
            drop.update(range(i, i + span))          # ARM7 bulk copy + data
        elif hi in (0x0, 0x1, 0x2) and is_arm7_addr(a):
            drop.add(i)                              # ARM7 branch / erase
    if not drop:
        return body
    kept = [p for i, p in enumerate(body) if i not in drop]
    # What remains after the guard must be more than the guard itself.
    if len(kept) <= 1:
        return None
    first_a, _ = kept[0]
    if (first_a >> 28) in (0x3, 0x4, 0x5, 0x6, 0x7, 0x8, 0x9, 0xA) and is_arm7_addr(first_a):
        # Re-guard on an opcode position only (E data holds instructions
        # that look like branch writes). Prefer the section's hook branch;
        # sections whose hook self-installs through its init code fall
        # back to "install while the hook's first word is not there yet".
        guard = fallback = None
        for i, _span in opcode_positions(kept):
            a, b = kept[i]
            if (a >> 28) == 0x0 and (b >> 24) in (0xEA, 0xEB):
                guard = (0x60000000 | (a & 0x0FFFFFFF), b)
                break
            if fallback is None and (a >> 28) == 0xE and i + 1 < len(kept):
                fallback = (0x60000000 | (a & 0x0FFFFFFF), kept[i + 1][0])
        if guard is None:
            guard = fallback
        if guard is None:
            raise ValueError(f"{name}: mixed ARM7/ARM9 section with nothing to re-guard on")
        kept[0] = guard
    return kept


def strip_arm7(pairs, name):
    out = []
    for section in split_sections(pairs):
        kept = strip_section(section[:-1], name)
        if kept is None:
            continue
        out.extend(kept)
        out.append((0xD2000000, 0))
    if not out:
        raise ValueError(f"{name}: nothing left after stripping ARM7 sections")
    if (out[0][0] >> 28) not in (0x3, 0x4, 0x5, 0x6, 0x7, 0x8, 0x9, 0xA):
        raise ValueError(f"{name}: stripped code does not start with a conditional guard")
    if not any((a >> 28) == 0xE for a, _b in out):
        raise ValueError(f"{name}: stripped code has no E bulk write (hook body)")
    for i, span in opcode_positions(out):
        a, _b = out[i]
        if (a >> 28) in (0x0, 0x1, 0x2, 0xE) and is_arm7_addr(a):
            raise ValueError(f"{name}: stripped code still writes ARM7 WRAM ({a:08X})")
    return out


def collect_inputs(inputs):
    """Return [(file, input_root)] for every .txt under the inputs."""
    files = []
    for raw in inputs:
        p = Path(raw)
        if p.is_file():
            if p.suffix.lower() == ".txt":
                files.append((p, p))
            else:
                print(f"skip {p}: not a .txt code file")
        elif p.is_dir():
            found = sorted(p.rglob("*.txt"))
            if not found:
                print(f"skip {p}: no .txt code files under it")
            files.extend((f, p) for f in found)
        else:
            raise SystemExit(f"input not found: {p}")
    return files


def discrete_name(base, used, folder, taken_from):
    """Name a differing same-ROM code so both schemes stay selectable.

    The distinguisher is the first folder-ancestry level at which this
    code's provenance differs from the one that took the base name
    (two folders both called "metroid" are told apart by their
    parents), falling back to a revision number. Content-derived
    names would need per-game knowledge the code does not declare;
    provenance plus the control map (extract_controls) is what the
    player actually chooses between.
    """
    if base not in used:
        return base
    mine = Path(folder).resolve().parts
    theirs = Path(taken_from).resolve().parts
    for level in range(1, min(len(mine), len(theirs)) + 1):
        if mine[-level] != theirs[-level] and f"{base} ({mine[-level]})" not in used:
            return f"{base} ({mine[-level]})"
    n = 2
    while f"{base} (rev{n})" in used:
        n += 1
    return f"{base} (rev{n})"


# ------------------------------------------------------------- usrcheat

def make_header():
    h = bytearray(0x100)
    h[0:0xC] = b"R4 CheatCode"
    h[0xD] = 1
    name = b"DS Analog Controls (CPAD, emulator)"
    struct.pack_into(f"{len(name)}s", h, 0x10, name)
    struct.pack_into("<I", h, 0x4C, 0x594153D5)
    h[0x50] = 1
    return h


def make_game_header(game_name, n_code):
    g = game_name.encode() + b"\0" * (4 - len(game_name) % 4)
    attr = bytearray(0x24)
    attr[0] = n_code
    attr[8] = 1
    return g + attr


def make_category_record(cat_name, count, desc=""):
    # R4 category item: flags word carries the member count in its low
    # 24 bits, bit 28 marks a category and bit 24 marks it exclusive
    # (radio behaviour: enabling one member disables the rest). The
    # reader then assigns the next `count` code items to this group.
    raw = cat_name.encode() + b"\0" + desc.encode() + b"\0"
    raw += b"\0" * ((4 - len(raw) % 4) % 4)
    flags = count | (1 << 28) | (1 << 24)
    return struct.pack("<I", flags) + raw


def make_code_record(code_name, words):
    raw = struct.pack(f"<{len(words)}I", *words)
    header = code_name.encode() + b"\0" + b"\0"  # name, empty description
    header += b"\0" * ((4 - len(header) % 4) % 4)  # % again: a name that
    # already lands on a word boundary needs no pad, and a full extra
    # word here shifts the word count the reader is about to read.
    header += struct.pack("<I", len(raw) // 4)
    total = (len(raw) + len(header)) // 4
    return struct.pack("<I", total) + header + raw


NDS_KEYS = ["A", "B", "Select", "Start", "DPad Right", "DPad Left",
            "DPad Up", "DPad Down", "R", "L", "X", "Y"]


def key_names(mask):
    return [name for bit, name in enumerate(NDS_KEYS) if mask & (1 << bit)]


def extract_controls(words):
    """A discrete, structural control map for one stripped code.

    Decoded from the code itself, at the level the code declares:
    bulk copies (hook installs), branch writes (sub-hook entries),
    and button-map writes (16-bit writes whose value is an NDS
    keymask, decoded with the usual KEYINPUT bit order). What a
    sub-hook *does* at its address is per-game knowledge the code
    does not carry; the map says where it hooks, not what it feels
    like. The host-side vocabulary follows shocoman's remap table:
    CPad and CStick as four directions each, plus ZL/ZR, over the
    twelve NDS keys.
    """
    pairs = list(zip(words[::2], words[1::2]))
    hooks, bulk, button_writes, clears = [], [], [], []
    for i, _span in opcode_positions(pairs):
        a, b = pairs[i]
        hi, addr = a >> 28, a & 0x0FFFFFFF
        if hi == 0xE:
            bulk.append({"dest": f"0x{addr:08X}", "bytes": b})
        elif hi == 0x0 and (b >> 24) in (0xEA, 0xEB):
            hooks.append(f"0x{addr:08X}")
        elif hi == 0x1 and b & 0xFFFF:
            button_writes.append({"addr": f"0x{addr:08X}", "value": f"0x{b & 0xFFFF:04X}",
                                  "buttons": key_names(b & 0xFFFF)})
        elif hi in (0x0, 0x1, 0x2) and b == 0:
            clears.append(f"0x{addr:08X}")
    return {
        "cpad_analog": 0x027FFDF0 in words,
        "hook_installs": bulk,
        "sub_hooks": hooks,
        "button_writes": button_writes,
        "clears": clears,
    }


def detect_layout(relay_cpp):
    """Classify a game's RTCOM_DATA_OUTPUT layout from its relay source.

    Packed (generic): one u32 at +4 packing ZL/ZR and both nub bytes.
    Extended (Metroid): ZL/ZR byte at +4, nub as s16 at +6 and +8.
    Returns None when the source is absent or unrecognised.
    """
    if relay_cpp is None or not relay_cpp.is_file():
        return None
    import re
    writes = {(int(m.group(2) or 0), int(m.group(1)))
              for m in re.finditer(r"\*\(vu(8|16|32) \*\)\(RTCOM_DATA_OUTPUT(?: \+ (\d+))?\)",
                                   relay_cpp.read_text(errors="replace"))}
    if (6, 16) in writes and (8, 16) in writes:
        return "extended"
    if (4, 32) in writes:
        return "packed"
    return None


def code_name_for(filename, force_full=()):
    # Upstream spells the second-stick variant two ways; emit one
    # canonical name. Emulators key the nub/ZL/ZR behaviour on it.
    # A few games (Metroid) ship a single code whose hook already
    # aims with the second word and has no variant tag at all; their
    # game codes can be forced with --full.
    gamecode = filename.split("-")[0]
    if gamecode in force_full or "Nub" in filename or "CStick" in filename:
        return CODE_NAME_FULL
    return CODE_NAME_PLAIN


def build_dat(files, force_full=(), layout_overrides=None):
    games = OrderedDict()  # rom_id -> [(code_name, words, source)]
    layouts = {}
    skipped = []
    for txt, root in files:
        rom_id = txt.name.split(" ")[0]
        gamecode, _, crc = rom_id.partition("-")
        if len(gamecode) != 4 or len(crc) != 8:
            print(f"skip {txt.name}: name is not <GAMECODE>-<CRC>")
            continue
        pairs = parse_pairs(txt.read_text(errors="replace"))
        try:
            sec = strip_arm7(pairs, txt.name)
        except ValueError as e:
            # A file whose ARM9 parts cannot be separated from the ARM7
            # relay (or that leans on it at runtime) is reported, not
            # silently shipped broken; the rest of the catalogue builds.
            print(f"SKIP {txt.name}: {e}")
            skipped.append({"source": str(txt), "reason": str(e)})
            continue
        words = [w for pair in sec for w in pair]
        base = code_name_for(txt.name, force_full)
        layout = (layout_overrides or {}).get(rom_id.partition("-")[0]) \
            or detect_layout(txt.parent / "relay" / "rtcom.cpp") or "packed"
        layouts[rom_id] = "extended" if "extended" in (layouts.get(rom_id), layout) else layout
        # Same ROM id from two sources with different content is two
        # control schemes, not a collision to drop: both are kept,
        # under discrete names (identical content still dedupes, since
        # running the same hook twice is never useful). They install
        # over the same addresses, so exactly one may be enabled at
        # a time -- enforced below by shipping them in one exclusive
        # category; the control map in the manifest is how the player
        # tells them apart.
        siblings = games.setdefault(rom_id, [])
        twin = next((c for c in siblings if c[1] == words), None)
        if twin is not None:
            print(f"duplicate (identical to {twin[0]!r}, ignored): {txt}")
            continue
        if base in {c[0] for c in siblings}:
            holder = next(c for c in siblings if c[0] == base)
            name = discrete_name(base, {c[0] for c in siblings}, txt.parent, Path(holder[2]).parent)
        else:
            name = base
        if name != base:
            print(f"note: {rom_id} also has {base!r}; this scheme is {name!r} (from {txt})")
        siblings.append((name, words, str(txt)))
        print(f"{rom_id}: {name}: {len(words)} words (from {len(pairs)} pairs, {txt})")

    out = bytearray(make_header())
    table = bytearray(0x10 * len(games))
    out += table + bytes(0x10)  # entry list plus its zero terminator
    for i, (rom_id, codes) in enumerate(games.items()):
        gamecode, _, crc = rom_id.partition("-")
        struct.pack_into("<4sI", table, 0x10 * i, gamecode.encode(), int(crc, 16))
        struct.pack_into("<I", table, 0x10 * i + 8, len(out))
        # Several codes for one ROM are alternative schemes: they
        # install hooks over the same addresses, so running two at
        # once corrupts controls (and disabling one does not uninstall
        # its hook until reset). Ship them in one exclusive category
        # so the frontend enforces exactly-one-enabled.
        grouped = len(codes) > 1
        out += make_game_header(rom_id, len(codes) + (1 if grouped else 0))
        if grouped:
            out += make_category_record(
                "Analog control schemes",
                len(codes),
                "Exclusive: enable one scheme. It takes over when you leave this menu.")
        for name, words, _src in codes:
            out += make_code_record(name, words)
    out[0x100:0x100 + len(table)] = table
    return bytes(out), games, skipped, layouts


def parse_layout_overrides(items):
    out = {}
    for it in items or ():
        code, _, layout = it.partition("=")
        if layout not in ("packed", "extended"):
            raise SystemExit(f"--layout expects GAMECODE=packed|extended, got {it!r}")
        out[code] = layout
    return out


def cmd_build(args):
    files = collect_inputs(args.inputs)
    for pat in getattr(args, "exclude", None) or ():
        kept = [(f, r) for f, r in files if pat not in str(f)]
        for f, _r in files:
            if pat in str(f):
                print(f"excluded by --exclude {pat!r}: {f}")
        files = kept
    if not files:
        raise SystemExit("no code files found in the given inputs")
    dat, games, skipped, layouts = build_dat(files, set(args.full or ()), parse_layout_overrides(getattr(args, "layout", None)))
    if not games:
        raise SystemExit("every input was skipped; nothing to write")
    out = Path(args.out)
    out.write_bytes(dat)
    manifest = [{
        "rom_id": rom_id,
        "gamecode": rom_id.partition("-")[0],
        "checksum": rom_id.partition("-")[2],
        "layout": layouts.get(rom_id, "packed"),
        "schemes_exclusive": len(codes) > 1,
        "codes": [{"name": name, "words": len(words), "source": src,
                   "controls": extract_controls(words)}
                  for name, words, src in codes],
    } for rom_id, codes in games.items()]
    mpath = Path(args.manifest) if args.manifest else out.with_suffix(out.suffix + ".manifest.json")
    mpath.write_text(json.dumps({"games": manifest, "skipped": skipped}, indent=1) + "\n")
    print(f"wrote {out}: {len(dat)} bytes, {len(games)} game(s), {len(skipped)} skipped; manifest {mpath}")
    return 0


def cmd_map(args):
    files = collect_inputs(args.inputs)
    if not files:
        raise SystemExit("no code files found in the given inputs")
    _dat, games, _skipped, _layouts = build_dat(files, set(getattr(args, "full", None) or ()), parse_layout_overrides(getattr(args, "layout", None)))
    print()
    print("host inputs (upstream remap vocabulary): A B Select Start, "
          "DPad x4, R L X Y, CPad x4 (analog), ZR ZL, CStick x4")
    for rom_id, codes in games.items():
        for name, words, _src in codes:
            c = extract_controls(words)
            print(f"\n{rom_id}  {name}")
            print(f"  CPad analog (reads 0x027FFDF0): {'yes' if c['cpad_analog'] else 'no'}")
            print(f"  second word (CStick/ZL/ZR) live: "
                  f"{'yes' if 'Nub' in name else 'no (plain variant leaves it zero)'}")
            for h in c["hook_installs"]:
                print(f"  hook install: {h['bytes']} bytes at {h['dest']}")
            if c["sub_hooks"]:
                print(f"  sub-hooks at: {', '.join(c['sub_hooks'])}")
            for w in c["button_writes"]:
                print(f"  button write: {w['addr']} = {w['value']} ({', '.join(w['buttons']) or 'no NDS keys'})")
            if c["clears"]:
                shown = ", ".join(c["clears"][:8]) + (" ..." if len(c["clears"]) > 8 else "")
                print(f"  clears: {shown}")
    return 0


# ------------------------------------------------------------- download

def fetch_repo(repo, ref, work):
    """Clone into work/repo if git works, else unpack the tarball."""
    dest = work / "repo"
    if shutil.which("git"):
        r = subprocess.run(["git", "clone", "--depth", "1", "--branch", ref, repo, str(dest)],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return dest
        print(f"git clone failed ({r.stderr.strip().splitlines()[-1:]}), trying tarball")
    url = repo.rstrip("/").removesuffix(".git") + f"/archive/refs/heads/{ref}.tar.gz"
    tarball = work / "repo.tar.gz"
    urllib.request.urlretrieve(url, tarball)
    with tarfile.open(tarball) as tf:
        tf.extractall(work, filter="data")
    top = next(p for p in work.iterdir() if p.is_dir())
    return top


def cmd_download(args):
    out = Path(args.out)
    if out.exists() and any(out.iterdir()) and not args.force:
        raise SystemExit(f"{out} already exists and is not empty (use --force to replace it)")
    with tempfile.TemporaryDirectory() as td:
        src = fetch_repo(args.repo, args.ref, Path(td))
        out.mkdir(parents=True, exist_ok=True)
        total = 0
        roots = [("sm64ds", src / "action_replay_codes")]
        pog = src / "patches_for_other_games"
        if pog.is_dir():
            roots += [(g.name, g / "action_replay_codes") for g in sorted(pog.iterdir()) if g.is_dir()]
        for game, folder in roots:
            if not folder.is_dir():
                continue
            txts = sorted(folder.glob("*.txt"))
            if not txts:
                continue
            dest = out / game
            dest.mkdir(exist_ok=True)
            for t in txts:
                shutil.copy2(t, dest / t.name)
            relay = (src if game == "sm64ds" else src / "patches_for_other_games" / game) \
                / "arm7_rtcom_patch" / "arm7" / "source" / "rtcom.cpp"
            if relay.is_file():
                (dest / "relay").mkdir(exist_ok=True)
                shutil.copy2(relay, dest / "relay" / "rtcom.cpp")
            total += len(txts)
            print(f"{game}: {len(txts)} code file(s)")
    print(f"downloaded {total} code file(s) into {out}")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="ds-emu-dac", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download", help="fetch shocoman's repo and collect its code files")
    d.add_argument("--out", default=str(Path(__file__).parent / "upstream"),
                   help="where the per-game code folders go (default: ./upstream beside this tool)")
    d.add_argument("--repo", default=REPO_DEFAULT)
    d.add_argument("--ref", default="master")
    d.add_argument("--force", action="store_true", help="replace a non-empty --out")
    d.set_defaults(fn=cmd_download)
    b = sub.add_parser("build", help="build a usrcheat.dat from code files or folders")
    b.add_argument("inputs", nargs="+", help=".txt files and/or folders containing them")
    b.add_argument("--out", required=True, help="output usrcheat.dat path")
    b.add_argument("--manifest", help="manifest JSON path (default: OUT.manifest.json)")
    b.add_argument("--exclude", action="append", metavar="SUBSTRING",
                   help="drop input files whose path contains SUBSTRING (repeatable); "
                        "for a source revision known not to work on an emulator")
    b.add_argument("--layout", action="append", metavar="GAMECODE=LAYOUT",
                   help="force the relay layout for a game code: packed|extended (repeatable)")
    b.add_argument("--full", action="append", metavar="GAMECODE",
                   help="treat this game code's plain code as the Nub/ZL/ZR variant "
                        "(repeatable; for games like Metroid whose single code aims with the second word)")
    b.set_defaults(fn=cmd_build)
    m = sub.add_parser("map", help="print the discrete control map for code files or folders")
    m.add_argument("inputs", nargs="+", help=".txt files and/or folders containing them")
    m.add_argument("--layout", action="append", metavar="GAMECODE=LAYOUT",
                   help="force the relay layout for a game code: packed|extended (repeatable)")
    m.add_argument("--full", action="append", metavar="GAMECODE",
                   help="treat this game code's plain code as the Nub/ZL/ZR variant (repeatable)")
    m.set_defaults(fn=cmd_map)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
