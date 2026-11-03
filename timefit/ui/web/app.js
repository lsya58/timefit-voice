"use strict";
/* TimeFit Voice 画面。Python 側は window.pywebview.api.*、Python からの通知は tf.onEvent() */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const api = () => window.pywebview.api;

const STATUS = {
  pending: { label: "待機", glyph: "" },
  running: { label: "処理中", glyph: "" },
  natural: { label: "等倍", glyph: "✓" },
  pause: { label: "間を調整", glyph: "✓" },
  speed: { label: "話速調整", glyph: "»" },
  rewrite: { label: "要約", glyph: "✎" },
  failed: { label: "収まらず", glyph: "⚠" },
};
const LEGEND = [
  ["natural", "そのまま / 間を調整"],
  ["speed", "話速調整"],
  ["rewrite", "AI 要約"],
  ["failed", "収まらず"],
];

const state = {
  info: null,
  settings: null,
  speakers: [],
  file: null,       // preview の結果
  output: null,
  results: {},      // index → 結果
  running: false,
  runningIndex: null,
  done: false,
  mode: "exact",
  logs: [],
  playing: null,
};

/* ---------- 小物 ---------- */
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const sec = (v) => `${v.toFixed(2)}s`;
function clock(t) {
  const m = Math.floor(t / 60);
  const s = t - m * 60;
  return `${String(m).padStart(2, "0")}:${s.toFixed(1).padStart(4, "0")}`;
}
function niceStep(total) {
  const steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
  return steps.find((s) => total / s <= 8) || 900;
}

let toastTimer;
function toast(msg, isErr = false, ms = 4200) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("err", isErr);
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), ms);
}

function log(message) {
  state.logs.push(message);
  const body = $("#logBody");
  const line = document.createElement("div");
  if (message.includes("【エラー】")) line.className = "err";
  else if (message.includes("⚠") || message.startsWith("※")) line.className = "warn";
  line.textContent = message;
  body.appendChild(line);
  body.scrollTop = body.scrollHeight;
  $("#logLast").textContent = message.trim();
}

/* ---------- 起動 ---------- */
async function boot() {
  const info = await api().init();
  state.info = info;
  state.settings = info.settings;
  state.mode = info.settings.fit_mode;

  const badge = $("#editionBadge");
  badge.textContent = `${info.editionLabel} v${info.version}`;
  badge.classList.toggle("free", info.edition === "free");
  $("#footnote").textContent = info.freeLimit
    ? `無料版: 1回 ${info.freeLimit} 行まで処理できます（製品版は無制限）。`
    : "作った音声を使うときは、出力される credits.txt のクレジットを載せてください。";

  applySettingsToControls();
  if (info.configError) toast(info.configError, true);
  await refreshEngine();
}

function applySettingsToControls() {
  const s = state.settings;
  $$("#mode button").forEach((b) => b.classList.toggle("on", b.dataset.value === state.mode));
  $("#maxSpeed").value = s.max_speed;
  $("#speedValue").textContent = `${Number(s.max_speed).toFixed(2)}×`;
  $("#allowRewrite").checked = s.allow_rewrite;
  updateRewriteHint();
}

function updateRewriteHint() {
  const hint = $("#rewriteHint");
  if ($("#allowRewrite").checked && !state.settings.hasApiKey) {
    hint.textContent = "APIキーが未設定です（右上の設定から登録）";
    hint.classList.add("warn");
  } else {
    hint.textContent = "話速の上限でも収まらない行だけ";
    hint.classList.remove("warn");
  }
}

async function refreshEngine() {
  const pill = $("#engineStatus");
  pill.className = "engine-pill";
  pill.querySelector(".label").textContent = "接続を確認中…";
  const res = await api().speakers();
  const sel = $("#speaker");
  const current = Number(sel.value || state.settings.default_speaker);
  sel.innerHTML = "";
  if (res.ok) {
    state.speakers = res.items;
    const engine = state.settings.credit_prefix || "音声エンジン";
    pill.classList.add("ok");
    pill.querySelector(".label").textContent = `${engine} 接続中`;
    let lastChar = null, group = null;
    for (const sp of res.items) {
      if (sp.character !== lastChar) {
        group = document.createElement("optgroup");
        group.label = sp.character;
        sel.appendChild(group);
        lastChar = sp.character;
      }
      const opt = new Option(`${sp.character}（${sp.style}）`, sp.id);
      group.appendChild(opt);
    }
  } else {
    state.speakers = [];
    pill.classList.add("ng");
    pill.querySelector(".label").textContent = "エンジン未起動 · クリックで再接続";
    pill.title = res.error;
    sel.appendChild(new Option(`話者ID ${current}（エンジン未接続）`, current));
  }
  sel.value = String(current);
  if (!sel.value && sel.options.length) sel.selectedIndex = 0;
  if (state.file) renderRows();
}

function speakerName(id) {
  const sp = state.speakers.find((s) => s.id === id);
  return sp ? `${sp.character}（${sp.style}）` : `話者ID ${id}`;
}

/* ---------- ファイル ---------- */
function setFile(preview) {
  if (!preview) return;
  if (!preview.ok) { toast(preview.error, true, 6000); return; }
  if (state.running) { toast("処理中はファイルを変更できません。"); return; }
  state.file = preview;
  state.output = preview.output;
  state.results = {};
  state.done = false;
  $("#fileName").textContent = preview.name;
  $("#fileSub").textContent = `${preview.kind === "srt" ? "字幕" : "CSV"} · ${preview.lines.length} 行`;
  $("#pickFile").classList.remove("empty");
  $("#outputPath").textContent = state.output;
  $("#runBtn").disabled = false;
  $("#emptyState").classList.add("hidden");
  $("#timelineView").classList.remove("hidden");
  $("#playAll").classList.add("hidden");
  $("#openOutput").classList.add("hidden");
  renderTimeline();
}

function starts() {
  let t = 0;
  return state.file.lines.map((ln) => {
    const s = ln.start ?? t;
    t = s + ln.target;
    return s;
  });
}

/* ---------- タイムライン ---------- */
function renderTimeline() {
  const f = state.file;
  const st = starts();
  const total = Math.max(...f.lines.map((ln, i) => st[i] + ln.target));
  state.total = total;
  state.starts = st;

  $("#scriptTitle").textContent = f.name;
  const limit = state.info.freeLimit;
  let meta = `${f.kind === "srt" ? "字幕" : "CSV"} · ${f.lines.length} 行 · 全体 ${clock(total)}`;
  if (limit && f.lines.length > limit) meta += ` · 無料版は先頭 ${limit} 行まで`;
  $("#scriptMeta").textContent = meta;

  // 全体の時間軸
  const ov = $("#overview");
  ov.innerHTML = "";
  f.lines.forEach((ln, i) => {
    const clip = document.createElement("div");
    clip.className = "ov-clip";
    clip.dataset.index = ln.index;
    clip.style.left = `${(st[i] / total) * 100}%`;
    clip.style.width = `max(3px, calc(${(ln.target / total) * 100}% - 2px))`;
    clip.addEventListener("mouseenter", (e) => { showTip(e, ln.index); highlight(ln.index, true); });
    clip.addEventListener("mousemove", moveTip);
    clip.addEventListener("mouseleave", () => { hideTip(); highlight(ln.index, false); });
    clip.addEventListener("click", () => {
      document.querySelector(`.row[data-index="${ln.index}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" });
    });
    ov.appendChild(clip);
  });

  const ruler = $("#ruler");
  ruler.innerHTML = "";
  const step = niceStep(total);
  for (let t = 0; t <= total + 1e-6; t += step) {
    const tick = document.createElement("span");
    tick.style.left = `${(t / total) * 100}%`;
    tick.textContent = step < 1 ? `${t.toFixed(1)}s` : (total >= 60 ? clock(t).replace(/\.\d$/, "") : `${t}s`);
    ruler.appendChild(tick);
  }

  $("#legend").innerHTML = LEGEND.map(([k, label]) => `<span><i class="${k}"></i>${label}</span>`).join("");
  renderRows();
  renderStats();
}

function rowStatus(ln) {
  const r = state.results[ln.index];
  if (r) return r.statusKey;
  if (state.runningIndex === ln.index) return "running";
  return "pending";
}

function barHtml(ln, r) {
  const target = ln.target;
  if (!r) {
    return `<div class="bar"><div class="bar-track"><div class="bar-slot" style="width:100%"></div></div>
      <div class="bar-label"><span class="num">目標 ${sec(target)}</span><span class="knobs"></span></div></div>`;
  }
  const speech = r.speech;
  const scale = Math.max(target, speech);
  const slotW = (target / scale) * 100;
  const fillW = (Math.min(speech, target) / scale) * 100;
  const over = speech > target + 0.0005;
  const silence = Math.max(0, r.duration - speech);  // 「ぴったり」で足した無音
  const knobs = [`×${r.speed.toFixed(2)}`];
  if (r.pause < 0.999) knobs.push(`間 ${r.pause.toFixed(2)}`);
  let split;
  if (over) split = `発話 <b>${sec(speech)}</b> · <b>${sec(speech - target)}</b> 超過`;
  else if (silence >= 0.05) split = `発話 <b>${sec(speech)}</b> <span class="sil">＋ 無音 ${sec(silence)}</span> ＝ ${sec(r.duration)}`;
  else split = `発話 <b>${sec(speech)}</b> / 枠 ${sec(target)}`;
  return `<div class="bar">
    <div class="bar-track">
      <div class="bar-slot" style="width:${slotW}%"></div>
      <div class="bar-fill" style="width:${fillW}%"></div>
      ${silence >= 0.05 && !over ? `<div class="bar-silence" style="left:${fillW}%;width:${slotW - fillW}%"></div>` : ""}
      ${over ? `<div class="bar-over" style="left:${slotW}%;width:${100 - slotW}%"></div>` : ""}
      <div class="bar-edge" style="left:calc(${slotW}% - 1px)"></div>
    </div>
    <div class="bar-label"><span class="num split">${split}</span><span class="knobs">${knobs.join(" · ")}</span></div>
  </div>`;
}

function rowHtml(ln, i) {
  const r = state.results[ln.index];
  const st = rowStatus(ln);
  const info = STATUS[st];
  const text = r ? r.text : ln.text;
  const spk = ln.speakerFromFile ? `<span class="spk">${esc(speakerName(ln.speaker))}</span>` : "";
  const orig = r && r.rewritten ? `<div class="orig" title="${esc(r.original)}">${esc(r.original)}</div>` : "";
  const msg = r && !r.ok ? `<div class="msg">${esc(r.message.split("（詳細")[0])}</div>` : "";
  let cta = "";
  if (r && r.rewritten && !r.approved && state.done) cta = `<button class="review-cta" data-review="${ln.index}">✎ AI が書き換えました · 確認する</button>`;
  else if (r && r.rewritten && r.approved) cta = `<button class="review-cta done" data-review="${ln.index}">✓ 確認済み</button>`;
  else if (r && !r.ok && state.done) cta = `<button class="review-cta" data-review="${ln.index}">✎ 原稿を短くする</button>`;
  const canEdit = r && state.done && !state.running;
  const playing = state.playing === ln.index;
  return `<div class="row ${st === "running" ? "running" : ""}" data-index="${ln.index}" data-st="${st}">
    <span class="idx">${String(ln.index).padStart(2, "0")}</span>
    <span class="start">${clock(state.starts[i])}</span>
    <span class="row-actions">
      <button class="play ${playing ? "playing" : ""}" data-play="${ln.index}" ${r ? "" : "disabled"} title="試聴">
        ${playing ? '<svg viewBox="0 0 24 24"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>'
                  : '<svg viewBox="0 0 24 24"><path d="M8 5.5v13l10.5-6.5L8 5.5Z"/></svg>'}
      </button>
      <button class="edit" data-review="${ln.index}" ${canEdit ? "" : "disabled"} title="原稿を確認・書き直す">
        <svg viewBox="0 0 24 24"><path d="M4 20h4L19 9l-4-4L4 16v4Z"/><path d="m13.5 6.5 4 4"/></svg>
      </button>
    </span>
    <div class="text">${spk}<div class="final" title="${esc(text)}">${esc(text)}</div>${orig}${msg}${cta}</div>
    <div class="bar-cell" data-tip="${ln.index}">${barHtml(ln, r)}</div>
    <span><span class="chip ${st === "running" ? "running" : ""}"><i></i>${info.glyph ? info.glyph + " " : ""}${info.label}</span></span>
  </div>`;
}

function renderRows() {
  const f = state.file;
  $("#rows").innerHTML = f.lines.map((ln, i) => rowHtml(ln, i)).join("");
  $$(".ov-clip").forEach((clip) => {
    const ln = f.lines.find((l) => l.index === Number(clip.dataset.index));
    const st = rowStatus(ln);
    clip.dataset.st = st;
    clip.classList.toggle("running", st === "running");
  });
}

function updateRow(index) {
  const i = state.file.lines.findIndex((l) => l.index === index);
  if (i < 0) return;
  const old = document.querySelector(`.row[data-index="${index}"]`);
  const tmp = document.createElement("div");
  tmp.innerHTML = rowHtml(state.file.lines[i], i);
  old?.replaceWith(tmp.firstElementChild);
  const clip = document.querySelector(`.ov-clip[data-index="${index}"]`);
  if (clip) {
    const st = rowStatus(state.file.lines[i]);
    clip.dataset.st = st;
    clip.classList.toggle("running", st === "running");
  }
}

function renderStats() {
  const vals = Object.values(state.results);
  const el = $("#stats");
  if (!vals.length) { el.innerHTML = ""; return; }
  const n = (keys) => vals.filter((r) => keys.includes(r.statusKey)).length;
  const items = [
    ["natural", "✓ そのまま", n(["natural", "pause"])],
    ["speed", "» 話速", n(["speed"])],
    ["rewrite", "✎ 要約", n(["rewrite"])],
    ["failed", "⚠ 収まらず", n(["failed"])],
    ["rewrite", "要確認", vals.filter((r) => r.rewritten && !r.approved).length],
  ].filter((x) => x[2] > 0);
  el.innerHTML = items.map(([k, label, c]) => `<span class="stat" data-st="${k}">${label} <b>${c}</b></span>`).join("");
}

function highlight(index, on) {
  document.querySelector(`.row[data-index="${index}"]`)?.classList.toggle("hl", on);
  document.querySelector(`.ov-clip[data-index="${index}"]`)?.classList.toggle("hl", on);
}

/* ---------- ツールチップ ---------- */
function showTip(e, index) {
  const ln = state.file.lines.find((l) => l.index === index);
  const i = state.file.lines.indexOf(ln);
  const r = state.results[index];
  const st = STATUS[rowStatus(ln)];
  const rows = [
    ["開始", clock(state.starts[i])],
    ["目標", sec(ln.target)],
  ];
  if (r) {
    rows.push(["話している部分", sec(r.speech)], ["話速", `×${r.speed.toFixed(2)}`]);
    if (r.pause < 0.999) rows.push(["句読点の間", `×${r.pause.toFixed(2)}`]);
    const t = r.timing;
    if (t) {
      rows.push(["処理時間", sec(t.total)]);
      if (t.aiCalls) rows.push(["　うち AI", `${sec(t.ai)} ×${t.aiCalls}回`]);
      if (t.wait) rows.push(["　うち 待ち", sec(t.wait)]);
      rows.push(["　うち 合成", `${sec(t.synth)} ×${t.synthCalls}回`]);
      if (t.aiErrors) rows.push(["AI のエラー", esc(t.aiErrors)]);
    }
  }
  const tip = $("#tooltip");
  tip.innerHTML = `<div class="tt-title">#${index} · ${st.glyph ? st.glyph + " " : ""}${st.label}</div>` +
    rows.map(([k, v]) => `<div class="tt-row"><span>${k}</span><b>${v}</b></div>`).join("");
  tip.classList.remove("hidden");
  moveTip(e);
}
function moveTip(e) {
  const tip = $("#tooltip");
  const x = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
  const y = Math.min(e.clientY + 16, window.innerHeight - tip.offsetHeight - 8);
  tip.style.left = `${x}px`;
  tip.style.top = `${y}px`;
}
function hideTip() { $("#tooltip").classList.add("hidden"); }

/* ---------- 実行 ---------- */
async function run() {
  if (!state.file || state.running) return;
  const params = {
    input: state.file.path,
    output: state.output,
    default_speaker: Number($("#speaker").value || state.settings.default_speaker),
    fit_mode: state.mode,
    max_speed: Number($("#maxSpeed").value),
    allow_rewrite: $("#allowRewrite").checked,
  };
  state.results = {};
  state.done = false;
  renderRows();
  renderStats();
  const res = await api().start(params);
  if (!res.ok) { toast(res.error, true); return; }
  setRunning(true);
  $("#progressText").textContent = "準備中…";
  $("#progressFill").style.width = "0%";
}

function setRunning(on) {
  state.running = on;
  $("#runBtn").classList.toggle("hidden", on);
  $("#cancelBtn").classList.toggle("hidden", !on);
  $("#progressWrap").classList.toggle("hidden", !on && !state.done);
  $("#pickFile").disabled = on;
  $("#pickOutput").disabled = on;
}

window.tf = {
  onEvent(ev) {
    switch (ev.type) {
      case "log":
        log(ev.message);
        break;
      case "lineStart":
        state.runningIndex = ev.index;
        updateRow(ev.index);
        $("#progressText").textContent = `${ev.position} / ${ev.total} 行目を処理中…`;
        document.querySelector(`.row[data-index="${ev.index}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
        break;
      case "lineDone":
        state.results[ev.result.index] = ev.result;
        if (state.runningIndex === ev.result.index) state.runningIndex = null;
        updateRow(ev.result.index);
        renderStats();
        $("#progressFill").style.width = `${(ev.position / ev.total) * 100}%`;
        break;
      case "done": {
        const s = ev.summary;
        state.runningIndex = null;
        state.done = true;
        setRunning(false);
        $("#progressFill").style.width = "100%";
        $("#progressText").textContent = s.cancelled ? `中止しました（${s.count} 行まで書き出し済み）` : `完了 · ${s.count} 行`;
        $("#runBtn span").textContent = "もう一度実行";
        $("#playAll").classList.remove("hidden");
        $("#openOutput").classList.remove("hidden");
        renderRows();
        renderStats();
        const toReview = Object.values(state.results).filter((r) => r.rewritten).length;
        let msg = s.failed ? `完了しました。${s.failed} 行は目標に収まりませんでした（⚠ の行の原稿を短くしてください）。` : "完了しました。すべての行が目標の尺に収まりました。";
        if (toReview) msg += ` AI が書き換えた ${toReview} 行は「確認する」から内容を確かめてください。`;
        if (s.skipped) msg += ` 無料版のため ${s.skipped} 行は処理していません。`;
        toast(msg, Boolean(s.failed), 7000);
        break;
      }
      case "error":
        state.runningIndex = null;
        setRunning(false);
        log(`【エラー】${ev.message}`);
        toast(ev.message, true, 8000);
        if (state.file) renderRows();
        break;
      case "file":
        setFile(ev.preview);
        break;
    }
  },
};

/* ---------- 試聴 ---------- */
async function togglePlay(index) {
  const player = $("#player");
  const prev = state.playing;
  if (prev !== null) {
    player.pause();
    state.playing = null;
    if (prev !== "all") updateRow(prev);
    if (prev === index) return;
  }
  const url = index === "all" ? await api().combined_audio() : await api().audio(index);
  if (!url) { toast("音声が見つかりませんでした。"); return; }
  player.src = url;
  state.playing = index;
  if (index !== "all") updateRow(index);
  player.play();
}
$("#player").addEventListener("ended", () => {
  const prev = state.playing;
  state.playing = null;
  if (prev !== null && prev !== "all") updateRow(prev);
});

/* ---------- 設定 ---------- */
function openSettings() {
  const s = state.settings;
  $("#apiKey").value = "";
  $("#apiKey").placeholder = s.hasApiKey ? `設定済み（${s.apiKeyHint}）· 変更するときだけ入力` : "未設定";
  $("#model").value = s.gemini_model;
  $("#fallbackModel").value = s.gemini_fallback_model;
  $("#thinking").value = s.gemini_thinking;
  $("#rewriteAttempts").value = s.max_rewrite_attempts;
  $("#minPause").value = s.min_pause_scale;
  $("#engineUrl").value = s.engine_url;
  $("#creditPrefix").value = s.credit_prefix;
  $("#diagResult").innerHTML = '<p class="hint">VOICEVOX と APIキーが使える状態か確認します。</p>';
  $("#settingsModal").classList.remove("hidden");
}
function closeSettings() { $("#settingsModal").classList.add("hidden"); }

async function saveSettings() {
  const data = {
    gemini_api_key: $("#apiKey").value,
    gemini_model: $("#model").value.trim(),
    gemini_fallback_model: $("#fallbackModel").value.trim(),
    gemini_thinking: $("#thinking").value,
    max_rewrite_attempts: Number($("#rewriteAttempts").value),
    min_pause_scale: Number($("#minPause").value),
    engine_url: $("#engineUrl").value.trim(),
    credit_prefix: $("#creditPrefix").value.trim(),
  };
  const engineChanged = data.engine_url !== state.settings.engine_url;
  const res = await api().save_settings(data);
  if (!res.ok) { toast(res.error, true); return; }
  state.settings = res.settings;
  updateRewriteHint();
  closeSettings();
  toast("設定を保存しました。");
  if (engineChanged) refreshEngine();
}

async function runDiag() {
  const box = $("#diagResult");
  box.innerHTML = '<p class="hint">確認中…（Gemini には短い文を1回だけ送ります）</p>';
  const items = await api().diagnose();
  box.innerHTML = items.map((i) =>
    `<div class="diag-item ${i.ok ? "ok" : "ng"}"><span class="mark">${i.ok ? "✓" : "✕"}</span><span>${esc(i.message)}</span></div>`).join("");
}

/* ---------- 原稿の確認・選び直し ---------- */

// 文字単位の差分（LCS）。a → b で消えた文字は del、増えた文字は ins
function diffChars(a, b) {
  const A = [...a], B = [...b];
  const n = A.length, m = B.length;
  const dp = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const ops = [];
  const push = (type, ch) => {
    const last = ops[ops.length - 1];
    if (last && last.type === type) last.text += ch; else ops.push({ type, text: ch });
  };
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (A[i] === B[j]) { push("same", A[i]); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) push("del", A[i++]);
    else push("ins", B[j++]);
  }
  while (i < n) push("del", A[i++]);
  while (j < m) push("ins", B[j++]);
  return ops;
}
const diffHtml = (ops, show) => ops.map((o) => {
  if (o.type === "same") return esc(o.text);
  if (o.type !== show) return "";
  return show === "del" ? `<del>${esc(o.text)}</del>` : `<ins>${esc(o.text)}</ins>`;
}).join("");

// 消えると困りそうな語（数字・カタカナ語・3文字以上の漢字語）
function keyTerms(text) {
  const found = text.match(/\d+(?:[.,]\d+)?|[ァ-ヴー・]{3,}|[一-龠々]{3,}/g) || [];
  return [...new Set(found)];
}
const lostTerms = (orig, text) => keyTerms(orig).filter((t) => !text.includes(t));

const rv = { index: null, result: null, line: null, selected: null, estTimer: null, estCache: {} };

function estLabel(e) {
  if (!e) return '<span class="spin"></span>';
  if (!e.ok) return `<span class="est-ng">${esc(e.error)}</span>`;
  const base = `推定 ${e.natural.toFixed(2)}s（${e.chars}文字）`;
  if (!e.fits) return `<span class="est-ng">${base} · 話速 ×${state.settings.max_speed} でも収まりません</span>`;
  const how = e.speed > 1.0005 ? `話速 ×${e.speed.toFixed(2)} で収まる` : (e.pause < 0.999 ? "間を詰めれば収まる" : "そのまま収まる");
  return `<span class="est-ok">${base} · ${how}</span>`;
}

async function estimateCached(text) {
  if (!rv.estCache[text]) rv.estCache[text] = api().estimate(rv.index, text);
  return rv.estCache[text];
}

function openReview(index) {
  const r = state.results[index];
  const line = state.file.lines.find((l) => l.index === index);
  if (!r || !line) return;
  rv.index = index; rv.result = r; rv.line = line; rv.estCache = {};
  const i = state.file.lines.indexOf(line);
  $("#rvTitle").textContent = r.ok ? `#${index} の原稿を確認` : `#${index} の原稿を短くする`;
  $("#rvMeta").textContent = `開始 ${clock(state.starts[i])} · 目標 ${sec(line.target)} · 今の状態: ${STATUS[r.statusKey].label}`;
  $("#rvText").value = r.text;
  rv.selected = r.text;
  renderReview();
  $("#reviewModal").classList.remove("hidden");
  setTimeout(() => $("#rvText").focus(), 50);
}
function closeReview() { $("#reviewModal").classList.add("hidden"); rv.index = null; }

function renderReview() {
  const r = rv.result;
  const text = $("#rvText").value.trim();
  $("#rvOriginal").innerHTML = diffHtml(diffChars(r.original, text), "del");
  const lost = lostTerms(r.original, text);
  const lostEl = $("#rvLost");
  lostEl.classList.toggle("hidden", !lost.length);
  lostEl.innerHTML = lost.length ? `⚠ 選んでいる文では、次の語が消えています（固有名詞・数字の可能性）: ${lost.map((t) => `<b>${esc(t)}</b>`).join("、")}` : "";

  const list = r.candidates.length ? r.candidates : [];
  const box = $("#rvCands");
  if (!list.length) {
    box.innerHTML = `<div class="cands-empty">まだ AI の案はありません。下のボタンで案を出してもらうか、「この文で作る」欄を直接書き直してください。</div>`;
  } else {
    box.innerHTML = list.map((c, k) => {
      const lostN = lostTerms(r.original, c).length;
      return `<button class="cand ${c === rv.selected ? "on" : ""}" data-cand="${k}">
        <span class="cand-label">案${k + 1}${c === r.text ? '<span class="now">いまの音声</span>' : ""}</span>
        <span><span class="cand-text">${diffHtml(diffChars(r.original, c), "ins")}</span>
        <span class="cand-est" data-est="${k}">${estLabel(null)}${lostN ? `<span class="cand-lost">消えた語 ${lostN}</span>` : ""}</span></span>
      </button>`;
    }).join("");
    list.forEach(async (c, k) => {
      const e = await estimateCached(c);
      const el = box.querySelector(`[data-est="${k}"]`);
      if (el) el.innerHTML = estLabel(e) + (lostTerms(r.original, c).length ? `<span class="cand-lost">消えた語 ${lostTerms(r.original, c).length}</span>` : "");
    });
  }
  updateReviewFooter();
  scheduleEstimate();
}

function updateReviewFooter() {
  const text = $("#rvText").value.trim();
  const same = text === rv.result.text;
  const btn = $("#rvApply");
  btn.textContent = same ? (rv.result.rewritten && !rv.result.approved ? "この内容で確定" : "閉じる") : "この文で作り直す";
  btn.dataset.action = same ? "approve" : "apply";
  btn.disabled = !text;
  $("#rvUseOriginal").disabled = text === rv.result.original;
}

function scheduleEstimate() {
  clearTimeout(rv.estTimer);
  const text = $("#rvText").value.trim();
  const el = $("#rvEst");
  if (!text) { el.innerHTML = ""; return; }
  el.innerHTML = estLabel(null);
  rv.estTimer = setTimeout(async () => {
    const e = await estimateCached(text);
    if ($("#rvText").value.trim() === text) el.innerHTML = estLabel(e);
  }, 350);
}

async function moreCandidates() {
  const btn = $("#rvMore");
  btn.disabled = true;
  btn.querySelector("span").textContent = "AI が考えています…";
  const res = await api().suggest(rv.index);
  btn.disabled = false;
  btn.querySelector("span").textContent = "AI に別の案を出してもらう";
  if (!res.ok) { toast(res.error, true, 6000); return; }
  rv.result.candidates = res.candidates;
  state.results[rv.index].candidates = res.candidates;
  renderReview();
}

async function applyReview() {
  const btn = $("#rvApply");
  const text = $("#rvText").value.trim();
  if (btn.dataset.action === "approve") {
    if (rv.result.rewritten && !rv.result.approved) {
      await api().approve(rv.index);
      state.results[rv.index].approved = true;
      updateRow(rv.index);
      renderStats();
    }
    closeReview();
    return;
  }
  btn.disabled = true;
  btn.textContent = "作り直しています…";
  const index = rv.index;
  const res = await api().apply(index, text);
  if (!res.ok) { toast(res.error, true, 6000); updateReviewFooter(); return; }
  state.results[index] = res.result;
  updateRow(index);
  renderStats();
  closeReview();
  const r = res.result;
  toast(r.ok ? `#${index} を作り直しました（${STATUS[r.statusKey].label}）。出力ファイルも更新済みです。`
             : `#${index} はこの文でも ${sec(r.speech - r.target)} はみ出します。もう少し短くしてください。`, !r.ok, 6000);
}

function bindReview() {
  $$("[data-close-review]").forEach((b) => b.addEventListener("click", closeReview));
  $("#reviewModal").addEventListener("click", (e) => { if (e.target.id === "reviewModal") closeReview(); });
  $("#rvCands").addEventListener("click", (e) => {
    const c = e.target.closest("[data-cand]");
    if (!c) return;
    rv.selected = rv.result.candidates[Number(c.dataset.cand)];
    $("#rvText").value = rv.selected;
    renderReview();
  });
  $("#rvText").addEventListener("input", () => {
    const text = $("#rvText").value.trim();
    rv.selected = rv.result.candidates.includes(text) ? text : null;
    $$("#rvCands .cand").forEach((el) => el.classList.toggle("on", rv.result.candidates[Number(el.dataset.cand)] === rv.selected));
    $("#rvOriginal").innerHTML = diffHtml(diffChars(rv.result.original, text), "del");
    const lost = lostTerms(rv.result.original, text);
    $("#rvLost").classList.toggle("hidden", !lost.length);
    $("#rvLost").innerHTML = lost.length ? `⚠ 選んでいる文では、次の語が消えています（固有名詞・数字の可能性）: ${lost.map((t) => `<b>${esc(t)}</b>`).join("、")}` : "";
    updateReviewFooter();
    scheduleEstimate();
  });
  $("#rvMore").addEventListener("click", moreCandidates);
  $("#rvApply").addEventListener("click", applyReview);
  $("#rvUseOriginal").addEventListener("click", () => {
    $("#rvText").value = rv.result.original;
    rv.selected = null;
    renderReview();
  });
}

/* ---------- イベント登録 ---------- */
function bindUi() {
  const pick = async () => setFile(await api().choose_input());
  $("#pickFile").addEventListener("click", pick);
  $("#pickFile2").addEventListener("click", pick);
  $("#pickOutput").addEventListener("click", async () => {
    const p = await api().choose_output();
    if (p) { state.output = p; $("#outputPath").textContent = p; }
  });
  $("#engineStatus").addEventListener("click", refreshEngine);
  $("#openSettings").addEventListener("click", openSettings);
  $("#runBtn").addEventListener("click", run);
  $("#cancelBtn").addEventListener("click", () => { api().cancel(); $("#progressText").textContent = "今の行が終わったら止めます…"; });
  $("#openOutput").addEventListener("click", () => api().open_output());
  $("#playAll").addEventListener("click", () => togglePlay("all"));

  $$("#mode button").forEach((b) => b.addEventListener("click", () => {
    state.mode = b.dataset.value;
    $$("#mode button").forEach((x) => x.classList.toggle("on", x === b));
  }));
  $("#maxSpeed").addEventListener("input", (e) => { $("#speedValue").textContent = `${Number(e.target.value).toFixed(2)}×`; });
  $("#allowRewrite").addEventListener("change", updateRewriteHint);

  $("#rows").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-play]");
    if (btn) togglePlay(Number(btn.dataset.play));
    const rv = e.target.closest("[data-review]");
    if (rv && !rv.disabled) openReview(Number(rv.dataset.review));
  });
  $("#trySample").addEventListener("click", async () => setFile(await api().sample()));
  bindReview();
  $("#rows").addEventListener("mouseover", (e) => {
    const cell = e.target.closest("[data-tip]");
    if (cell && !cell.contains(e.relatedTarget)) showTip(e, Number(cell.dataset.tip));
  });
  $("#rows").addEventListener("mousemove", (e) => { if (e.target.closest("[data-tip]")) moveTip(e); });
  $("#rows").addEventListener("mouseout", (e) => {
    const cell = e.target.closest("[data-tip]");
    if (cell && !cell.contains(e.relatedTarget)) hideTip();
  });

  $("#logToggle").addEventListener("click", (e) => {
    if (e.target.id === "logCopy") {
      navigator.clipboard.writeText(state.logs.join("\n")).then(() => toast("ログをコピーしました。"));
      return;
    }
    $("#log").classList.toggle("open");
  });

  $$("[data-close]").forEach((b) => b.addEventListener("click", closeSettings));
  $("#settingsModal").addEventListener("click", (e) => { if (e.target.id === "settingsModal") closeSettings(); });
  $("#saveSettings").addEventListener("click", saveSettings);
  $("#runDiag").addEventListener("click", runDiag);
  $$(".presets button").forEach((b) => b.addEventListener("click", () => {
    $("#engineUrl").value = b.dataset.engine;
    $("#creditPrefix").value = b.dataset.credit;
  }));
  $$("[data-url]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); api().open_url(a.dataset.url); }));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeSettings(); closeReview(); } });

  // ドラッグ中の見た目（ファイルの受け取り自体は Python 側で行う）
  let depth = 0;
  document.addEventListener("dragenter", (e) => { e.preventDefault(); depth++; document.body.classList.add("dragging"); });
  document.addEventListener("dragleave", () => { if (--depth <= 0) { depth = 0; document.body.classList.remove("dragging"); } });
  document.addEventListener("dragover", (e) => e.preventDefault());
  document.addEventListener("drop", (e) => { e.preventDefault(); depth = 0; document.body.classList.remove("dragging"); });
}

bindUi();
if (window.pywebview && window.pywebview.api) boot();
else window.addEventListener("pywebviewready", boot);
