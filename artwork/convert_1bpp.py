#!/usr/bin/env python3
"""
Convert an image to 128x128 monochrome for the ROM VGA screensaver.

ROM address mapping (pixel → ROM):
  addr[11:5] = y[6:0]     (wordline / GDS Y)
  addr[4:1]  = x[3:0]     (sub-bitline pair / GDS X within block)
  addr[0]    = 0 or 1     (duplicated for clean GDS pattern)
  output_bit = x[6:4]     (which q[] bit / GDS X block)

Output files:
  artwork/logo_1bpp.bin   - raw binary (4096 bytes, one per ROM address)
  src/logo_1bpp.hex       - Verilog $readmemh format (4096 hex lines)
"""

import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("Pillow not installed. Install with: pip install Pillow")
    sys.exit(1)

SIZE = 128


def image_to_bitmap(img_path):
    """Load image, resize to 128x128, threshold to monochrome.
    Returns a 128x128 list-of-lists (row-major), 1=white, 0=black."""
    img = Image.open(img_path).convert("L")  # grayscale
    img = img.resize((SIZE, SIZE), Image.LANCZOS)
    pixels = img.load()
    bitmap = []
    for y in range(SIZE):
        row = []
        for x in range(SIZE):
            row.append(0 if pixels[x, y] >= 128 else 1)
        bitmap.append(row)
    return bitmap


def checkerboard_bitmap(block_size=16):
    """Generate a checkerboard test pattern."""
    bitmap = []
    for y in range(SIZE):
        row = []
        for x in range(SIZE):
            row.append(1 if ((x // block_size) + (y // block_size)) % 2 == 0 else 0)
        bitmap.append(row)
    return bitmap


def gradient_bitmap():
    """Generate a gradient test pattern (vertical bars of increasing width)."""
    bitmap = []
    for y in range(SIZE):
        row = []
        for x in range(SIZE):
            # Top half: vertical gradient (columns)
            # Bottom half: horizontal gradient (rows)
            if y < 64:
                row.append(1 if x % 2 == 0 or x > y * 2 else 0)
            else:
                row.append(1 if (x + y) % 4 < 2 else 0)
        bitmap.append(row)
    return bitmap


def bitmap_to_rom(bitmap):
    """Convert 128x128 bitmap to 4096-byte ROM data.

    ROM generator maps: wordlines → GDS X, bitlines → GDS Y.
    To make the image appear correctly in GDS:
      addr[11:5] = x[6:0]     → wordline → GDS X (image column)
      addr[4:1]  = y[3:0]     → sub-bitline pair → GDS Y (image row low bits)
      addr[0]    = LSB        → duplicated (same data for 0 and 1)
      output_bit = y[6:4]     → bitline block → GDS Y (image row high bits)

    Y is flipped (127-y) so image appears right-side up in GDS
    (GDS Y increases upward, image Y increases downward).
    RTL compensates with ~y in the address/mux.
    """
    rom_data = bytearray(4096)
    for x in range(SIZE):                # addr[11:5] = x → wordline → GDS X
        for y_lo in range(16):           # addr[4:1] = y[3:0] → sub-bitline
            byte_val = 0
            for bit in range(8):         # q[bit] = y[6:4] → bitline block
                py = bit * 16 + y_lo     # image y coordinate
                # Rotate 180 for GDS orientation (flip both X and Y)
                pixel = bitmap[py][SIZE - 1 - x]
                byte_val |= (pixel << bit)
            # Write same value for addr[0]=0 and addr[0]=1
            addr_base = x * 32 + y_lo * 2
            rom_data[addr_base + 0] = byte_val
            rom_data[addr_base + 1] = byte_val
    return rom_data


def write_outputs(rom_data, bin_path, hex_path):
    """Write binary and hex output files."""
    with open(bin_path, "wb") as f:
        f.write(rom_data)

    with open(hex_path, "w") as f:
        for byte in rom_data:
            f.write(f"{byte:02X}\n")


def main():
    script_dir = Path(__file__).parent
    bin_path = script_dir / "logo_1bpp.bin"
    hex_path = script_dir.parent / "src" / "logo_1bpp.hex"

    if len(sys.argv) > 1 and sys.argv[1] == "--checkerboard":
        print("Generating 128x128 checkerboard test pattern...")
        bitmap = checkerboard_bitmap()
    elif len(sys.argv) > 1 and sys.argv[1] == "--gradient":
        print("Generating 128x128 gradient test pattern...")
        bitmap = gradient_bitmap()
    elif len(sys.argv) > 1:
        img_path = sys.argv[1]
        print(f"Converting {img_path} to 128x128 monochrome...")
        bitmap = image_to_bitmap(img_path)
    else:
        # Default: use logo.png if it exists, otherwise checkerboard
        logo_path = script_dir / "logo.png"
        if logo_path.exists():
            print(f"Converting {logo_path} to 128x128 monochrome...")
            bitmap = image_to_bitmap(str(logo_path))
        else:
            print("No image found, generating checkerboard test pattern...")
            bitmap = checkerboard_bitmap()

    # Count pixels
    total_set = sum(sum(row) for row in bitmap)
    print(f"  Bitmap: {SIZE}x{SIZE}, {total_set}/{SIZE*SIZE} pixels set "
          f"({100*total_set/(SIZE*SIZE):.1f}%)")

    rom_data = bitmap_to_rom(bitmap)
    write_outputs(rom_data, bin_path, hex_path)

    print(f"  Binary: {bin_path} ({len(rom_data)} bytes)")
    print(f"  Hex:    {hex_path} ({len(rom_data)} lines)")

    # Save preview PNG
    preview_path = script_dir / "logo_1bpp_preview.png"
    try:
        preview = Image.new("1", (SIZE, SIZE))
        pixels = preview.load()
        for y in range(SIZE):
            for x in range(SIZE):
                pixels[x, y] = bitmap[y][x]
        preview.save(str(preview_path))
        print(f"  Preview: {preview_path}")
    except Exception as e:
        print(f"  (preview skipped: {e})")

    # Verify round-trip: extract bitmap back from ROM data
    # ROM stores 180-deg rotated: addr = {(127-x)[6:0], y[3:0], 0}, bit = y[6:4]
    errors = 0
    for y in range(SIZE):
        for x in range(SIZE):
            fx = SIZE - 1 - x  # flipped x (as stored in ROM)
            addr = fx * 32 + (y & 0xF) * 2  # addr[0]=0
            bit = (y >> 4) & 7
            extracted = (rom_data[addr] >> bit) & 1
            if extracted != bitmap[y][x]:
                errors += 1
    if errors:
        print(f"  WARNING: {errors} round-trip errors!")
    else:
        print(f"  Round-trip verification: PASS")


if __name__ == "__main__":
    main()
