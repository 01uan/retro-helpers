"""
YGO Forbidden Memories - Hand Card Reader
==========================================
Reads hand card IDs directly from RetroArch (Beetle PSX) process memory.
"""
import pymem
import struct
import ctypes
from ctypes import wintypes
import time
import sys
import json
import os
from itertools import combinations

PROCESS_NAME = "retroarch.exe"
HAND_OFFSET = 0x1A7AE4   # PSX RAM offset for hand cards (from ePSXe reference + CE confirmation)
CARD_STRIDE = 28          # 2 bytes card ID + 26 bytes gap = 28 bytes between card starts
HAND_SIZE = 5             # Number of cards in hand
PSX_RAM_SIZE = 0x200000   # 2MB

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


EQUIP_BOOST = 500  # FM equip cards give +500 ATK / +500 DEF


def load_fusion_data():
    """Load fusions.json and Cards.json, return (fusion_dict, equip_set, card_names, card_stats)."""
    with open(os.path.join(DATA_DIR, "fusions.json"), "r", encoding="utf-8") as f:
        fusions_raw = json.load(f)
    with open(os.path.join(DATA_DIR, "Cards.json"), "r", encoding="utf-8") as f:
        cards = json.load(f)
    # Build fast (a, b) -> result lookup
    fusion_dict = {}
    for a, entries in enumerate(fusions_raw):
        if entries:
            for e in entries:
                fusion_dict[(a, e["card"])] = e["result"]
    # Build equip compatibility set: (equip_id, monster_id)
    equip_set = set()
    card_names = {}
    card_stats = {}
    for c in cards:
        cid = c["Id"]
        card_names[cid] = c["Name"]
        card_stats[cid] = (c.get("Attack", 0), c.get("Defense", 0))
        if c.get("Equip"):
            for monster_id in c["Equip"]:
                equip_set.add((cid, monster_id))
    return fusion_dict, equip_set, card_names, card_stats


def fuse(a, b, fusion_dict):
    """Look up fusion result for two card IDs. Returns result_id or None."""
    return fusion_dict.get((a, b)) or fusion_dict.get((b, a))


def find_combos(hand, fusion_dict, equip_set, card_names, card_stats):
    """Find all combo chains (fusions + equips, 1-4 steps) from current hand.

    Each step is (card_a, card_b, result_id, step_type) where step_type is 'fusion' or 'equip'.
    For equips, result_id == monster_id (monster stays, equip consumed).
    Returns dict: n_steps -> list of chains.
    """
    all_chains = []
    seen = set()

    def recurse(available, chain):
        for i, j in combinations(range(len(available)), 2):
            a, b = available[i], available[j]

            # Try fusion
            result = fuse(a, b, fusion_dict)
            if result is not None:
                new_chain = chain + [(a, b, result, 'fusion')]
                key = frozenset((min(s[0], s[1]), max(s[0], s[1]), s[2], s[3]) for s in new_chain)
                if key not in seen:
                    seen.add(key)
                    all_chains.append(list(new_chain))
                    remaining = [available[k] for k in range(len(available)) if k != i and k != j]
                    remaining.append(result)
                    if len(remaining) >= 2:
                        recurse(remaining, new_chain)

            # Try equip: a equips onto b, or b equips onto a
            for equip_id, monster_id in [(a, b), (b, a)]:
                if (equip_id, monster_id) in equip_set:
                    new_chain = chain + [(equip_id, monster_id, monster_id, 'equip')]
                    key = frozenset((min(s[0], s[1]), max(s[0], s[1]), s[2], s[3]) for s in new_chain)
                    if key not in seen:
                        seen.add(key)
                        all_chains.append(list(new_chain))
                        # Equip consumed, monster stays
                        remaining = [available[k] for k in range(len(available)) if k != i and k != j]
                        remaining.append(monster_id)
                        if len(remaining) >= 2:
                            recurse(remaining, new_chain)

    hand_nonzero = [c for c in hand if c != 0]
    recurse(hand_nonzero, [])

    return all_chains


def chain_final_atk(chain, card_stats):
    """Compute the final ATK of the last result in a chain (including equip boosts)."""
    equip_counts = {}
    last_result = None
    for a, b, result, step_type in chain:
        if step_type == 'fusion':
            equip_counts.pop(a, None)
            equip_counts.pop(b, None)
            last_result = result
        else:  # equip
            equip_counts[result] = equip_counts.get(result, 0) + 1
            last_result = result
    base_atk = card_stats.get(last_result, (0, 0))[0]
    return base_atk + equip_counts.get(last_result, 0) * EQUIP_BOOST


def chain_final_key(chain, card_stats):
    """Return (result_card_id, final_atk) identifying the end result of a chain."""
    equip_counts = {}
    last_result = None
    for a, b, result, step_type in chain:
        if step_type == 'fusion':
            equip_counts.pop(a, None)
            equip_counts.pop(b, None)
            last_result = result
        else:
            equip_counts[result] = equip_counts.get(result, 0) + 1
            last_result = result
    base_atk = card_stats.get(last_result, (0, 0))[0]
    final_atk = base_atk + equip_counts.get(last_result, 0) * EQUIP_BOOST
    return (last_result, final_atk)


def dedup_combos(combos, card_stats):
    """Remove duplicate end monsters, keeping the most efficient chain (fewest steps, then highest ATK)."""
    combos.sort(key=lambda c: (len(c), -chain_final_atk(c, card_stats)))
    seen = set()
    unique = []
    for chain in combos:
        monster_id = chain_final_key(chain, card_stats)[0]
        if monster_id not in seen:
            seen.add(monster_id)
            unique.append(chain)
    return unique


def format_chain(chain, card_names, card_stats):
    """Format a combo chain for display."""
    lines = []
    equip_counts = {}
    for i, (a, b, result, step_type) in enumerate(chain):
        na = card_names.get(a, f"#{a}")
        nb = card_names.get(b, f"#{b}")
        nr = card_names.get(result, f"#{result}")
        prefix = "    → " if i > 0 else "  "
        if step_type == 'fusion':
            atk, def_ = card_stats.get(result, (0, 0))
            equip_counts.pop(a, None)
            equip_counts.pop(b, None)
            lines.append(f"{prefix}{na} + {nb} = {nr} ({atk}/{def_})")
        else:  # equip
            equip_counts[result] = equip_counts.get(result, 0) + 1
            base_atk, base_def = card_stats.get(result, (0, 0))
            total_boost = equip_counts[result] * EQUIP_BOOST
            lines.append(f"{prefix}{na} ⊕ {nb} → {nr} ({base_atk + total_boost}/{base_def + total_boost})")
    return "\n".join(lines)


def display_combos(combos, card_names, card_stats):
    """Print combos sorted: fewest steps first, then highest ATK. No duplicate end results."""
    if not combos:
        print("No combos available.")
        return
    combos = dedup_combos(combos, card_stats)
    print(f"\nAvailable combos ({len(combos)} total):")
    current_n = None
    for chain in combos:
        n = len(chain)
        if n != current_n:
            current_n = n
            count = sum(1 for c in combos if len(c) == n)
            print(f"\n  --- {n + 1}-card combos ({count}) ---")
        print(format_chain(chain, card_names, card_stats))

    # Print the best combo (highest final ATK)
    if combos:
        best = max(combos, key=lambda c: chain_final_atk(c, card_stats))
        atk = chain_final_atk(best, card_stats)
        print(f"\n  ★ BEST: {atk} ATK")
        print(format_chain(best, card_names, card_stats))


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('BaseAddress', ctypes.c_ulonglong),
        ('AllocationBase', ctypes.c_ulonglong),
        ('AllocationProtect', wintypes.DWORD),
        ('_pad1', wintypes.DWORD),
        ('RegionSize', ctypes.c_ulonglong),
        ('State', wintypes.DWORD),
        ('Protect', wintypes.DWORD),
        ('Type', wintypes.DWORD),
        ('_pad2', wintypes.DWORD),
    ]


def attach():
    """Attach to RetroArch process."""
    try:
        pm = pymem.Pymem(PROCESS_NAME)
    except Exception as e:
        print(f"ERROR: Could not attach to {PROCESS_NAME}: {e}")
        print("Make sure RetroArch is running!")
        sys.exit(1)
    print(f"Attached to RetroArch (PID: {pm.process_id})")
    return pm


def read_hand_at(pm, base):
    """Try reading 5 hand cards from a given base address. Returns list or None."""
    try:
        total_bytes = (HAND_SIZE - 1) * CARD_STRIDE + 2
        data = pm.read_bytes(base + HAND_OFFSET, total_bytes)
        cards = []
        for i in range(HAND_SIZE):
            offset = i * CARD_STRIDE
            card_id = struct.unpack_from('<H', data, offset)[0]
            cards.append(card_id)
        return cards
    except Exception:
        return None


def find_all_psx_candidates(pm):
    """Find ALL memory regions with valid hand card data at the known offset."""
    MEM_COMMIT = 0x1000
    kernel32 = ctypes.windll.kernel32
    address = 0
    candidates = []

    while address < 0x7FFFFFFFFFFF:
        mbi = MEMORY_BASIC_INFORMATION()
        result = kernel32.VirtualQueryEx(
            pm.process_handle, ctypes.c_ulonglong(address),
            ctypes.byref(mbi), ctypes.sizeof(mbi)
        )
        if result == 0:
            break
        if mbi.RegionSize == 0:
            address += 0x1000
            continue

        if (mbi.State == MEM_COMMIT and
                mbi.RegionSize >= PSX_RAM_SIZE and
                mbi.Protect in (0x02, 0x04, 0x08, 0x40)):
            base = mbi.BaseAddress
            cards = read_hand_at(pm, base)
            if cards and all(1 <= c <= 722 for c in cards):
                # Skip obvious false positives: all cards the same
                if len(set(cards)) == 1:
                    continue
                candidates.append((base, mbi.RegionSize, cards))

        address = mbi.BaseAddress + mbi.RegionSize

    # Sort: prefer regions that are exactly 2MB (real PSX RAM mirrors)
    candidates.sort(key=lambda c: (0 if c[1] == PSX_RAM_SIZE else 1, c[0]))
    return candidates


def find_psx_ram_base(pm):
    """Find the live PSX RAM base. Uses two reads to detect which region is live."""
    candidates = find_all_psx_candidates(pm)
    if not candidates:
        return None

    # Deduplicate by hand values (mirrors have identical data)
    unique = {}  # hand_tuple -> (base, rsize, cards)
    for base, rsize, cards in candidates:
        key = tuple(cards)
        if key not in unique:
            unique[key] = (base, rsize, cards)

    unique_list = list(unique.values())

    if len(unique_list) == 1:
        base, rsize, cards = unique_list[0]
        print(f"Found PSX RAM at 0x{base:X} — Hand: {cards}")
        return base

    # Multiple distinct candidates — detect the live one by waiting for a change
    print(f"\nFound {len(unique_list)} distinct regions with valid hand data:")
    for i, (base, rsize, cards) in enumerate(unique_list):
        print(f"  {i+1}. 0x{base:X} (size {rsize:#x}) — Hand: {cards}")

    print("\nDetecting live region... play/draw a card or press a button in-game.")
    # Snapshot current values
    snapshots = {base: list(cards) for base, rsize, cards in candidates}
    try:
        for _ in range(120):  # Wait up to 60 seconds
            time.sleep(0.5)
            for base in list(snapshots.keys()):
                current = read_hand_at(pm, base)
                if current and current != snapshots[base]:
                    print(f"  Detected live change at 0x{base:X}: {snapshots[base]} -> {current}")
                    return base
    except KeyboardInterrupt:
        pass

    # Fallback: let user pick
    print("\nNo change detected. Pick manually:")
    for i, (base, rsize, cards) in enumerate(unique_list):
        print(f"  {i+1}. 0x{base:X} — {cards}")
    while True:
        choice = input("Which matches your hand? (number): ").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(unique_list):
                return unique_list[idx][0]
        except ValueError:
            pass


def read_hand(pm, psx_base):
    """Read the 5 hand card IDs from memory (26-byte stride struct)."""
    total_bytes = (HAND_SIZE - 1) * CARD_STRIDE + 2
    data = pm.read_bytes(psx_base + HAND_OFFSET, total_bytes)
    cards = []
    for i in range(HAND_SIZE):
        offset = i * CARD_STRIDE
        card_id = struct.unpack_from('<H', data, offset)[0]
        cards.append(card_id)
    return cards


def main():
    pm = attach()

    print("Loading fusion & equip data...")
    fusion_dict, equip_set, card_names, card_stats = load_fusion_data()
    print(f"Loaded {len(card_names)} cards, {len(fusion_dict)} fusion pairs, {len(equip_set)} equip combos.")

    print("Searching for PSX RAM...")
    psx_base = find_psx_ram_base(pm)
    if psx_base is None:
        print("ERROR: Could not find PSX RAM with valid hand data.")
        print("Make sure you are in a duel with cards in your hand!")
        sys.exit(1)

    hand = read_hand(pm, psx_base)
    print(f"\nHand cards: {[card_names.get(c, c) for c in hand]}")
    for i, card_id in enumerate(hand):
        print(f"  Slot {i+1}: {card_names.get(card_id, f'#{card_id}')} ({card_id})")

    combos = find_combos(hand, fusion_dict, equip_set, card_names, card_stats)
    display_combos(combos, card_names, card_stats)

    # Continuous monitoring mode
    print("\n--- Monitoring hand (Ctrl+C to stop) ---")
    prev_hand = hand
    try:
        while True:
            time.sleep(0.5)
            try:
                hand = read_hand(pm, psx_base)
            except Exception:
                print("Read error — RetroArch may have closed.")
                break
            if hand != prev_hand:
                print(f"\nHand changed: {[card_names.get(c, c) for c in hand]}")
                for i, card_id in enumerate(hand):
                    print(f"  Slot {i+1}: {card_names.get(card_id, f'#{card_id}')} ({card_id})")
                combos = find_combos(hand, fusion_dict, equip_set, card_names, card_stats)
                display_combos(combos, card_names, card_stats)
                prev_hand = hand
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()