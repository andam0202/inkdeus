// 8bit の PNG（グレースケール / RGB）のエンコーダ（canvas.toBlob は RGBA になるので自前で書く）。
// 圧縮はブラウザ内蔵の CompressionStream('deflate')（zlib 形式 = PNG の IDAT そのまま）。

const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

function crc32(parts) {
  let c = 0xffffffff;
  for (const buf of parts) {
    for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  }
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const head = new Uint8Array(8);
  const view = new DataView(head.buffer);
  view.setUint32(0, data.length);
  for (let i = 0; i < 4; i++) head[4 + i] = type.charCodeAt(i);
  const tail = new Uint8Array(4);
  new DataView(tail.buffer).setUint32(0, crc32([head.subarray(4), data]));
  return [head, data, tail];
}

/**
 * @param {number} width
 * @param {number} height
 * @param {(y0: number, rows: number) => Uint8Array} readRows  上から rows 行ぶんの画素（rows*width バイト）
 * @returns {Promise<Blob>}
 */
export function encodeGrayPNG(width, height, readRows) {
  return encodePNG(width, height, 1, readRows);
}

/**
 * @param {number} width
 * @param {number} height
 * @param {1|3} channels  1 = グレースケール、3 = RGB
 * @param {(y0: number, rows: number) => Uint8Array} readRows  上から rows 行ぶんの画素（rows*width*channels バイト）
 * @returns {Promise<Blob>}
 */
export async function encodePNG(width, height, channels, readRows) {
  if (typeof CompressionStream === 'undefined') {
    throw new Error('このブラウザは CompressionStream に未対応のため PNG を書き出せません');
  }
  const cs = new CompressionStream('deflate');
  const compressed = new Response(cs.readable).arrayBuffer();
  const writer = cs.writable.getWriter();
  const STRIP = Math.max(1, Math.floor((1 << 22) / width));
  for (let y0 = 0; y0 < height; y0 += STRIP) {
    const rows = Math.min(STRIP, height - y0);
    const px = readRows(y0, rows);
    const line = width * channels;
    const raw = new Uint8Array(rows * (line + 1));      // 各行の先頭にフィルタ種別 0（None）
    for (let r = 0; r < rows; r++) {
      raw.set(px.subarray(r * line, (r + 1) * line), r * (line + 1) + 1);
    }
    await writer.write(raw);
  }
  await writer.close();
  const idat = new Uint8Array(await compressed);

  const ihdr = new Uint8Array(13);
  const view = new DataView(ihdr.buffer);
  view.setUint32(0, width);
  view.setUint32(4, height);
  ihdr[8] = 8;      // ビット深度
  ihdr[9] = channels === 3 ? 2 : 0;      // カラータイプ 0 = グレースケール、2 = RGB
  const signature = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  return new Blob(
    [signature, ...chunk('IHDR', ihdr), ...chunk('IDAT', idat), ...chunk('IEND', new Uint8Array(0))],
    { type: 'image/png' });
}
