#!/usr/bin/env python3

#
# SKY130 ROM temporay content generator
#
# Copyright (C) 2026 Sylvain Munaut <tnt@246tNt.com>
# SPDX-License-Identifier: GPL-3.0-only
#

import pathlib
from collections import namedtuple

import numpy as np
import gdstk


DEBUG = False


class Rect(namedtuple('Rect', 'x0 x1 y0 y1')):

	__slots__ = []

	@property
	def width(self):
		return self.x1 - self.x0

	@property
	def height(self):
		return self.y1 - self.y0



class Geometry:

	def __init__(self, addr_bits, out_bits, n_bl_sel):
		# Store external parameters
		self.addr_bits = addr_bits
		self.out_bits  = out_bits

		# Validate bl group muxing ?
		if n_bl_sel not in [2, 3, 4, 5]:
			raise ValueError("Unsupported Bit-Line group muxing")

		# Save geometry parameters
		self.n_wl_sel = self.addr_bits - n_bl_sel
		self.n_bl_sel = n_bl_sel

		self.n_wl     = 1 << self.n_wl_sel
		self.n_bl_grp = 1 << self.n_bl_sel
		self.n_bl_tot = self.n_bl_grp * self.out_bits


class Content:

	def __init__(self, geom):
		self.geom = geom
		self.data = np.zeros([geom.n_wl, geom.n_bl_tot], dtype=np.bool_)

	def load_bin(self, fn, endian='little'):
		# Bytes per word
		bpw = (self.geom.out_bits + 7) // 8

		# Read input file
		with open(fn, 'rb') as fh:
			for addr in range(0, 1 << self.geom.addr_bits):
				# Get chunk
				chunk = fh.read(bpw)
				if len(chunk) < bpw:
					if len(chunk):
						print("[!] Data file not a multiple of word size\n")
					else:
						print("[!] Data file too short\n")
					break

				# Convert to word
				w = int.from_bytes(chunk, endian)

				# Store in data
				for bit in range(self.geom.out_bits):
					self.data[self.l2p(addr, bit)] = w & (1 << bit);

			if fh.read(1):
				print("[!] Data file is too long\n")

	def zero(self):
		self.data = np.zeros(self.data.shape, dtype=np.bool_)

	def randomize(self):
		self.data = np.random.randint(2, size=self.data.shape, dtype=np.bool_)

	def l2p(self, addr, bit):
		wl = addr >> self.geom.n_bl_sel
		bl = (bit << self.geom.n_bl_sel) | (addr & ((1 << self.geom.n_bl_sel) - 1))
		return (wl, bl)

	def p2l(self, wl, bl):
		addr = (wl << self.geom.n_bl_sel) | (bl & ((1 << self.geom.n_bl_sel) - 1))
		bit  = bl >> self.geom.n_bl_sel
		return (addr, bit)

	def get(self, wl, bl):
		return self.data[wl,bl]




BitGridRow = namedtuple('BitGridRow', 'type index rect')


class BitGrid:

	def __init__(self, gen):
		# Save params
		self.gen = gen
		self.lib = gen.lib

		# Load/generate cells
		self.cells = {}
		self.cells_generate()

	def new_cell(self, name):
		self.cells[name] = cell = gdstk.Cell(name)
		return cell

	def cells_generate(self):
		variants = {
			'nd'  : (False, None, None),
			'dnn' : (True,  None, None),
			'doo' : (True, 'O', 'O'),
			'doc' : (True, 'O', 'C'),
			'dco' : (True, 'C', 'O'),
			'dcc' : (True, 'C', 'C'),
		}

		for vn, (diff, cktl, cktr) in variants.items():
			# Create cell
			cell = self.new_cell(f'bg_cfg_{vn}')

			# Create boundary
			cell.add( gdstk.rectangle([0.175, 0], [0.675, 0.7], 235, 4) )

			# Create diffusion
			if diff:
				cell.add( gdstk.rectangle([0.08, 0.135], [0.77, 0.565], 65, 20) )

			# Create left contact
			if cktl is not None:
				ofs = 0.03 if (cktl == 'O') else 0
				cell.add( gdstk.rectangle([0.09+ofs, 0.265], [0.26+ofs, 0.435], 66, 44) )

			# Create right contact
			if cktr is not None:
				ofs = -0.03 if (cktr == 'O') else 0
				cell.add( gdstk.rectangle([0.59+ofs, 0.265], [0.76+ofs, 0.435], 66, 44) )

	def _mk_cfg(self):
		# Dimensions
		n_wl = self.gen.geom.n_wl
		n_bl = self.gen.geom.n_bl_tot

		# Create array of configuration bits
		data = self.gen.content.data

		cfg = np.zeros((n_wl * 2, n_bl // 2), dtype=np.bool_)

		cfg[0::4,] = data[0::2,0::2]	# Even bit cell left  (bl0)
		cfg[1::4,] = data[0::2,1::2]	# Even bit cell right (bl1)
		cfg[2::4,] = data[1::2,1::2]	# Odd  bit cell left  (bl1)
		cfg[3::4,] = data[1::2,0::2]	# Odd  bit cell right (bl0)

		# Required Diffusion mask
		diff = np.array(cfg)

		# Required Contacts mask
		ckt = np.zeros(((n_wl * 2) + 1, n_bl // 2), dtype=np.bool_)
		ckt[1:,:]   |= diff
		ckt[0:-1,:] |= diff

		# Skip density fill so transistor pattern is visible as silicon art
		# diff |= ~(ckt[1:,:] & ckt[0:-1,:])

		# Add dummy word lines left/right of diff array
		# to handle easily checking if left/right have diffusion or not
		diff = np.vstack([ np.zeros( (1, n_bl // 2) ), diff, np.zeros( (1, n_bl // 2) ) ])

		# Actually add all the cells
		bg_cfg = self.new_cell('bg_cfg')

		for row in self.rows:
			# Skip non bit-lines
			if row.type != 'bl':
				continue

			# Scan each columns
			for i in range(n_wl * 2):
				# Position
				xp = (self.pitch_x / 2) * i
				yp = row.rect.y0

				# Diffusion ?
				if diff[i+1, row.index]:
					# Both contacts ? (if not, contact will be added by neighbor cell)
					if ckt[i, row.index] and ckt[i+1, row.index]:
						clt = 'c' if diff[i,   row.index] else 'o'
						crt = 'c' if diff[i+2, row.index] else 'o'
						cn = 'd' + clt + crt
					else:
						cn = 'dnn'
				else:
					cn = 'nd'

				# Add config cell
				bg_cfg.add(gdstk.Reference(
					self.cells[f'bg_cfg_{cn}'],
					origin = (xp,yp)
				))

		return bg_cfg

	def layout(self):
		# Sub-cells pitch / height
		self.pitch_x = 1.0
		self.pitch_y = 0.7

		h_bl  = self.pitch_y
		h_w2p = 0.45
		h_tap = 0.7

		# Create the bit grid cell
		bg = self.new_cell('bg')

		# Create all columns (word lines)
		# This is uniform, following the pitch of the BitCell
		self.cols = self.pitch_x * np.arange(self.gen.geom.n_wl + 1)

		# Create all rows (bit lines)
		self.rows = []

		x0 = self.cols[0]
		x1 = self.cols[-1]
		y0 = 0.0

		n_w2p = 0
		n_tap = 0

			# Helpers
		def add_w2p():
			nonlocal y0, n_w2p
			self.rows.append(BitGridRow(
				'w2p', None,
				Rect(x0, x1, y0, y0 + h_w2p)
			))
			y0 += h_w2p
			n_w2p = 0

		def add_tap():
			nonlocal y0, n_tap
			self.rows.append(BitGridRow(
				'tap', None,
				Rect(x0, x1, y0, y0 + h_tap)
			))
			y0 += h_tap
			n_tap = 0

		def add_bl(idx):
			nonlocal y0
			self.rows.append(BitGridRow(
				'bl', idx,
				Rect(x0, x1, y0, y0 + h_bl)
			))
			y0 += h_bl

			# Start with a wl2poly
			# (no need for tap, the Word Line driver include it)
		add_w2p()

			# Scan each bitline
		for i in range(self.gen.geom.n_bl_tot // 2):
			if n_w2p == 8:
				add_w2p()

			if n_tap == 32:
				add_tap()

			add_bl(i)

			n_w2p += 1
			n_tap += 1

			# Add w2p if needed
		if n_w2p > 4:
			add_w2p()

			# Always finish with a tap
		if n_tap:
			add_tap()

		# Create the configuration
		bg.add(gdstk.Reference(self._mk_cfg(), (0.0, 0.0)))

		# Done
		return bg


class Generator:

	def __init__(self, geom, content):
		# Store geometry / content
		self.geom = geom
		self.content = content

		# Create GDS library
		self.lib = gdstk.Library()

	def bg(self):
		bg = BitGrid(self)
		bgl = bg.layout()
		bgl.flatten()
		bgl.filter([(235, 4)], True)
		return bgl


def main(argv0, version, name, data_file=None):

	# Geometry: (addr_bits, n_bl_sel, out_bits)
	VERS = {
		'rom_128x32x8': (12, 5,  8),
	}

	vdat = VERS[version]

	geom = Geometry(vdat[0], vdat[2], vdat[1])

	# Content
	content = Content(geom)

	if data_file is not None:
		content.load_bin(data_file)
	else:
		content.randomize()

	# BG Gen
	gen = Generator(geom, content)
	bg = gen.bg()

	# Load base
	lib  = gdstk.read_gds(f'data/{version}.gds')
	base = lib[f'{version}']
	core = lib[f'{version}_core']

	core.add(gdstk.Reference(
		bg,
		origin = (0.0, 0.0)
	))

	base.flatten()
	base.name = name

	lib_out = gdstk.Library()
	lib_out.add(base)
	lib_out.write_gds(f'{name}.gds')

	# Aux
	for ext in ['lib', 'lef']:
		data = open(f'data/{version}.{ext}', 'r').read()
		data = data.replace(version, name)
		open(f'{name}.{ext}', 'w').write(data)


if __name__ == '__main__':

	import sys
	main(*sys.argv)

