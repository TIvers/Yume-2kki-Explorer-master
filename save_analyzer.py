#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Анализатор save-файлов (.lsd) Yume 2kki для Yume 2kki Explorer.

Извлекает из LcfSaveData:
  - собранные эффекты (inventory items)
  - посещённые карты (visit-count variables + текущая карта)
  - переключатели (switches) — для меню-тем и флагов
  - переменные (variables)
  - текущую позицию / map ID

Использование:
    python save_analyzer.py Save01.lsd
    python save_analyzer.py Save01.lsd -o save_analysis.json
    python save_analyzer.py Save01.lsd --pretty

Выходной JSON можно подгрузить в Explorer (см. README / UI),
чтобы подсветить посещённые миры, собранные эффекты и темы меню.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# LCF helpers (BER + chunk reader)
# ---------------------------------------------------------------------------

def read_ber(f: BytesIO) -> int:
    result = 0
    while True:
        b = f.read(1)
        if not b:
            raise EOFError("Unexpected EOF while reading BER")
        b = b[0]
        result = (result << 7) | (b & 0x7F)
        if not (b & 0x80):
            break
    return result


def read_chunks(data: bytes) -> Dict[int, bytes]:
    f = BytesIO(data)
    hlen = read_ber(f)
    header = f.read(hlen)
    if header != b"LcfSaveData":
        raise ValueError(f"Not a valid LcfSaveData (header={header!r})")

    chunks: Dict[int, bytes] = {}
    while f.tell() < len(data):
        try:
            cid = read_ber(f)
            csize = read_ber(f)
            chunks[cid] = f.read(csize)
        except EOFError:
            break
    return chunks


def read_subchunks(chunk: bytes) -> Dict[int, bytes]:
    f = BytesIO(chunk)
    subs: Dict[int, bytes] = {}
    while f.tell() < len(chunk):
        try:
            sid = read_ber(f)
            ssize = read_ber(f)
            subs[sid] = f.read(ssize)
        except EOFError:
            break
    return subs


# ---------------------------------------------------------------------------
# Known Yume 2kki mappings (partial, expandable)
# ---------------------------------------------------------------------------

# Effects are stored as items. Typical range starts around 50.
# Mapping is approximate / version-dependent; names from public/lang/effect-ja.json
# and common community lists. Expand as needed.
EFFECT_ITEM_IDS: Dict[int, str] = {
    # Common mapping used by many tools / community (may shift between versions)
    50: "Instructions",
    51: "Bike",
    52: "Boy",
    53: "Chainsaw",
    54: "Lantern",
    55: "Fairy",
    56: "Spacesuit",
    57: "Glasses",
    58: "Rainbow",
    59: "Wolf",
    60: "Eyeball Bomb",
    61: "Telephone",
    62: "Maiko",
    63: "Twintails",
    64: "Penguin",
    65: "Insect",
    66: "Spring",
    67: "Invisible",
    68: "Plaster Cast",
    69: "Stretch",
    70: "Haniwa",
    71: "Cake",
    72: "Child",
    73: "Gakuran",
    74: "Trombone",
    75: "Tissue",
    76: "Red Riding Hood",
    77: "Polygon",
    78: "Marginal",
    79: "Teru-Teru Bōzu",
    80: "Bat",
    81: "Drum",
    82: "Grave",
    83: "Railroad Crossing",
    84: "Bunny Ears",
    85: "Dice",
    86: "Effect 36",
    87: "Effect 37",
}

# Visit-count variables documented on the wiki (Variables page).
# Format: var_id -> human-readable map / purpose
VISIT_VARS: Dict[int, str] = {
    162: "Urotsuki's Room (Real)",
    163: "Urotsuki's Balcony (Real)",
    164: "Urotsuki's Room (Dream)",
    165: "Urotsuki's Balcony (Dream)",
    166: "Sound Room",
    170: "Nexus (Door Room)",
    171: "Hand Hub",
    172: "Chocolate World",
    173: "Art Gallery",
    174: "Red Streetlight World",
    175: "Theatre World",
    176: "Broken Faces Area",
    177: "Intestines Maze",
    178: "Gray Road",
    # further ranges exist (0561+ etc.) — add as needed
}

# Menu themes are unlocked via events; tracking is often in switches or
# dedicated variables. We expose raw switches/vars so the UI can match
# against menu_themes.json locations.


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------

def parse_inventory(status_chunk: bytes) -> Dict[int, int]:
    """Return {item_id: quantity} from Status chunk."""
    subs = read_subchunks(status_chunk)
    item_ids: List[int] = []
    qtys: List[int] = []
    if 12 in subs:
        raw = subs[12]
        item_ids = [struct.unpack_from("<H", raw, i)[0] for i in range(0, len(raw), 2)]
    if 13 in subs:
        qtys = list(subs[13])
    inv: Dict[int, int] = {}
    for iid, q in zip(item_ids, qtys):
        if q > 0:
            inv[iid] = q
    return inv


def parse_switches(system_chunk: bytes) -> List[int]:
    """Return list of ON switch IDs (1-based)."""
    subs = read_subchunks(system_chunk)
    data = subs.get(0x20, b"")
    return [i + 1 for i, b in enumerate(data) if b != 0]


def parse_variables(system_chunk: bytes) -> Dict[int, int]:
    """Return {var_id: value} for non-zero variables (1-based)."""
    subs = read_subchunks(system_chunk)
    data = subs.get(0x22, b"")
    out: Dict[int, int] = {}
    for i in range(0, len(data) // 4):
        v = struct.unpack_from("<i", data, i * 4)[0]
        if v != 0:
            out[i + 1] = v
    return out


def parse_party_position(party_chunk: bytes) -> Dict[str, Any]:
    """Best-effort current map / coordinates from Party chunk."""
    subs = read_subchunks(party_chunk)
    result: Dict[str, Any] = {}
    # Common layout in EasyRPG / RM2k3 saves:
    # 12 = map id (sometimes), 13 = x, ... — varies; we keep raw useful fields
    for sid, name in [(12, "raw_12"), (13, "raw_13"), (54, "map_id_candidate"),
                      (62, "x_candidate"), (63, "y_candidate")]:
        if sid in subs:
            raw = subs[sid]
            if len(raw) == 1:
                result[name] = raw[0]
            elif len(raw) == 2:
                result[name] = struct.unpack("<H", raw)[0]
            else:
                result[name] = raw.hex()
    return result


def analyze_save(path: Path) -> Dict[str, Any]:
    data = path.read_bytes()
    chunks = read_chunks(data)

    system = chunks.get(101, b"")
    status = chunks.get(109, b"")
    party = chunks.get(104, b"")

    inventory = parse_inventory(status) if status else {}
    switches = parse_switches(system) if system else []
    variables = parse_variables(system) if system else {}
    position = parse_party_position(party) if party else {}

    # Effects
    effects_collected = []
    for iid, qty in sorted(inventory.items()):
        name = EFFECT_ITEM_IDS.get(iid, f"Item_{iid}")
        effects_collected.append({
            "itemId": iid,
            "name": name,
            "quantity": qty,
        })

    # Visited maps from known visit variables
    visited_maps = []
    for vid, label in sorted(VISIT_VARS.items()):
        count = variables.get(vid, 0)
        if count > 0:
            visited_maps.append({
                "variableId": vid,
                "label": label,
                "visitCount": count,
            })

    # Heuristic: any variable whose name on the wiki starts with 【in】
    # (we only have the known subset above; the rest stay in "variables")

    # Menu themes: we cannot map every switch without the LDB,
    # so we report switch count + expose the full list for downstream matching.
    menu_theme_hint = {
        "note": "Menu themes are unlocked by events; match switch/variable "
                "IDs against the game database or menu_themes.json locations. "
                "A future version will auto-map them once LDB item/switch "
                "names are available.",
        "switchesOnCount": len(switches),
        "switchesOn": switches[:200],  # cap for readability
    }

    return {
        "source": str(path),
        "effects": {
            "collectedCount": len(effects_collected),
            "collected": effects_collected,
            "allKnownEffectItemIds": sorted(EFFECT_ITEM_IDS.keys()),
        },
        "maps": {
            "visitedFromVariables": visited_maps,
            "currentPosition": position,
            "note": "Full map-visit bitmask lives in game-specific variables/switches; "
                    "only documented visit-count vars are listed here.",
        },
        "menuThemes": menu_theme_hint,
        "raw": {
            "switchesOn": switches,
            "variablesNonZero": {str(k): v for k, v in sorted(variables.items())},
            "inventory": inventory,
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Yume 2kki .lsd save analyzer")
    ap.add_argument("save", type=Path, help="Path to .lsd save file")
    ap.add_argument("-o", "--output", type=Path, help="Write JSON to this file")
    ap.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    args = ap.parse_args()

    if not args.save.is_file():
        print(f"File not found: {args.save}", file=sys.stderr)
        return 1

    try:
        result = analyze_save(args.save)
    except Exception as e:
        print(f"Failed to parse save: {e}", file=sys.stderr)
        return 1

    text = json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None)

    if args.output:
        args.output.write_text(text, encoding="utf-8")
        print(f"Wrote {args.output}")
    else:
        print(text)

    # Short human summary
    print("\n--- Summary ---", file=sys.stderr)
    print(f"Effects collected: {result['effects']['collectedCount']}", file=sys.stderr)
    for e in result["effects"]["collected"]:
        print(f"  • {e['name']} (item {e['itemId']})", file=sys.stderr)
    print(f"Visit variables with count > 0: {len(result['maps']['visitedFromVariables'])}",
          file=sys.stderr)
    for m in result["maps"]["visitedFromVariables"]:
        print(f"  • {m['label']}: {m['visitCount']}", file=sys.stderr)
    print(f"Switches ON: {result['menuThemes']['switchesOnCount']}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
