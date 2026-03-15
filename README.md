![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg) ![](../../workflows/test/badge.svg) ![](../../workflows/fpga/badge.svg)

# ROM VGA Screensaver with Silicon Art

A bouncing logo VGA screensaver for Tiny Tapeout, where the logo image is stored in a NOR ROM macro. The transistor pattern in the ROM is arranged so that the image is visible in die photos — silicon art.

Based on [tt-rom-vga-screensaver](https://github.com/urish/tt-rom-vga-screensaver) by Uri Shaked.

## How it works

A 128x128 monochrome bitmap is stored in a 4096x8-bit NOR ROM (32,768 cells). The VGA controller reads pixels from the ROM and displays a white-on-black bouncing logo at 640x480 @ 25MHz. Tiling and 2x scaling modes are available via input pins.

In the physical GDS, each ROM cell either has an NFET transistor (pixel = 1) or doesn't (pixel = 0). By controlling which cells have transistors, the TT logo appears as a visible pattern in the diffusion layer of the fabricated chip.

## ROM generation

The ROM macro is generated using the `rom_gen/` scripts. To regenerate with a different image:

```bash
# 1. Convert image to ROM binary (128x128 monochrome)
python3 artwork/convert_1bpp.py path/to/image.png

# 2. Generate ROM GDS macro from binary
cd rom_gen
python3 rom.py rom_128x32x8 rom_vga_logo ../artwork/logo_1bpp.bin

# 3. Copy generated files to macro directory
gzip -c rom_vga_logo.gds > ../macro/rom_vga_logo.gds.gz
cp rom_vga_logo.lef ../macro/rom_vga_logo.lef
# Keep the existing .lib (generator uses different port names)
```

The converter (`artwork/convert_1bpp.py`) handles the address mapping between image pixels and ROM cells, including orientation transforms so the image appears correctly in both the VGA output and the GDS layout.

### ROM address mapping

```
addr[11:5] = x[6:0]    -> wordline (GDS X axis)
addr[4:1]  = y[3:0]    -> sub-bitline pair (GDS Y axis)
addr[0]    = 0          -> duplicated
output_bit = y[6:4]    -> bitline block (GDS Y axis)
```

The RTL uses `~x` in the address to compensate for a 180-degree rotation applied in the ROM data (so the image appears right-side up in both the GDS viewer and on the VGA display).

## Pinout

| Pin | Function |
|-----|----------|
| ui_in[0] | Tile mode (fill screen with logo) |
| ui_in[4:6] | Gamepad PMOD (latch, clk, data) |
| uo_out[0] | R1 |
| uo_out[1] | G1 |
| uo_out[2] | B1 |
| uo_out[3] | VSync |
| uo_out[4] | R0 |
| uo_out[5] | G0 |
| uo_out[6] | B0 |
| uo_out[7] | HSync |
