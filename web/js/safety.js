// データを失わないための画面まわりの部品: 知らせの帯、以前の状態に戻すダイアログ、筆のタイルの詰め方。

const el = (tag, props = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) node.setAttribute(k, v === true ? '' : v);
  }
  node.append(...children.filter((c) => c != null && c !== false));
  return node;
};

/**
 * 知らせの帯（ヘッダのすぐ下に出る、消えない知らせ）。key ごとに 1 本。
 * show(key, text, { kind: 'warn' | 'error', action: { label, run } })
 */
export function createNotices(root) {
  const bars = new Map();
  const sync = () => { root.hidden = bars.size === 0; };
  return {
    show(key, text, { kind = 'warn', action = null, detail = '' } = {}) {
      let bar = bars.get(key);
      if (!bar) {
        bar = el('div', { class: 'notice', 'data-key': key });
        bars.set(key, bar);
        root.append(bar);
      }
      bar.className = `notice ${kind}`;
      bar.replaceChildren(
        el('span', { class: 'notice-text', text }),
        detail ? el('span', { class: 'notice-detail', text: detail }) : null,
        action ? el('button', { type: 'button', class: 'notice-action', onclick: () => action.run(), text: action.label }) : null);
      sync();
    },
    hide(key) {
      const bar = bars.get(key);
      if (bar) { bar.remove(); bars.delete(key); }
      sync();
    },
    has: (key) => bars.has(key),
    keys: () => [...bars.keys()],
    text: (key) => (bars.has(key) ? bars.get(key).querySelector('.notice-text').textContent : null),
  };
}

/**
 * 画素の並び（channels バイト/画素）を連長圧縮する: [個数 1〜255, 値 × channels] の繰り返し。
 * 消去マスクやペンの絵は同じ値が続くので、1 筆ぶんのタイルが数 KB に収まる（ページを閉じる間際でも送り切れる大きさ）
 */
export function rleEncode(data, channels) {
  const n = data.length / channels;
  const out = new Uint8Array(n * (channels + 1));
  let o = 0;
  let i = 0;
  while (i < n) {
    const a = data[i * channels];
    const b = channels > 1 ? data[i * channels + 1] : 0;
    let run = 1;
    while (run < 255 && i + run < n && data[(i + run) * channels] === a
      && (channels === 1 || data[(i + run) * channels + 1] === b)) run += 1;
    out[o++] = run;
    out[o++] = a;
    if (channels > 1) out[o++] = b;
    i += run;
  }
  return out.subarray(0, o);
}

/**
 * タイルの並び [{x, y, w, h, data}] を、サーバへ送る本体にする
 * （先頭 4 バイト = 見出しの長さ、JSON の見出し、連長圧縮したタイルの画素）
 */
export function packTiles(width, height, channels, tiles) {
  const packed = tiles.map((t) => rleEncode(t.data, channels));
  const head = new TextEncoder().encode(JSON.stringify({ width, height, channels, encoding: 'rle',
    tiles: tiles.map((t, i) => ({ x: t.x, y: t.y, w: t.w, h: t.h, n: packed[i].length })) }));
  const body = new Uint8Array(4 + head.length + packed.reduce((sum, p) => sum + p.length, 0));
  new DataView(body.buffer).setUint32(0, head.length, true);
  body.set(head, 4);
  let at = 4 + head.length;
  for (const p of packed) { body.set(p, at); at += p.length; }
  return body;
}

/**
 * 選ぶダイアログ。rows = [{ label, note, value }]。選んだ value（やめたら null）を返す。
 * buttons = [{ label, value, primary }] を渡すと、一覧の代わりにボタンだけのダイアログになる
 */
export function choose({ title, message = '', rows = [], buttons = [], empty = '', cancel = 'やめる', id = 'chooser' }) {
  return new Promise((resolve) => {
    const dialog = el('dialog', { class: 'chooser', id });
    const close = (value) => { dialog.close(); dialog.remove(); resolve(value); };
    dialog.addEventListener('cancel', (e) => { e.preventDefault(); close(null); });
    dialog.append(
      el('h3', { text: title }),
      message ? el('p', { class: 'chooser-message', text: message }) : null,
      rows.length ? el('div', { class: 'chooser-list' }, ...rows.map((row) => el('button', {
        type: 'button', class: 'chooser-row', 'data-value': String(row.value), onclick: () => close(row.value) },
      el('span', { class: 'chooser-label', text: row.label }), el('span', { class: 'chooser-note', text: row.note || '' }))))
        : (empty ? el('p', { class: 'chooser-empty', text: empty }) : null),
      el('div', { class: 'chooser-buttons' },
        ...buttons.map((b) => el('button', { type: 'button', class: b.primary ? 'primary' : '', 'data-value': String(b.value),
          onclick: () => close(b.value), text: b.label })),
        el('button', { type: 'button', class: 'chooser-cancel', onclick: () => close(null), text: cancel })));
    document.body.append(dialog);
    dialog.showModal();
  });
}
