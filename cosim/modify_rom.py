#!/usr/bin/env python3
"""
Modify the ROM SPICE netlist to match a target 128x128 monochrome bitmap.

Uses the same ROM structure analysis as extract_rom.py to understand the
existing netlist, then adds/removes NFET ROM cells to match the target image.

ROM cell convention: NFET present = bit 1 (after inverting output buffer).

Usage:
    python3 modify_rom.py [--bitmap artwork/logo_1bpp.bin] [--verify]
"""

import sys
import re
from pathlib import Path
from collections import defaultdict

# Reuse parser from extract_rom.py
sys.path.insert(0, str(Path(__file__).parent))
from extract_rom import parse_spice, flatten_rom, build_m1_mapping, build_wordline_map


def build_rom_cell_map(nfet_list, bitline_to_qbit, m1_expr, wordline_map):
    """Map each ROM cell NFET to its logical address.

    Returns:
        cell_map: {(bit_idx, full_addr): (drain_net, gate_net, source_net)}
        sub_bitline_map: {(bit_idx, addr_0_4): sub_bitline_net}
    """
    nfet_adj = defaultdict(list)
    for d, g, s in nfet_list:
        if d != s:
            nfet_adj[d].append((g, s))
            nfet_adj[s].append((g, d))

    cell_map = {}  # (bit_idx, full_addr) → True
    sub_bitline_map = {}  # (bit_idx, addr_0_4) → sub_bitline_net

    for bl_net, bit_idx in bitline_to_qbit.items():
        def trace_tree(net, visited, addr_val, addr_mask):
            # Check for ROM cells (NFET to VGND with wordline gate)
            for gate, other in nfet_adj[net]:
                if other == "VGND" and gate in wordline_map:
                    wl_addr = wordline_map[gate]
                    full_addr = addr_val | wl_addr
                    cell_map[(bit_idx, full_addr)] = True

            # At leaf level (all 5 bits determined), record sub-bitline
            if bin(addr_mask).count('1') == 5:
                sub_bitline_map[(bit_idx, addr_val)] = net

            # Follow tree edges
            for gate, other in nfet_adj[net]:
                if other in visited or other == "VGND":
                    continue
                if gate not in m1_expr:
                    continue
                bit, is_complement = m1_expr[gate]
                if bit > 4:
                    continue
                bit_mask = 1 << bit
                if addr_mask & bit_mask:
                    continue

                new_mask = addr_mask | bit_mask
                new_val = addr_val if is_complement else addr_val | bit_mask

                visited.add(other)
                trace_tree(other, visited, new_val, new_mask)
                visited.discard(other)

        visited = {bl_net}
        trace_tree(bl_net, visited, 0, 0)

    return cell_map, sub_bitline_map


def load_target_bitmap(bin_path):
    """Load target bitmap from binary file (4096 bytes, ROM format).

    Returns: set of (bit_idx, addr) tuples where NFET should be present.
    """
    data = Path(bin_path).read_bytes()
    assert len(data) == 4096, f"Expected 4096 bytes, got {len(data)}"

    target_cells = set()
    for addr in range(4096):
        byte_val = data[addr]
        for bit in range(8):
            if byte_val & (1 << bit):
                target_cells.add((bit, addr))
    return target_cells


def modify_spice_netlist(spice_path, target_cells, output_path=None):
    """Modify the SPICE netlist to match target bitmap.

    Strategy:
    1. Parse the netlist to understand ROM structure
    2. Identify existing ROM cells and their addresses
    3. Rewrite rom_vga_logo_core subcircuit with correct cells
    """
    if output_path is None:
        output_path = spice_path

    print(f"Parsing {spice_path}...")
    subcircuits = parse_spice(str(spice_path))

    print("Analyzing ROM structure...")
    nfet_list, pfet_list, bitline_to_qbit = flatten_rom(subcircuits)
    m1_expr = build_m1_mapping(nfet_list)
    wordline_map = build_wordline_map(nfet_list, pfet_list, m1_expr)

    print(f"  {len(wordline_map)} wordlines, {len(bitline_to_qbit)} bitlines")

    # Build cell map and sub-bitline map
    existing_cells, sub_bitline_map = build_rom_cell_map(
        nfet_list, bitline_to_qbit, m1_expr, wordline_map)

    print(f"  {len(existing_cells)} existing ROM cells")
    print(f"  {len(sub_bitline_map)} sub-bitlines mapped")
    print(f"  {len(target_cells)} target ROM cells")

    # Compute changes
    to_add = target_cells - set(existing_cells.keys())
    to_remove = set(existing_cells.keys()) - target_cells
    unchanged = target_cells & set(existing_cells.keys())
    print(f"  Add: {len(to_add)}, Remove: {len(to_remove)}, Keep: {len(unchanged)}")

    # Build reverse maps for modification
    # wordline: addr_5_11 → n498# net
    addr_to_wordline = {}
    for wl_net, addr_val in wordline_map.items():
        addr_to_wordline[addr_val] = wl_net

    # Now rewrite the SPICE netlist
    # We need to:
    # 1. Keep all non-ROM-cell MOSFETs in rom_vga_logo_core
    # 2. Remove ROM cells that shouldn't exist
    # 3. Add ROM cells that should exist

    # Read the raw file and find rom_vga_logo_core subcircuit boundaries
    with open(spice_path) as f:
        raw_lines = f.readlines()

    # Find core subcircuit boundaries
    core_start = None
    core_end = None
    for i, line in enumerate(raw_lines):
        stripped = line.strip()
        if stripped.startswith(".subckt") and "rom_vga_logo_core" in stripped:
            core_start = i
        elif core_start is not None and stripped.startswith(".ends"):
            core_end = i
            break

    if core_start is None or core_end is None:
        print("ERROR: Could not find rom_vga_logo_core subcircuit!")
        return False

    print(f"  Core subcircuit: lines {core_start+1}-{core_end+1}")

    # Parse the core subcircuit to identify ROM cell MOSFETs
    # ROM cells are NFETs with:
    #   - source or drain = VGND
    #   - gate = n498# wordline net
    #   - the other terminal is a sub-bitline (internal net)
    core_sub = subcircuits["rom_vga_logo_core"]
    core_ports_set = set(core_sub["ports"])

    # Build sets of raw net names for matching against SPICE text.
    # wordline_map keys have "core:" prefix from flatten_rom's resolve();
    # raw SPICE uses bare net names inside the subcircuit.
    wordline_nets_raw = set()
    for wl_net in wordline_map.keys():
        raw = wl_net[5:] if wl_net.startswith("core:") else wl_net
        wordline_nets_raw.add(raw)

    # Find VGND port names in core subcircuit
    # (VGND is connected through port mapping, not directly named)
    top = subcircuits["rom_vga_logo"]
    core_inst = None
    for inst in top["instances"]:
        if inst["subckt"] == "rom_vga_logo_core":
            core_inst = inst
            break
    vgnd_nets_raw = set()
    for i, port_name in enumerate(core_sub["ports"]):
        if core_inst["nets"][i] == "VGND":
            vgnd_nets_raw.add(port_name)

    print(f"  VGND ports in core: {vgnd_nets_raw}")

    # Re-parse the core subcircuit to get exact line positions
    # We need to handle continuation lines
    core_lines = raw_lines[core_start:core_end + 1]

    # Join continuation lines for analysis
    current_line = ""
    current_start_idx = 0
    line_ranges = []  # (start_idx, end_idx, joined_content)

    for i, line in enumerate(core_lines):
        stripped = line.rstrip("\n")
        if stripped.startswith("+") and current_line:
            current_line += " " + stripped[1:].strip()
        else:
            if current_line:
                line_ranges.append((current_start_idx, i - 1, current_line))
            current_line = stripped.strip()
            current_start_idx = i
    if current_line:
        line_ranges.append((current_start_idx, len(core_lines) - 1, current_line))

    # Identify ROM cell MOSFET lines
    # ROM cells are NFETs with: source or drain = VGND port, gate = wordline net
    rom_cell_line_indices = set()  # indices into line_ranges
    for lr_idx, (start_idx, end_idx, content) in enumerate(line_ranges):
        if not content.startswith("X"):
            continue
        parts = content.split()
        model = None
        nets = []
        for p in parts[1:]:
            if "=" in p:
                break
            if p.startswith("sky130_fd_pr__"):
                model = p
            else:
                nets.append(p)
        if model and "nfet" in model and len(nets) == 4:
            d, g, s, bulk = nets
            is_rom_cell = False
            if (s in vgnd_nets_raw or d in vgnd_nets_raw) and g in wordline_nets_raw:
                is_rom_cell = True
            if is_rom_cell:
                rom_cell_line_indices.add(lr_idx)

    print(f"  {len(rom_cell_line_indices)} ROM cell MOSFET lines identified")

    # Build output: keep non-ROM-cell lines, add new ROM cells
    output_lines = []
    # Copy everything before core subcircuit
    output_lines.extend(raw_lines[:core_start])

    # Rebuild core subcircuit
    for lr_idx, (start_idx, end_idx, content) in enumerate(line_ranges):
        if lr_idx in rom_cell_line_indices:
            continue  # Skip old ROM cells
        # Copy original lines (preserving formatting)
        for i in range(start_idx, end_idx + 1):
            output_lines.append(core_lines[i])

    # Find a ROM cell MOSFET to use as template for dimensions
    template_params = "w=420000u l=150000u"  # default
    if rom_cell_line_indices:
        for lr_idx in rom_cell_line_indices:
            _, _, content = line_ranges[lr_idx]
            match = re.search(r'(w=\S+\s+l=\S+)', content)
            if match:
                template_params = match.group(1)
                break
    else:
        # If no ROM cells were identified, scan all NFETs for typical ROM dimensions
        for lr_idx, (start_idx, end_idx, content) in enumerate(line_ranges):
            if content.startswith("X") and "nfet" in content:
                match = re.search(r'(w=\S+\s+l=\S+)', content)
                if match:
                    template_params = match.group(1)
                    break

    # Before .ends, insert new ROM cells
    # Remove the last line (.ends) temporarily
    ends_line = output_lines.pop()

    # Pick one VGND port name to use for new cells
    vgnd_raw = sorted(vgnd_nets_raw)[0]  # consistent choice
    print(f"  Using VGND net: {vgnd_raw}")

    # Generate new ROM cell NFETs
    cell_counter = 0
    warnings = 0
    for bit_idx, addr in sorted(target_cells):
        addr_0_4 = addr & 0x1F
        addr_5_11 = addr & 0xFE0

        # Find sub-bitline net
        key = (bit_idx, addr_0_4)
        if key not in sub_bitline_map:
            # Try with just bits 1-4 (addr[0] might vary)
            key_alt = (bit_idx, addr_0_4 & 0x1E)
            if key_alt in sub_bitline_map:
                key = key_alt
            else:
                if warnings < 5:
                    print(f"  WARNING: No sub-bitline for bit={bit_idx} addr_0_4={addr_0_4}")
                warnings += 1
                continue

        sub_bl = sub_bitline_map[key]
        # Remove "core:" prefix if present
        if sub_bl.startswith("core:"):
            sub_bl = sub_bl[5:]

        # Find wordline net (strip "core:" prefix for raw SPICE)
        if addr_5_11 not in addr_to_wordline:
            if warnings < 5:
                print(f"  WARNING: No wordline for addr_5_11={addr_5_11}")
            warnings += 1
            continue
        wl = addr_to_wordline[addr_5_11]
        if wl.startswith("core:"):
            wl = wl[5:]

        cell_counter += 1
        output_lines.append(
            f"Xrom_cell_{cell_counter} {vgnd_raw} {wl} {sub_bl} {vgnd_raw} "
            f"sky130_fd_pr__nfet_01v8 {template_params}\n"
        )
    if warnings > 5:
        print(f"  ({warnings} total warnings, {warnings - 5} suppressed)")

    output_lines.append(ends_line)

    # Copy everything after core subcircuit
    output_lines.extend(raw_lines[core_end + 1:])

    print(f"  Generated {cell_counter} new ROM cell NFETs")

    # Write output
    with open(output_path, "w") as f:
        f.writelines(output_lines)

    print(f"  Written to {output_path}")
    return True


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Modify ROM SPICE netlist")
    parser.add_argument("--bitmap", default=None,
                        help="Path to target bitmap binary (default: artwork/logo_1bpp.bin)")
    parser.add_argument("--spice", default=None,
                        help="Path to input SPICE netlist (default: macro/rom_vga_logo.lvs.spice)")
    parser.add_argument("--output", default=None,
                        help="Path to output SPICE netlist (default: overwrite input)")
    parser.add_argument("--verify", action="store_true",
                        help="Verify the output using extract_rom logic")
    args = parser.parse_args()

    base_dir = Path(__file__).parent.parent
    bitmap_path = args.bitmap or str(base_dir / "artwork" / "logo_1bpp.bin")
    spice_path = args.spice or str(base_dir / "macro" / "rom_vga_logo.lvs.spice")
    output_path = args.output or spice_path

    print(f"Loading target bitmap: {bitmap_path}")
    target_cells = load_target_bitmap(bitmap_path)
    print(f"  {len(target_cells)} cells should have NFETs")

    success = modify_spice_netlist(spice_path, target_cells, output_path)
    if not success:
        sys.exit(1)

    if args.verify:
        print("\nVerifying modified netlist...")
        from extract_rom import (parse_spice, flatten_rom, build_m1_mapping,
                                 build_wordline_map, build_tree_and_rom)
        subcircuits = parse_spice(output_path)
        nfet_list, pfet_list, bitline_to_qbit = flatten_rom(subcircuits)
        m1_expr = build_m1_mapping(nfet_list)
        wl_map = build_wordline_map(nfet_list, pfet_list, m1_expr)
        rom_cells = build_tree_and_rom(nfet_list, bitline_to_qbit, m1_expr, wl_map)

        # Build result bytes
        result = bytearray(4096)
        for (bit_idx, addr), _ in rom_cells.items():
            if 0 <= addr < 4096:
                result[addr] |= (1 << bit_idx)

        # Compare against target
        expected = Path(bitmap_path).read_bytes()
        errors = 0
        for addr in range(4096):
            if result[addr] != expected[addr]:
                if errors < 10:
                    print(f"  MISMATCH addr={addr:04d}: got=0x{result[addr]:02x} exp=0x{expected[addr]:02x}")
                errors += 1

        if errors == 0:
            print("  PASS: Modified netlist matches target bitmap!")
        else:
            print(f"  FAIL: {errors}/4096 mismatches")
            sys.exit(1)


if __name__ == "__main__":
    main()
