/* Speech Studio — sunucu şemasına sadık: meta / job / result{words,turns,..}
   / speakers / summary / questions. Zaman çizelgesi dalga formu + konuşmacı
   şeritleri + canlı oynatma imleci ile çizilir. */
"use strict";

const SPEAKER_COLORS = [
  "#2f6fed", "#d97b29", "#1f9d77", "#c0467c", "#7a5cd6", "#0e91b3", "#c9a227", "#b94b32",
];

const state = {
  key: "",
  current: null,
  cache: {},
  peaks: [],
  raf: 0,
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "")
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");


async function api(path, options = {}) {
  const res = await fetch(path, { ...options, headers: (options.headers || {}) });
  if (res.status === 401) {
    $("statusline").textContent = "yetki reddedildi";
    throw new Error("unauthorized");
  }
  if (!res.ok) {
    let message = "istek başarısız (" + res.status + ")";
    try { message = (await res.json()).detail || message; } catch (_) {}
    throw new Error(message);
  }
  return res;
}
const apiJSON = (path, options = {}) => api(path, options).then((r) => r.json());

function mmss(sec) {
  const s = Math.max(0, Math.floor(sec));
  return String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
}
const speakerColor = (no) => SPEAKER_COLORS[(Math.abs(Number(no)) - 1 + 32) % SPEAKER_COLORS.length];
const speakerName = (no, map) => (map && map[String(no)]) ? map[String(no)] : "Konuşmacı " + no;

/* ------------------------------------------------------------------ yükleme */

function upload(file) {
  const bar = $("upload-progress");
  const fill = $("upload-progress-bar");
  const text = $("upload-progress-text");
  const errBox = document.querySelector(".upload-error");
  if (errBox) errBox.remove();

  const form = new FormData();
  form.append("file", file);
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/recordings");
  const headers = { "X-Requested-With": "speech-studio" };
  for (const [k, v] of Object.entries(headers)) xhr.setRequestHeader(k, v);

  bar.hidden = false;
  fill.style.width = "0%";
  text.textContent = "Yükleniyor · " + file.name;

  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    const pct = Math.round((e.loaded / e.total) * 100);
    fill.style.width = pct + "%";
    text.textContent = "Yükleniyor · %" + pct + " · " + file.name;
  };
  xhr.onload = () => {
    bar.hidden = true;
    if (xhr.status !== 201 && xhr.status !== 200) {
      showUploadError("Yükleme başarısız (" + xhr.status + ")");
      return;
    }
    try {
      const created = JSON.parse(xhr.responseText);
      $("statusline").textContent = "Yüklendi — işlem sıraya alındı.";
      open(created.id);
    } catch (e) {
      showUploadError("Sunucu yanıtı okunamadı.");
    }
  };
  xhr.onerror = () => { bar.hidden = true; showUploadError("Bağlantı kesildi — tekrar deneyin."); };
  xhr.send(form);
}

function showUploadError(message) {
  const drop = $("drop");
  const old = document.querySelector(".upload-error");
  if (old) old.remove();
  const p = document.createElement("div");
  p.className = "upload-error";
  p.textContent = message;
  drop.querySelector(".drop-body").appendChild(p);
  $("statusline").textContent = message;
}

const drop = $("drop");
["dragover", "dragenter"].forEach((ev) => drop.addEventListener(ev, (e) => {
  e.preventDefault(); drop.classList.add("hot");
}));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => {
  e.preventDefault(); drop.classList.remove("hot");
}));
drop.addEventListener("drop", (e) => {
  const file = e.dataTransfer.files && e.dataTransfer.files[0];
  if (file) upload(file);
});
$("pick").addEventListener("click", () => $("file").click());
$("file").addEventListener("change", (e) => {
  const file = e.target.files && e.target.files[0];
  e.target.value = "";
  if (file) upload(file);
});

/* ----------------------------------------------------------- kayıtlar listesi */

async function refreshList() {
  let data;
  try {
    data = await apiJSON("/api/status");
  } catch (err) {
    $("statusline").textContent = err.message;
    return;
  }
  const llm = data.ollama && data.ollama.reachable ? "· LLM " + data.ollama.model : "· LLM kapalı";
  $("statusline").textContent =
    "NeMo-Speech · GPU" + " " + llm + (data.queue ? " · sırada " + data.queue : "");

  const list = $("list");
  list.innerHTML = "";
  $("list-empty").style.display = data.recordings.length ? "none" : "block";
  for (const rec of data.recordings) {
    const li = document.createElement("li");
    li.dataset.id = rec.id;
    if (rec.id === state.current) li.className = "active";
    li.innerHTML =
      '<div class="t">' + esc(rec.title) + "</div>" +
      '<div class="m"><span class="state ' + esc(rec.state) + '">' + esc(rec.state) + "</span>" +
      "<span>" + (rec.duration ? mmss(rec.duration) : "—") + "</span>" +
      (rec.speaker_count ? "<span>" + rec.speaker_count + " kişi</span>" : "") + "</div>";
    li.addEventListener("click", () => open(rec.id));
    list.appendChild(li);
  }
}

/* ------------------------------------------------------ dalga + şerit + imleç */

function drawTimeline() {
  const item = state.cache[state.current];
  const result = (item && item.result) || {};
  const turns = result.turns || [];
  const duration = result.duration || 0;
  const peaks = state.peaks;

  const canvas = $("wave");
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 800;
  const cssH = 200;
  if (!cssW) return;
  canvas.width = cssW * dpr;
  canvas.height = cssH * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);

  const styles = getComputedStyle(document.documentElement);
  const line = styles.getPropertyValue("--line").trim();
  const ink3 = styles.getPropertyValue("--ink-3").trim();
  const ink2 = styles.getPropertyValue("--ink-2").trim();

  const rulerH = 20;
  const waveH = 84;
  const lanesTop = rulerH + waveH + 12;
  const laneH = Math.min(22, Math.max(14, (cssH - lanesTop - 8) / Math.max(1, uniqueSpeakers(turns).length)));

  ctx.fillStyle = ink3;
  ctx.font = "10px ui-monospace, Menlo, monospace";
  ctx.strokeStyle = line;
  ctx.lineWidth = 1;
  if (duration > 0) {
    const step = niceTick(duration);
    for (let t = 0; t <= duration + 0.001; t += step) {
      const x = Math.round((t / duration) * cssW) + 0.5;
      ctx.beginPath(); ctx.moveTo(x, rulerH - 6); ctx.lineTo(x, cssH); ctx.stroke();
      if (t > 0) ctx.fillText(mmss(t), x + 4, rulerH - 8);
    }
  }

  if (peaks.length && duration > 0) {
    const mid = rulerH + waveH / 2;
    const barW = cssW / peaks.length;
    ctx.fillStyle = styles.getPropertyValue("--surface-2").trim();
    ctx.globalAlpha = 0.65;
    peaks.forEach((p, i) => {
      const h = Math.max(1.5, Math.min(1, p) * waveH * 0.92);
      ctx.fillRect(i * barW, mid - h / 2, Math.max(0.8, barW * 0.6), h);
    });
    ctx.globalAlpha = 1;
  }

  const speakers = uniqueSpeakers(turns);
  const laneNames = item ? item.speakers || {} : {};
  speakers.forEach((no, idx) => {
    const y = lanesTop + idx * laneH;
    ctx.fillStyle = speakerColor(no);
    ctx.fillRect(0, y + 2, 3, laneH - 5);
    ctx.fillStyle = ink2;
    ctx.font = "10px ui-sans-serif, sans-serif";
    if (cssW > 520) {
      const label = speakerName(no, laneNames);
      ctx.fillText(label.slice(0, 14), 9, y + laneH / 2 + 3);
    }
    for (const turn of turns) {
      if (turn.speaker !== no || !duration) continue;
      const x = (turn.start / duration) * cssW + 44;
      const w = Math.max(2, ((turn.end - turn.start) / duration) * cssW - 2);
      ctx.fillStyle = speakerColor(no);
      roundRect(ctx, x, y + 3, w, laneH - 7, 3);
      ctx.fill();
    }
  });

  const audio = $("audio");
  if (duration > 0 && audio.duration) {
    const t = Math.min(audio.currentTime, duration);
    const x = ((t / duration) * cssW) + 0.5;
    ctx.strokeStyle = styles.getPropertyValue("--accent").trim();
    ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, cssH); ctx.stroke();
    ctx.fillStyle = styles.getPropertyValue("--accent").trim();
    ctx.fillRect(x - 3, 0, 6, 3);
  }
}

function niceTick(duration) {
  const raw = duration / 10;
  const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300];
  return steps.find((s) => s >= raw) || 600;
}

function uniqueSpeakers(turns) {
  return [...new Set(turns.map((t) => t.speaker))].sort((a, b) => a - b);
}

function roundRect(ctx, x, y, w, h, r) {
  const rr = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.arcTo(x + w, y, x + w, y + h, rr);
  ctx.arcTo(x + w, y + h, x, y + h, rr);
  ctx.arcTo(x, y + h, x, y, rr);
  ctx.arcTo(x, y, x + w, y, rr);
  ctx.closePath();
}

function animate() {
  drawTimeline();
  state.raf = requestAnimationFrame(animate);
}

function seek(seconds) {
  const audio = $("audio");
  audio.currentTime = Math.min(Math.max(0, seconds), audio.duration || seconds);
  audio.play().catch(() => {});
}

$("wave").addEventListener("click", (e) => {
  const rect = e.currentTarget.getBoundingClientRect();
  const duration = ((state.cache[state.current] || {}).result || {}).duration || 0;
  if (!duration) return;
  seek(((e.clientX - rect.left) / rect.width) * duration);
});

/* --------------------------------------------------------------- döküm */

function renderTurns(item) {
  const turns = (item.result && item.result.turns) || [];
  const box = $("turns");
  if (!turns.length) {
    box.innerHTML = item.job && item.job.state === "failed"
      ? '<div class="empty">İşlem başarısız: ' + esc(item.job.detail || "") + "</div>"
      : '<div class="empty">Kayıt işlendiğinde konuşma turları burada belirir.</div>';
    return;
  }
  box.innerHTML = "";
  for (const turn of turns) {
    const row = document.createElement("div");
    row.className = "turn";
    row.innerHTML =
      '<div class="ts">' + mmss(turn.start) + "</div>" +
      '<div class="who" style="color:' + speakerColor(turn.speaker) + '">' +
      esc(speakerName(turn.speaker, item.speakers)) + "</div>" +
      '<div class="txt">' + esc(turn.text) + "</div>";
    row.addEventListener("click", () => seek(turn.start));
    box.appendChild(row);
  }
}

/* ------------------------------------------------------------ özet & sorular */

function renderSummary(text) {
  const el = $("summary");
  if (!text) {
    el.innerHTML = '<div class="empty">Kayıt işlendiğinde özet üretilebilir.</div>';
    return;
  }
  el.innerHTML = text.split(/\n\n+/).map((blk) => {
    const heading = blk.match(/^##\s*(.+)$/);
    if (heading) return "<h3>" + esc(heading[1]) + "</h3>";
    if (/^[-*]\s/.test(blk)) {
      return "<ul>" + blk.split("\n").map((l) => "<li>" + esc(l.replace(/^[-*]\s*/, "")) + "</li>").join("") + "</ul>";
    }
    return "<p>" + esc(blk).replace(/\n/g, "<br>") + "</p>";
  }).join("");
}

function renderQa(item) {
  const thread = $("qa-thread");
  thread.innerHTML = "";
  for (const qa of item.questions || []) {
    const q = document.createElement("div");
    q.className = "qa-q";
    q.textContent = qa.question;
    const a = document.createElement("div");
    a.className = "qa-a";
    a.textContent = qa.answer;
    thread.append(q, a);
  }
  thread.scrollTop = thread.scrollHeight;
}

async function summarize() {
  if (!state.current) return;
  $("summarize").disabled = true;
  $("summary").innerHTML =
    '<div class="skeleton" style="width:92%"></div>' +
    '<div class="skeleton" style="width:78%"></div>' +
    '<div class="skeleton" style="width:85%"></div>';
  try {
    const res = await apiJSON("/api/recordings/" + state.current + "/summary", { method: "POST" });
    renderSummary(res.summary);
  } catch (err) {
    $("summary").innerHTML = '<div class="empty">Özet alınamadı: ' + esc(err.message) + "</div>";
  } finally {
    $("summarize").disabled = false;
  }
}

async function ask(question) {
  if (!state.current || !question) return;
  $("ask").disabled = true;
  try {
    const res = await apiJSON("/api/recordings/" + state.current + "/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const item = state.cache[state.current];
    if (item) {
      item.questions = (item.questions || []).concat([{ question: res.question, answer: res.answer }]);
      renderQa(item);
    }
  } catch (err) {
    $("statusline").textContent = "Soru sorulamadı: " + err.message;
  } finally {
    $("ask").disabled = false;
  }
}

/* ------------------------------------------------------------ kayıt görünümü */

function renderAll(item) {
  const meta = item.meta || {};
  const result = item.result || {};
  $("rec-title").textContent = meta.title || state.current;
  $("rec-meta").textContent = result.duration
    ? mmss(result.duration) + " · " + (result.speaker_count || "?") + " konuşmacı"
    : (item.job && item.job.state ? "durum: " + item.job.state : "");
  $("export-link").href = "/api/export/" + state.current + ".md";
  const speakers = uniqueSpeakers(result.turns || []);
  renderLegend(speakers, item.speakers);
  renderTurns(item);
  renderSummary(item.summary || "");
  renderQa(item);
  drawTimeline();
}

function renderLegend(speakers, names) {
  const legend = $("legend");
  legend.innerHTML = "";
  for (const no of speakers) {
    const chip = document.createElement("span");
    chip.className = "chip";
    const dot = document.createElement("span");
    dot.className = "dot";
    dot.style.background = speakerColor(no);
    const input = document.createElement("input");
    input.value = speakerName(no, names);
    input.maxLength = 24;
    input.title = "isimlendirmek için yazın";
    input.addEventListener("change", () => rename(no, input.value));
    chip.append(dot, input);
    legend.appendChild(chip);
  }
}

async function rename(no, name) {
  const item = state.cache[state.current];
  if (!item) return;
  const names = { ...(item.speakers || {}), [String(no)]: name };
  try {
    const res = await apiJSON("/api/recordings/" + state.current + "/speakers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ names }),
    });
    item.speakers = res.speakers;
    renderAll(item);
  } catch (err) {
    $("statusline").textContent = err.message;
  }
}

async function open(id) {
  state.current = id;
  $("audio").src = "/api/recordings/" + id + "/audio";
  try {
    const item = await apiJSON("/api/recordings/" + id);
    state.cache[id] = item;
    renderAll(item);
    try {
      const peaks = await apiJSON("/api/recordings/" + id + "/peaks");
      state.peaks = peaks.peaks || [];
      renderAll(state.cache[id]);
    } catch (_) { state.peaks = []; }
  } catch (err) {
    $("turns").innerHTML = '<div class="empty">Yüklenemedi: ' + esc(err.message) + "</div>";
  }
  refreshList();
}

/* ------------------------------------------------------------------ olaylar */

$("summarize").addEventListener("click", summarize);
$("ask-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("question").value.trim();
  if (q) { $("question").value = ""; ask(q); }
});

$("delete").addEventListener("click", async () => {
  if (!state.current || !confirm("Kayıt silinsin mi? Bu işlem geri alınamaz.")) return;
  try {
    await api("/api/recordings/" + state.current, { method: "DELETE" });
    state.current = null;
    state.peaks = [];
    $("audio").removeAttribute("src");
    $("rec-title").textContent = "Bir kayıt seçin";
    $("turns").innerHTML = '<div class="empty">Kayıt işlendiğinde konuşma turları burada belirir.</div>';
    renderSummary("");
    renderQa({ questions: [] });
    refreshList();
  } catch (err) {
    $("statusline").textContent = err.message;
  }
});


window.addEventListener("resize", drawTimeline);
$("audio").addEventListener("loadedmetadata", drawTimeline);

async function boot() {
  const params = new URLSearchParams(location.search);
  const wanted = params.get("rec");
  if (wanted) history.replaceState({}, "", location.pathname);
  await refreshList();
  if (wanted) {
    await open(wanted);
  } else if (!state.current) {
    const first = document.querySelector("#list li");
    if (first && first.dataset.id) await open(first.dataset.id);
  }
}

async function tick() {
  await refreshList();
  if (state.current) {
    try {
      const item = await apiJSON("/api/recordings/" + state.current);
      if (JSON.stringify(item) !== JSON.stringify(state.cache[state.current])) {
        state.cache[state.current] = item;
        renderAll(item);
      }
    } catch (_) { /* geçici */ }
  }
  setTimeout(tick, 3000);
}

boot();
tick();
animate();