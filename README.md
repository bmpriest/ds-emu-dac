# ds-emu-dac — DS analog-controls cheat codes for emulators

This project owns the *codes* for DSperate's analog-controls feature. The
emulator itself is `cores/DSperate`; the NextUI packaging is
`community-paks/minui-nintendo-dsperate-pak`. This folder is the third
piece: it turns shocoman's Circle Pad (CPAD) Action Replay codes into
codes an emulator can run, and builds the `usrcheat.dat` catalogue the
packagings bundle.

## Why a transformation is needed at all

Upstream codes (shocoman/Analog-Controls-for-NDS-Games-on-3DS) run on a
real 3DS. Each one installs an ARM7 program that uploads code to the
ARM11 over RTCom (a mailbox made of two RTC registers); the ARM11 side
copies Circle Pad / C-Stick / ZL / ZR state into RTC registers, the ARM7
side relays it into guest RAM at `0x027FFDF0`, and an ARM9 hook in the
game reads it from there.

An emulator has no ARM11 and needs no relay: it writes `0x027FFDF0`
itself every frame (DSperate: `pad.analog` + `input::apply()`). So each
code is stripped to its ARM9 parts:

- bulk copies and writes into ARM7/shared WRAM are removed (both the
  `0x03xxxxxx` view and the shared-WRAM `0x027Cxxxx` view);
- a section that was guarded on an ARM7 RTC condition is re-guarded to
  "install while the hook branch (or the hook's first word) is not yet
  in place", so installation still happens exactly once;
- second-stick variants are renamed to one canonical
  `CPAD [with Nub stick, ZL and ZR]` — upstream spells it both that and
  `[with CStick support]`, and emulators key the nub/ZL/ZR behaviour on
  the name.

A file whose ARM9 parts cannot be separated, or that leans on the ARM7
relay at runtime, is skipped with the reason printed and recorded in the
manifest — never shipped silently broken. Today that is exactly
Tony Hawk Motion (both regions), whose later sections are gated on a
shared-RAM flag its ARM7 code sets.

Two different revisions of one ROM's code (a local copy beside a
fresh download, say) are two control schemes, and both are kept:
identical content dedupes, different content gets a discrete name
derived from the first folder level at which its provenance differs
(`CPAD` beside `CPAD (upstream)`), so the player can choose in
the menu. They install over the same addresses, so the build ships
all of a ROM's schemes in one exclusive R4 category: the frontend
treats them as radio buttons and only one can be enabled at a time.
`./ds_emu_dac.py map INPUT...` prints
the discrete control map for each scheme -- hook installs, sub-hook
addresses, button-map writes decoded as NDS keys -- and the same
map is embedded per code in the build manifest.

Schemes can play differently even under the same variant name, and the
map cannot show it: it says where a code hooks, not what the hook does.
Metroid's two are compared in DSperate's
`docs/techniques/07-3ds-circle-pad-cheats.md`, section 6a. Both carry the
`[with Nub stick, ZL and ZR]` name because that name is what makes the
emulator write the second-stick data, but neither has a ZL action.

The catalogue the pak ships is built from this folder with:

```sh
./ds_emu_dac.py build metroid upstream --full AMHE --full AMHJ --full AMHP \
    --out ../../community-paks/minui-nintendo-dsperate-pak/config/usrcheat-analog.dat
```

## Usage

```sh
# Fetch every code file from shocoman's repo into ./upstream/<game>/
./ds_emu_dac.py download [--out DIR] [--ref master] [--force]

# Build a usrcheat.dat from files and/or folders (recursive .txt scan)
./ds_emu_dac.py build metroid --out metroid.dat
./ds_emu_dac.py build upstream --out usrcheat-analog.dat
./ds_emu_dac.py build "upstream/sm64ds/ASME-AEA63749 (USA v1.0).txt" --out one.dat
```

Inputs may be single `.txt` files, a folder of them (like `metroid/`
here, used as the test fixture), or a folder of per-game folders (like
the downloaded `upstream/`). Anything that is not a `.txt` — including
an upstream `usrcheat.dat` sitting next to the codes, as in `metroid/` —
is ignored with a note.

Outputs: the `.dat` (standard R4 format; DSperate matches entries by
game code + header checksum and lists the codes in its Cheats menu) and
a `<name>.manifest.json` beside it listing every ROM id, variant and
source file written, plus every skipped file and why.

## Tests

```sh
python3 -m unittest discover tests
```

The suite builds the bundled `metroid/` fixture (six ROMs) and checks
the re-guarded main section, the canonical variant names, and that no
ARM7 write survives the strip.

## Relationship to the other repos

- `cores/DSperate` implements the emulator side (`pad.analog`, the
  `0x027FFDF0` writes, the CPAD-cheat activation). Its technique note
  `docs/techniques/07-3ds-circle-pad-cheats.md` describes the design.
- Packagings (the NextUI pak today) vendor a built `.dat` from here and
  add their own per-game config; they do not regenerate codes at build
  time. The emulator reads it as a database separate from `usrcheat.dat`
  (`paths.analog_cheats`, or `DSP_ANALOG_CHEATS` from the launcher).
