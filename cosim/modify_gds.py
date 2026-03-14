#!/usr/bin/env python3
"""
Modify the ROM GDS layout to match a target 128x128 monochrome bitmap.

Adds/removes diffusion polygons (layer 65/20) in the ROM array to create
a transistor pattern that visually represents the target image in GDS viewers.

ROM physical layout:
  - GDS Y-axis: 128 wordlines (~0.7 um pitch), selected by addr[11:5]
  - GDS X-axis: 256 columns = 8 output blocks x 32 sub-bitlines (~0.5 um pitch)
  - ROM cell: NFET = diffusion polygon (~0.69 x 0.43 um)
  - Wordline poly runs continuously; only diffusion needs adding/removing

Usage:
    python3 modify_gds.py [--bitmap artwork/logo_1bpp.bin] [--verify]
"""

import sys
import gzip
from pathlib import Path
import numpy as np

try:
    import gdstk
except ImportError:
    print("gdstk not installed. Install with: pip install gdstk")
    sys.exit(1)


# ROM array physical parameters (from GDS analysis)
DIFF_LAYER = 65
DIFF_DATATYPE = 20

# Expected grid dimensions
N_COLS = 256  # 8 output bits x 32 sub-bitlines
N_ROWS = 128  # 128 wordlines


def find_rom_core_cell(library):
    """Find the rom_vga_logo_core cell in the GDS library."""
    for cell in library.cells:
        if cell.name == "rom_vga_logo_core":
            return cell
    # Try partial match
    for cell in library.cells:
        if "core" in cell.name.lower():
            return cell
    return None


def find_diffusion_polygons(cell, rom_only=True):
    """Find diffusion (layer 65/20) polygons in the cell.

    If rom_only=True, filter to only ROM cell-sized polygons (~0.69 x 0.43 um)
    to exclude infrastructure transistors (decoders, buffers).

    Returns list of (polygon, bbox) tuples.
    """
    all_polys = []
    for poly in cell.polygons:
        if poly.layer == DIFF_LAYER and poly.datatype == DIFF_DATATYPE:
            bbox = poly.bounding_box()
            if bbox is not None:
                all_polys.append((poly, bbox))

    if not rom_only:
        return all_polys

    # Find most common size (ROM cells dominate)
    if not all_polys:
        return all_polys

    widths = [bb[1][0] - bb[0][0] for _, bb in all_polys]
    heights = [bb[1][1] - bb[0][1] for _, bb in all_polys]
    med_w = np.median(widths)
    med_h = np.median(heights)

    # Filter to ROM cell size (within 5% of median)
    tol_w = med_w * 0.08
    tol_h = med_h * 0.08
    rom_polys = [(p, bb) for p, bb in all_polys
                 if abs((bb[1][0] - bb[0][0]) - med_w) < tol_w
                 and abs((bb[1][1] - bb[0][1]) - med_h) < tol_h]

    print(f"  Filtered {len(all_polys)} → {len(rom_polys)} ROM-sized "
          f"({med_w:.3f}x{med_h:.3f} +/- {tol_w:.3f}x{tol_h:.3f})")
    return rom_polys


def analyze_rom_grid(diff_polys):
    """Analyze diffusion polygon positions to determine ROM grid parameters.

    Returns:
        x_positions: sorted unique X center positions (256 expected)
        y_positions: sorted unique Y center positions (128 expected)
        x_pitch, y_pitch: spacing between adjacent positions
        cell_width, cell_height: typical diffusion polygon dimensions
    """
    if not diff_polys:
        print("  WARNING: No diffusion polygons found!")
        return None

    # Get centers and sizes
    centers_x = []
    centers_y = []
    widths = []
    heights = []
    for poly, bbox in diff_polys:
        cx = (bbox[0][0] + bbox[1][0]) / 2
        cy = (bbox[0][1] + bbox[1][1]) / 2
        w = bbox[1][0] - bbox[0][0]
        h = bbox[1][1] - bbox[0][1]
        centers_x.append(cx)
        centers_y.append(cy)
        widths.append(w)
        heights.append(h)

    # Cluster positions to find grid
    x_positions = _cluster_positions(centers_x)
    y_positions = _cluster_positions(centers_y)

    # Typical cell size
    cell_width = np.median(widths)
    cell_height = np.median(heights)

    # Pitch
    x_pitch = np.median(np.diff(x_positions)) if len(x_positions) > 1 else 0.5
    y_pitch = np.median(np.diff(y_positions)) if len(y_positions) > 1 else 0.7

    print(f"  Grid: {len(x_positions)} X x {len(y_positions)} Y positions")
    print(f"  X range: {x_positions[0]:.3f} - {x_positions[-1]:.3f}, pitch: {x_pitch:.3f}")
    print(f"  Y range: {y_positions[0]:.3f} - {y_positions[-1]:.3f}, pitch: {y_pitch:.3f}")
    print(f"  Cell size: {cell_width:.3f} x {cell_height:.3f}")
    print(f"  Total diffusion polygons: {len(diff_polys)}")

    return {
        "x_positions": x_positions,
        "y_positions": y_positions,
        "x_pitch": x_pitch,
        "y_pitch": y_pitch,
        "cell_width": cell_width,
        "cell_height": cell_height,
    }


def _cluster_positions(values, tolerance=0.15):
    """Cluster nearby values and return sorted unique positions.

    Default tolerance of 0.15 um works for ROM grid (0.5 um X pitch, 0.7 um Y pitch).
    """
    if not values:
        return np.array([])
    sorted_vals = np.sort(values)
    clusters = [sorted_vals[0]]
    cluster_sums = [sorted_vals[0]]
    cluster_counts = [1]

    for v in sorted_vals[1:]:
        if abs(v - clusters[-1]) < tolerance:
            cluster_sums[-1] += v
            cluster_counts[-1] += 1
            clusters[-1] = cluster_sums[-1] / cluster_counts[-1]
        else:
            clusters.append(v)
            cluster_sums.append(v)
            cluster_counts.append(1)

    return np.array(clusters)


def map_grid_to_logical(grid_info):
    """Map physical grid positions to logical (output_bit, addr) coordinates.

    X-axis mapping (256 columns):
      8 output bit blocks x 32 sub-bitlines per block
      Within block: 16 sub-bitline pairs x 2 columns per pair
      q[0] → cols 0-31, q[1] → cols 32-63, ..., q[7] → cols 224-255

    Y-axis mapping (128 rows):
      Direct: row i → wordline i → addr[11:5] = i

    Returns:
      col_to_logical: {col_idx: (bit_idx, addr_4_1, addr_0)}
      row_to_logical: {row_idx: addr_11_5_value}
    """
    x_pos = grid_info["x_positions"]
    y_pos = grid_info["y_positions"]

    # Column mapping: straightforward left-to-right
    col_to_logical = {}
    for col_idx in range(min(len(x_pos), N_COLS)):
        block = col_idx // 32  # output bit (q[0..7])
        within_block = col_idx % 32
        pair = within_block // 2  # addr[4:1]
        addr_0 = within_block % 2  # addr[0]
        col_to_logical[col_idx] = (block, pair, addr_0)

    # Row mapping: straightforward bottom-to-top or top-to-bottom
    row_to_logical = {}
    for row_idx in range(min(len(y_pos), N_ROWS)):
        row_to_logical[row_idx] = row_idx  # addr[11:5] = row index

    return col_to_logical, row_to_logical


def build_occupancy_grid(diff_polys, grid_info):
    """Build a boolean grid of which cells have diffusion polygons.

    Returns: 2D numpy array [n_rows x n_cols] of booleans
    """
    x_pos = grid_info["x_positions"]
    y_pos = grid_info["y_positions"]
    tolerance = min(grid_info["x_pitch"], grid_info["y_pitch"]) * 0.3

    grid = np.zeros((len(y_pos), len(x_pos)), dtype=bool)

    for poly, bbox in diff_polys:
        cx = (bbox[0][0] + bbox[1][0]) / 2
        cy = (bbox[0][1] + bbox[1][1]) / 2

        # Find nearest grid position
        col = np.argmin(np.abs(x_pos - cx))
        row = np.argmin(np.abs(y_pos - cy))

        if abs(x_pos[col] - cx) < tolerance and abs(y_pos[row] - cy) < tolerance:
            grid[row, col] = True

    return grid


def load_target_bitmap(bin_path):
    """Load target bitmap and convert to grid format.

    Returns: 2D numpy array [128 x 256] matching GDS grid layout
    """
    data = Path(bin_path).read_bytes()
    assert len(data) == 4096, f"Expected 4096 bytes, got {len(data)}"

    grid = np.zeros((N_ROWS, N_COLS), dtype=bool)

    for y in range(128):  # row = wordline
        # GDS Y increases upward; ROM row 0 is at bottom of GDS.
        # Flip Y so the image appears right-side up in GDS viewers.
        gds_row = N_ROWS - 1 - y
        for x_lo in range(16):  # addr[4:1]
            addr = y * 32 + x_lo * 2  # addr[0] = 0
            byte_val = data[addr]
            for bit in range(8):  # output bit
                pixel = (byte_val >> bit) & 1
                if pixel:
                    # Map to GDS grid column
                    col_0 = bit * 32 + x_lo * 2  # addr[0] = 0
                    col_1 = bit * 32 + x_lo * 2 + 1  # addr[0] = 1
                    grid[gds_row, col_0] = True
                    grid[gds_row, col_1] = True

    return grid


def modify_gds(gds_path, target_grid, output_path=None):
    """Modify the GDS to match target bitmap.

    1. Read GDS
    2. Find rom_vga_logo_core cell
    3. Remove all existing diffusion polygons in ROM array
    4. Add new diffusion polygons where target = 1
    5. Write modified GDS
    """
    if output_path is None:
        output_path = gds_path

    # Read GDS (handle gzip)
    print(f"Reading GDS: {gds_path}")
    if str(gds_path).endswith(".gz"):
        # Decompress first
        import tempfile
        with gzip.open(gds_path, 'rb') as gz:
            tmp = tempfile.NamedTemporaryFile(suffix=".gds", delete=False)
            tmp.write(gz.read())
            tmp.close()
            library = gdstk.read_gds(tmp.name)
            Path(tmp.name).unlink()
    else:
        library = gdstk.read_gds(str(gds_path))

    print(f"  Library: {library.name}, {len(library.cells)} cells")
    for cell in library.cells:
        n_poly = len(cell.polygons)
        n_ref = len(cell.references)
        print(f"    {cell.name}: {n_poly} polygons, {n_ref} references")

    # Find core cell
    core_cell = find_rom_core_cell(library)
    if core_cell is None:
        print("ERROR: Could not find rom_vga_logo_core cell!")
        return False

    print(f"\nAnalyzing {core_cell.name}...")

    # Find existing diffusion polygons
    diff_polys = find_diffusion_polygons(core_cell)
    if not diff_polys:
        print("  No diffusion polygons found on layer 65/20!")
        print("  Checking all layers...")
        layer_counts = {}
        for poly in core_cell.polygons:
            key = (poly.layer, poly.datatype)
            layer_counts[key] = layer_counts.get(key, 0) + 1
        for key, count in sorted(layer_counts.items()):
            print(f"    Layer {key[0]}/{key[1]}: {count} polygons")
        return False

    # Analyze grid
    grid_info = analyze_rom_grid(diff_polys)
    if grid_info is None:
        return False

    # Build current occupancy
    current_grid = build_occupancy_grid(diff_polys, grid_info)
    print(f"  Current occupancy: {np.sum(current_grid)}/{current_grid.size} cells")

    # Check grid dimensions
    n_rows = len(grid_info["y_positions"])
    n_cols = len(grid_info["x_positions"])
    if n_rows != N_ROWS or n_cols != N_COLS:
        print(f"  WARNING: Grid is {n_cols}x{n_rows}, expected {N_COLS}x{N_ROWS}")
        if n_rows < N_ROWS or n_cols < N_COLS:
            print("  Grid too small, cannot proceed!")
            return False

    # Compute changes
    target_trimmed = target_grid[:n_rows, :n_cols]
    to_add = target_trimmed & ~current_grid
    to_remove = current_grid & ~target_trimmed
    print(f"  Target occupancy: {np.sum(target_trimmed)}/{target_trimmed.size} cells")
    print(f"  To add: {np.sum(to_add)}, To remove: {np.sum(to_remove)}")

    # Remove ROM-sized diffusion polygons that should be empty
    x_pos = grid_info["x_positions"]
    y_pos = grid_info["y_positions"]
    tolerance = min(grid_info["x_pitch"], grid_info["y_pitch"]) * 0.3

    # Build set of ROM polygon IDs to remove
    polys_to_remove = set()
    for i, (poly, bbox) in enumerate(diff_polys):
        cx = (bbox[0][0] + bbox[1][0]) / 2
        cy = (bbox[0][1] + bbox[1][1]) / 2
        col = np.argmin(np.abs(x_pos - cx))
        row = np.argmin(np.abs(y_pos - cy))
        if (abs(x_pos[col] - cx) < tolerance and
            abs(y_pos[row] - cy) < tolerance and
            to_remove[row, col]):
            polys_to_remove.add(id(poly))

    # Remove ROM polygons that should be empty using cell.remove()
    polys_to_remove_list = [poly for poly, bbox in diff_polys
                            if id(poly) in polys_to_remove]
    if polys_to_remove_list:
        core_cell.remove(*polys_to_remove_list)
    removed_count = len(polys_to_remove_list)

    print(f"  Removed {removed_count} diffusion polygons")

    # Add new diffusion polygons
    cw = grid_info["cell_width"]
    ch = grid_info["cell_height"]
    added_count = 0
    for row in range(n_rows):
        for col in range(n_cols):
            if to_add[row, col]:
                cx = x_pos[col]
                cy = y_pos[row]
                rect = gdstk.rectangle(
                    (cx - cw / 2, cy - ch / 2),
                    (cx + cw / 2, cy + ch / 2),
                    layer=DIFF_LAYER,
                    datatype=DIFF_DATATYPE,
                )
                core_cell.add(rect)
                added_count += 1

    print(f"  Added {added_count} diffusion polygons")

    # Write output
    if str(output_path).endswith(".gz"):
        # Write to temp, then gzip
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=".gds", delete=False)
        tmp.close()
        library.write_gds(tmp.name)
        with open(tmp.name, 'rb') as f_in:
            with gzip.open(str(output_path), 'wb') as f_out:
                f_out.write(f_in.read())
        Path(tmp.name).unlink()
    else:
        library.write_gds(str(output_path))

    print(f"  Written to {output_path}")
    return True


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Modify ROM GDS layout")
    parser.add_argument("--bitmap", default=None,
                        help="Path to target bitmap binary (default: artwork/logo_1bpp.bin)")
    parser.add_argument("--gds", default=None,
                        help="Path to input GDS file (default: macro/rom_vga_logo.gds.gz)")
    parser.add_argument("--output", default=None,
                        help="Path to output GDS (default: overwrite input)")
    parser.add_argument("--analyze-only", action="store_true",
                        help="Only analyze the GDS, don't modify")
    args = parser.parse_args()

    base_dir = Path(__file__).parent.parent
    bitmap_path = args.bitmap or str(base_dir / "artwork" / "logo_1bpp.bin")
    gds_path = args.gds or str(base_dir / "macro" / "rom_vga_logo.gds.gz")
    output_path = args.output or gds_path

    if args.analyze_only:
        print(f"Analyzing GDS: {gds_path}")
        if str(gds_path).endswith(".gz"):
            import tempfile
            with gzip.open(gds_path, 'rb') as gz:
                tmp = tempfile.NamedTemporaryFile(suffix=".gds", delete=False)
                tmp.write(gz.read())
                tmp.close()
                library = gdstk.read_gds(tmp.name)
                Path(tmp.name).unlink()
        else:
            library = gdstk.read_gds(str(gds_path))

        core_cell = find_rom_core_cell(library)
        if core_cell:
            diff_polys = find_diffusion_polygons(core_cell)
            grid_info = analyze_rom_grid(diff_polys)
            if grid_info:
                current_grid = build_occupancy_grid(diff_polys, grid_info)
                print(f"\n  Occupancy: {np.sum(current_grid)}/{current_grid.size}")
                # Print visual representation (8x8 blocks)
                n_rows = len(grid_info["y_positions"])
                n_cols = len(grid_info["x_positions"])
                print(f"\n  Visual (1 char = 4x4 block of cells):")
                for r in range(0, n_rows, 4):
                    line = "  "
                    for c in range(0, n_cols, 4):
                        block = current_grid[r:r+4, c:c+4]
                        density = np.mean(block)
                        if density > 0.75:
                            line += "#"
                        elif density > 0.25:
                            line += "."
                        else:
                            line += " "
                    print(line)
        return

    print(f"Loading target bitmap: {bitmap_path}")
    target_grid = load_target_bitmap(bitmap_path)
    print(f"  Target: {np.sum(target_grid)}/{target_grid.size} cells set")

    success = modify_gds(gds_path, target_grid, output_path)
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
