// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 andam0202
//
// inkdeus の連携スクリプトの部品: 全ページを印刷向けの PDF 1つにまとめる。
// 各ページを漫画エディタの書き出し関数で PNG にして 1 枚ずつサーバへ送り、サーバが PDF にまとめる
// （ブラウザのメモリに全ページを溜めない）。漫画エディタの原稿サイズ・DPI の設定には触らない。
(function () {
  'use strict';
  const bridge = window.inkdeusBridge;
  if (!bridge || !bridge.x || bridge.exportPdf) return;
  /* global canvas, btmGetGuids, getCanvasGUID, btmSaveCurrentPage, chengeCanvasByGuid, btmWaitForPageReady,
     btmUpdateHandleText, btmProjectsMap */
  const { state, ui, store, el, api, say, run, optional, nextFrames, commands, requireFeature, busyWhile, exportName,
    renderPagePng, markClean } = bridge.x;

  const SIZES = { B5: [182, 257], A5: [148, 210], A4: [210, 297], B6: [128, 182] };
  const DEFAULTS = { size: 'B5', widthMm: 182, heightMm: 257, matchAspect: false, dpi: 600, bleedMm: 0, fit: 'contain',
    color: 'gray', threshold: 128, binding: 'right', layout: 'single' };
  const MAX_SIDE = 16384;                    // ブラウザが 1 枚の画像として作れる大きさの目安
  const MAX_AREA = 250e6;
  const supported = (state.server || []).includes('mangaPdf');
  const job = { running: false, abort: false, name: '' };

  // ページの範囲（例 "1-8,10"）→ 昇順の番号の並び。空は全ページ
  function parsePages(spec, total) {
    if (spec == null || !String(spec).trim()) return Array.from({ length: total }, (_, i) => i + 1);
    const found = new Set();
    String(spec).split(',').forEach((part) => {
      const m = part.match(/^\s*(\d+)\s*(?:-\s*(\d+)\s*)?$/);
      if (!m) throw new Error(`ページの範囲として読めません: ${part.trim() || '（空）'}（例: 1-8,10）`);
      const first = parseInt(m[1], 10);
      const last = parseInt(m[2] || m[1], 10);
      if (first < 1 || last < first) throw new Error(`ページの範囲がおかしいです: ${part.trim()}`);
      if (last > total) throw new Error(`${last} ページはありません（全 ${total} ページ）`);
      for (let n = first; n <= last; n++) found.add(n);
    });
    return Array.from(found).sort((a, b) => a - b);
  }

  // そのページを書き出す倍率: 「仕上がり＋塗り足し」の画素数にちょうど足りる大きさ（サーバの計算と同じ式）
  function multiplierFor(settings, cw, ch) {
    let w = settings.widthMm;
    let h = settings.heightMm;
    if ((cw > ch) !== (w > h)) { const t = w; w = h; h = t; }
    if (settings.matchAspect) {
      const s = Math.min(w / cw, h / ch);
      w = cw * s;
      h = ch * s;
    }
    const fullW = ((w + settings.bleedMm * 2) / 25.4) * settings.dpi;
    const fullH = ((h + settings.bleedMm * 2) / 25.4) * settings.dpi;
    const wanted = settings.fit === 'cover' ? Math.max(fullW / cw, fullH / ch) : Math.min(fullW / cw, fullH / ch);
    const limit = Math.min(MAX_SIDE / Math.max(cw, ch), Math.sqrt(MAX_AREA / (cw * ch)));
    return { multiplier: Math.min(wanted, limit), limited: wanted > limit, pixels: [Math.round(fullW), Math.round(fullH)] };
  }

  async function exportPdf(opts) {
    const o = opts || {};
    if (!supported) throw new Error('サーバが PDF の書き出しに対応していません（inkdeus を新しい版で起動し直してください）');
    requireFeature('exportAll');
    if (job.running) throw new Error('PDF の書き出しは既に進行中です');
    const name = exportName(o.name);
    const total = btmGetGuids().length;
    const numbers = parsePages(o.pages, total);
    const base = '/api/manga/pdf/' + encodeURIComponent(name);
    job.running = true;
    job.abort = false;
    job.name = name;
    renderResult(null);
    renderProgress('準備しています…');
    const progress = (done) => {
      const text = `PDF を書き出しています…（${done} / ${numbers.length} ページ）`;
      renderProgress(text);
      say(text, 'work');
      if (o.onProgress) o.onProgress({ done, total: numbers.length });
    };
    let begun = false;
    try {
      const started = await api('POST', base + '/begin', JSON.stringify(Object.assign({ title: name }, o.settings || {})));
      begun = true;
      const settings = started.settings;
      let limited = false;
      await busyWhile('PDF を書き出しています…', async () => {
        const start = getCanvasGUID();
        await btmSaveCurrentPage(false);                       // いまのページを確定してから離れる
        const guids = btmGetGuids();
        try {
          for (let i = 0; i < numbers.length; i++) {
            if (job.abort) throw Object.assign(new Error('PDF の書き出しを中止しました'), { code: 'aborted' });
            progress(i);
            const guid = guids[numbers[i] - 1];
            if (getCanvasGUID() !== guid) {
              await chengeCanvasByGuid(guid);
              await btmWaitForPageReady();
              await nextFrames(2);
            }
            const m = multiplierFor(settings, canvas.width, canvas.height);
            limited = limited || m.limited;
            let png = await renderPagePng(m.multiplier);
            if (png.blob.size < 100) throw new Error(`ブラウザがこの大きさの画像を作れません（${m.pixels.join('×')} px）。解像度を下げてください`);
            await api('POST', `${base}/page/${numbers[i]}`, png.blob);
            png = null;                                        // 次のページの前に手放す
          }
        } finally {
          if (getCanvasGUID() !== start && btmProjectsMap.has(start)) {
            await chengeCanvasByGuid(start);
            await btmWaitForPageReady();
            optional(() => btmUpdateHandleText());
          }
        }
      });
      if (job.abort) throw Object.assign(new Error('PDF の書き出しを中止しました'), { code: 'aborted' });
      progress(numbers.length);
      const info = await api('POST', base + '/finish', '{}');
      begun = false;
      await nextFrames(2);
      markClean();
      info.limited = limited;
      renderResult(info);
      say(`PDF を書き出しました: ${info.pages} ページ → ${info.path}`
        + (limited ? '（ブラウザの上限のため、一部のページは小さく書き出して拡大しました）' : ''));
      return info;
    } catch (e) {
      if (begun) await api('POST', base + '/abort', '{}').catch(() => {});
      if (e && e.code === 'aborted') { say(e.message); renderProgress(''); }
      throw e;
    } finally {
      job.running = false;
      renderProgress('');
    }
  }

  function abortPdf() {
    const was = job.running;
    job.abort = true;
    return { aborting: was, name: job.name };
  }

  // ---- 画面 ----

  const view = {};

  function renderProgress(text) {
    if (!view.progress) return;
    view.progress.hidden = !text;
    view.progressText.textContent = text || '';
    view.button.disabled = job.running || !supported || !state.features.exportAll;
    view.abort.disabled = !job.running;
  }

  function renderResult(info) {
    view.result.hidden = !info;
    if (!info) return;
    view.result.replaceChildren(
      el('span', { class: 'ikb-dim ikb-pdf-path', text: `PDF: ${info.path}`, title: info.path }),
      el('a', { class: 'ikb-link ikb-pdf-open', href: info.url, target: '_blank', rel: 'noopener', text: '開く' }));
  }

  function field(label, control, note) {
    return el('label', { class: 'ikb-field' }, [el('span', { text: label }), control]
      .concat(note ? [el('span', { class: 'ikb-dim ikb-field-note', text: note })] : []));
  }
  function select(id, options) {
    return el('select', { id }, options.map(([value, text]) => el('option', { value: String(value), text })));
  }
  function number(id, min, max, step) {
    return el('input', { type: 'number', id, min: String(min), max: String(max), step: String(step) });
  }

  function buildDialog() {
    const d = view;
    d.pages = el('input', { type: 'text', id: 'ikb-pdf-pages', placeholder: '空欄 = 全ページ（例 1-8,10）' });
    d.size = select('ikb-pdf-size', Object.entries(SIZES).map(([k, [w, h]]) => [k, `${k}（${w}×${h} mm）`])
      .concat([['custom', 'カスタム']]));
    d.width = number('ikb-pdf-width', 10, 2000, 0.1);
    d.height = number('ikb-pdf-height', 10, 2000, 0.1);
    d.custom = el('div', { class: 'ikb-row ikb-pdf-custom' }, [d.width, el('span', { text: '×' }), d.height, el('span', { text: 'mm' })]);
    d.match = el('input', { type: 'checkbox', id: 'ikb-pdf-match' });
    d.dpi = select('ikb-pdf-dpi', [[350, '350 dpi'], [600, '600 dpi'], [1200, '1200 dpi'], ['custom', 'カスタム']]);
    d.dpiCustom = number('ikb-pdf-dpi-custom', 72, 2400, 1);
    d.bleed = select('ikb-pdf-bleed', [[0, 'なし'], [3, '3 mm'], [5, '5 mm']]);
    d.fit = select('ikb-pdf-fit', [['contain', '収める（余白は白）'], ['cover', '埋める（はみ出しは切る）']]);
    d.color = select('ikb-pdf-color', [['gray', 'グレースケール'], ['bw', '白黒2値'], ['rgb', 'そのまま（RGB）']]);
    d.threshold = number('ikb-pdf-threshold', 1, 254, 1);
    d.thresholdRow = field('しきい値', d.threshold, 'これより暗い所が黒（1〜254）');
    d.binding = select('ikb-pdf-binding', [['right', '右綴じ（右から左へ読む）'], ['left', '左綴じ']]);
    d.layout = select('ikb-pdf-layout', [['single', '1ページずつ'], ['spread-cover', '見開き（表紙だけ単独）'], ['spread', '見開き']]);
    d.name = el('input', { type: 'text', id: 'ikb-pdf-name', maxlength: '64' });
    d.error = el('div', { class: 'ikb-msg', 'data-kind': 'error', hidden: true });
    d.ok = el('button', { type: 'submit', class: 'ikb-primary', id: 'ikb-pdf-ok', text: '書き出す' });
    d.form = el('form', { method: 'dialog', class: 'ikb-form' }, [
      el('h3', { text: 'PDF を書き出す' }),
      field('対象のページ', d.pages),
      field('仕上がりサイズ', d.size),
      d.custom,
      el('label', { class: 'ikb-check' }, [d.match, el('span', { text: 'ページの縦横比に合わせる（余白なしで収まる最大）' })]),
      field('解像度', d.dpi, '白黒の線画は 600 dpi 以上がおすすめです'),
      d.dpiCustom,
      field('塗り足し', d.bleed, '塗り足しありのとき、絵は「仕上がり＋塗り足し」いっぱいに置かれます（トンボは描きません）'),
      field('はめ込み方', d.fit),
      field('色', d.color),
      d.thresholdRow,
      field('綴じ方向', d.binding),
      field('開いたときの表示', d.layout),
      field('出力名', d.name),
      d.error,
      el('div', { class: 'ikb-row ikb-dialog-buttons' }, [
        el('button', { type: 'button', text: 'やめる', onclick: () => d.dialog.close() }), d.ok]),
    ]);
    d.dialog = el('dialog', { class: 'ikb-dialog', id: 'ikb-pdf-dialog' }, [d.form]);
    const sync = () => {
      d.custom.hidden = d.size.value !== 'custom';
      d.dpiCustom.hidden = d.dpi.value !== 'custom';
      d.thresholdRow.hidden = d.color.value !== 'bw';
    };
    [d.size, d.dpi, d.color].forEach((node) => node.addEventListener('change', sync));
    d.sync = sync;
    d.form.addEventListener('submit', (e) => {
      e.preventDefault();
      let settings;
      try {
        settings = readDialog();
        exportName(d.name.value);
        parsePages(d.pages.value, btmGetGuids().length);
      } catch (err) {
        d.error.textContent = err.message;
        d.error.hidden = false;
        return;
      }
      store.set('pdf', settings);
      d.dialog.close();
      run(() => exportPdf({ name: d.name.value, pages: d.pages.value, settings }));
    });
    ui.root.appendChild(d.dialog);
  }

  function readDialog() {
    const d = view;
    const dpi = d.dpi.value === 'custom' ? parseFloat(d.dpiCustom.value) : parseFloat(d.dpi.value);
    if (!(dpi >= 72 && dpi <= 2400)) throw new Error('解像度は 72〜2400 dpi で指定してください');
    const settings = { size: d.size.value, matchAspect: d.match.checked, dpi, bleedMm: parseFloat(d.bleed.value),
      fit: d.fit.value, color: d.color.value, threshold: parseInt(d.threshold.value, 10) || 128,
      binding: d.binding.value, layout: d.layout.value };
    if (settings.size === 'custom') {
      settings.widthMm = parseFloat(d.width.value);
      settings.heightMm = parseFloat(d.height.value);
      if (!(settings.widthMm >= 10 && settings.heightMm >= 10)) throw new Error('カスタムの仕上がりサイズ（mm）を入れてください');
    }
    return settings;
  }

  function openDialog() {
    const d = view;
    const s = Object.assign({}, DEFAULTS, store.get('pdf', {}));
    d.size.value = SIZES[s.size] || s.size === 'custom' ? s.size : 'B5';
    d.width.value = String(s.widthMm || 182);
    d.height.value = String(s.heightMm || 257);
    d.match.checked = !!s.matchAspect;
    const preset = [350, 600, 1200].includes(s.dpi);
    d.dpi.value = preset ? String(s.dpi) : 'custom';
    d.dpiCustom.value = String(s.dpi);
    d.bleed.value = [0, 3, 5].includes(s.bleedMm) ? String(s.bleedMm) : '0';
    d.fit.value = s.fit;
    d.color.value = s.color;
    d.threshold.value = String(s.threshold);
    d.binding.value = s.binding;
    d.layout.value = s.layout;
    d.pages.value = '';
    d.name.value = ui.name.value.trim() || state.name || 'manga';
    d.error.hidden = true;
    d.sync();
    d.dialog.showModal();
  }

  function build() {
    view.button = el('button', { type: 'button', id: 'ikb-pdf', text: 'PDF を書き出す…',
      title: '全ページ（または範囲）を、印刷向けの PDF 1つにまとめます', onclick: () => run(() => openDialog()) });
    view.progressText = el('span', { class: 'ikb-dim' });
    view.abort = el('button', { type: 'button', id: 'ikb-pdf-abort', text: '中止', disabled: true, onclick: () => abortPdf() });
    view.progress = el('div', { class: 'ikb-row ikb-pdf-progress', hidden: true }, [view.progressText, view.abort]);
    view.result = el('div', { class: 'ikb-row ikb-pdf-result', hidden: true });
    const row = el('div', { class: 'ikb-row ikb-pdf-row' }, [view.button]);
    const anchor = ui.btnExportAll.parentNode;
    anchor.after(row, view.progress, view.result);
    if (!supported) {
      // 古いサーバ（PDF をまとめる API が無い）では出さない
      row.hidden = true;
      return;
    }
    buildDialog();
    renderProgress('');
  }

  build();
  bridge.exportPdf = exportPdf;
  bridge.abortPdf = abortPdf;
  bridge.pdfSizes = () => Object.assign({}, SIZES);
  bridge.listPdfs = () => api('GET', '/api/manga/pdf');
  commands.pdf = (args, report) => exportPdf({ name: args.name, pages: args.pages, settings: args.settings, onProgress: report });
  commands['pdf-abort'] = () => abortPdf();
})();
