#!/usr/bin/env node
/**
 * pngcrop.js —— 纯 node PNG 裁剪/放大（无第三方依赖）
 *   用法: node pngcrop.js <输入.png> <输出.png> <x> <y> <w> <h> [scale]
 *   用途: 只截一小块放大看，比读整屏省很多 token
 *   支持: 8bit 非隔行 PNG，颜色类型 0(灰)/2(RGB)/4(灰A)/6(RGBA)
 */
const fs = require('fs'), zlib = require('zlib');

function readPNG(buf) {
  if (buf.readUInt32BE(0) !== 0x89504e47) throw new Error('不是 PNG');
  let off = 8, w = 0, h = 0, bd = 0, ct = 0, inter = 0;
  const idat = [];
  while (off < buf.length) {
    const len = buf.readUInt32BE(off);
    const type = buf.toString('ascii', off + 4, off + 8);
    const data = buf.slice(off + 8, off + 8 + len);
    if (type === 'IHDR') {
      w = data.readUInt32BE(0); h = data.readUInt32BE(4);
      bd = data[8]; ct = data[9]; inter = data[12];
    } else if (type === 'IDAT') idat.push(data);
    else if (type === 'IEND') break;
    off += 12 + len;
  }
  if (bd !== 8) throw new Error('只支持 8bit，当前 ' + bd);
  if (inter) throw new Error('不支持隔行 PNG');
  const ch = { 0: 1, 2: 3, 4: 2, 6: 4 }[ct];
  if (!ch) throw new Error('不支持颜色类型 ' + ct);
  const raw = zlib.inflateSync(Buffer.concat(idat));
  const stride = w * ch;
  const out = Buffer.alloc(h * stride);
  let p = 0;
  for (let y = 0; y < h; y++) {
    const ft = raw[p++];
    const line = raw.slice(p, p + stride); p += stride;
    const cur = out.slice(y * stride, (y + 1) * stride);
    const prev = y > 0 ? out.slice((y - 1) * stride, y * stride) : Buffer.alloc(stride);
    for (let x = 0; x < stride; x++) {
      const a = x >= ch ? cur[x - ch] : 0, b = prev[x], c = x >= ch ? prev[x - ch] : 0;
      let v = line[x];
      if (ft === 1) v += a; else if (ft === 2) v += b; else if (ft === 3) v += (a + b) >> 1;
      else if (ft === 4) { const pa = Math.abs(b - c), pb = Math.abs(a - c), pc = Math.abs(a + b - 2 * c);
        v += (pa <= pb && pa <= pc) ? a : (pb <= pc ? b : c); }
      cur[x] = v & 0xff;
    }
  }
  return { w, h, ch, ct, px: out };
}

function writePNG(w, h, ch, px) {
  const stride = w * ch;
  const raw = Buffer.alloc(h * (stride + 1));
  for (let y = 0; y < h; y++) {
    raw[y * (stride + 1)] = 0;
    px.copy(raw, y * (stride + 1) + 1, y * stride, (y + 1) * stride);
  }
  const ctOut = ch === 4 ? 6 : ch === 3 ? 2 : ch === 2 ? 4 : 0;
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4);
  ihdr[8] = 8; ihdr[9] = ctOut; ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0;
  const chunk = (type, data) => {
    const b = Buffer.alloc(8 + data.length + 4);
    b.writeUInt32BE(data.length, 0); b.write(type, 4, 'ascii');
    data.copy(b, 8);
    b.writeUInt32BE(crc(Buffer.concat([Buffer.from(type, 'ascii'), data])) >>> 0, 8 + data.length);
    return b;
  };
  let table = null;
  function crc(buf) {
    if (!table) { table = []; for (let n = 0; n < 256; n++) { let c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; table[n] = c >>> 0; } }
    let c = 0xffffffff;
    for (let i = 0; i < buf.length; i++) c = table[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
  }
  return Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr), chunk('IDAT', zlib.deflateSync(raw)), chunk('IEND', Buffer.alloc(0))]);
}

const [inF, outF, X, Y, W, H, S] = process.argv.slice(2);
if (!inF || !outF) { console.log('用法: node pngcrop.js <输入.png> <输出.png> <x> <y> <w> <h> [scale]'); process.exit(2); }
const img = readPNG(fs.readFileSync(inF));
let x = Math.max(0, parseInt(X || 0, 10)), y = Math.max(0, parseInt(Y || 0, 10));
let w = Math.min(parseInt(W || img.w, 10), img.w - x), h = Math.min(parseInt(H || img.h, 10), img.h - y);
const s = S ? parseFloat(S) : 1;
if (!(w > 0 && h > 0)) { console.error('裁剪区域为空'); process.exit(1); }
const ow = Math.round(w * s), oh = Math.round(h * s);
const out = Buffer.alloc(ow * oh * img.ch);
for (let oy = 0; oy < oh; oy++) {
  const sy = Math.min(h - 1, Math.floor(oy / s));
  for (let ox = 0; ox < ow; ox++) {
    const sx = Math.min(w - 1, Math.floor(ox / s));
    img.px.copy(out, (oy * ow + ox) * img.ch, ((y + sy) * img.w + (x + sx)) * img.ch, ((y + sy) * img.w + (x + sx)) * img.ch + img.ch);
  }
}
fs.writeFileSync(outF, writePNG(ow, oh, img.ch, out));
console.log(`  ✅ ${outF}  ${ow}x${oh}（从 ${img.w}x${img.h} 裁 (${x},${y}) ${w}x${h} ×${s}）`);
