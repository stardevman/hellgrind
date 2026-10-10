// Hell Grind Stüdyo: mevcut /api uç noktalarının üzerine basit bir panel. Derleme adımı yok.
"use strict";

const API = "/api";
const EMOTIONS = { neutral: "Nötr", joy: "Neşe", anger: "Öfke", fear: "Korku", sadness: "Hüzün",
  surprise: "Şaşkınlık", disgust: "İğrenme", tension: "Gerilim" };
// backend/app/config.py -> DEFAULT_PROJECT_SETTINGS ile aynı tutulmalı.
const DEFAULT_SETTINGS = {
  clip_threshold: [0.85, "Quality Gate eşiği"], excellent_threshold: [0.92, "Erken durma eşiği"],
  min_pass_rate: [0.3, "Test turu geçme oranı"], probe_n: [10, "Test turu varyasyonu"],
  full_n: [50, "Tam tur üst sınırı"], wave_size: [10, "Dalga büyüklüğü"], refined_k: [3, "Rafine havuz"],
  max_correction_rounds: [2, "Düzeltme turu"], probe_resolution: ["480p", "Test çözünürlüğü"],
  full_resolution: ["720p", "Tam tur çözünürlüğü"], soft_budget_ratio: [0.8, "Yumuşak bütçe oranı"],
  max_spend_per_10min: [20.0, "10 dk harcama sınırı"], loudness_lufs: [-23.0, "Ses yüksekliği (LUFS)"],
};
const SCENE_STATUS = {
  pending: ["Sırada", "run"], parsed: ["Hazır", ""], probing: ["Test turu", "run"], generating: ["Üretiliyor", "run"],
  correcting: ["Düzeltiliyor", "run"], refined: ["Rafine", "run"], needs_review: ["İnceleme gerekli", "bad"],
  audio: ["Ses", "run"], lipsync: ["Dudak senkronu", "run"], muxing: ["Montaj", "run"], done: ["Bitti", "ok"],
  failed: ["Hata", "bad"], frozen: ["Donduruldu", "ice"],
};
const PROJECT_STATUS = { draft: ["Taslak", ""], parsing: ["Ayrıştırılıyor", "run"], producing: ["Üretimde", "run"],
  paused: ["Duraklatıldı", "ice"], killed: ["Durduruldu", "bad"], completed: ["Tamamlandı", "ok"] };
const STAGE_NAMES = { VISUAL: "Görüntü", LIPSYNC: "Dudak senkronu", MUX: "Montaj" };
const BUSY = new Set(["pending", "probing", "generating", "correcting", "refined", "audio", "lipsync", "muxing"]);

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const usd = (n) => `$${Number(n || 0).toFixed(2)}`;
const pill = (map, key) => { const [label, cls] = map[key] || [key, ""]; return `<span class="pill ${cls}">${esc(label)}</span>`; };

// ---------- yerel hafıza (API'de proje/karakter listesi yok) ----------
const store = {
  get(k, d) { try { return JSON.parse(localStorage.getItem("hg." + k)) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem("hg." + k, JSON.stringify(v)); } catch { /* gizli pencere */ } },
};

const state = { step: 1, projectId: store.get("current", null), project: null, scenes: [], sceneId: null, poll: null };

// ---------- API ----------
async function api(method, path, body) {
  const res = await fetch(API + path, {
    method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    let msg = data?.detail ?? res.statusText;
    if (Array.isArray(msg)) msg = msg.map((d) => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join(", ");
    const err = new Error(`${res.status}: ${msg}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " error" : "");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.add("hidden"), isError ? 6000 : 3000);
}

async function withBusy(btn, fn) {
  if (btn) btn.disabled = true;
  try { return await fn(); } catch (e) { toast(e.message, true); } finally { if (btn) btn.disabled = false; }
}

const formData = (form) => Object.fromEntries(new FormData(form).entries());
const numOrNull = (v) => (v === "" || v == null ? null : Number(v));

// ---------- projeler ----------
function rememberProject(p) {
  const list = store.get("projects", []).filter((x) => x.id !== p.id);
  list.unshift({ id: p.id, title: p.title });
  store.set("projects", list.slice(0, 30));
  state.projectId = p.id;
  store.set("current", p.id);
}

function renderPicker() {
  const list = store.get("projects", []);
  $("#project-picker").innerHTML = `<option value="">— Proje seç —</option>` +
    list.map((p) => `<option value="${esc(p.id)}" ${p.id === state.projectId ? "selected" : ""}>${esc(p.title)}</option>`).join("");
}

async function loadProject() {
  if (!state.projectId) { state.project = null; state.scenes = []; return; }
  try {
    const [p, scenes] = await Promise.all([api("GET", `/projects/${state.projectId}`), api("GET", `/projects/${state.projectId}/scenes`)]);
    state.project = p;
    state.scenes = scenes;
    if (!state.scenes.some((s) => s.id === state.sceneId)) state.sceneId = state.scenes[0]?.id ?? null;
  } catch (e) {
    if (e.status === 404 || e.status === 422) {
      store.set("projects", store.get("projects", []).filter((x) => x.id !== state.projectId));
      state.projectId = null; store.set("current", null); state.project = null; state.scenes = [];
      toast("Proje bulunamadı, listeden çıkarıldı.", true);
    } else throw e;
  }
}

function budgetBar(limit, spent, reserved) {
  const pct = (n) => Math.min(100, (n / (limit || 1)) * 100).toFixed(1);
  return `<div class="bar"><span class="spent" style="width:${pct(spent)}%"></span><span class="reserved" style="width:${pct(reserved)}%"></span></div>
    <div class="row-between small"><span><b>${usd(spent)}</b> harcandı · ${usd(reserved)} ayrıldı</span><span class="muted">sınır ${usd(limit)}</span></div>`;
}

function renderProject() {
  const p = state.project;
  $("#project-empty").classList.toggle("hidden", !!p);
  $("#project-view").classList.toggle("hidden", !p);
  $("#frozen-banner").classList.toggle("hidden", !p?.frozen);
  $$("#stepper button").forEach((b) => { b.disabled = !p && b.dataset.step !== "1"; });
  if (!p) return;
  $("#pv-title").textContent = p.title;
  $("#pv-status").outerHTML = `<span id="pv-status">${pill(PROJECT_STATUS, p.status)}${p.soft_limited ? ' <span class="pill run">Yumuşak sınır</span>' : ""}</span>`;
  $("#pv-id").textContent = p.id;
  $("#pv-budget").innerHTML = budgetBar(p.budget_limit_usd, p.spent_usd, p.reserved_usd);
  $("#pv-settings").innerHTML = Object.entries(p.settings).map(([k, v]) =>
    `<span>${esc(DEFAULT_SETTINGS[k]?.[1] ?? k)}</span><span>${esc(v)}</span>`).join("");
  const chars = store.get("chars." + p.id, []);
  $("#character-list").innerHTML = chars.length ? chars.map((c) => `<li>${esc(c.name)}</li>`).join("") : `<li class="muted">Henüz karakter yok.</li>`;
  $("#scene-chars").innerHTML = chars.map((c) => `<label class="check"><input type="checkbox" name="character_ids" value="${esc(c.id)}"> ${esc(c.name)}</label>`).join("");
  $$(".character-select").forEach((sel) => {
    sel.innerHTML = `<option value="">(yok)</option>` + chars.map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("");
  });
  const btn = $("#btn-killswitch");
  btn.textContent = p.frozen ? "▶ Kill-Switch: Devam ettir" : "🧊 Kill-Switch: Dondur";
  btn.className = p.frozen ? "primary" : "danger";
}

// ---------- sahneler ----------
function nextSceneCode() {
  const max = state.scenes.reduce((m, s) => Math.max(m, Number(s.scene_code.slice(1, 4)) || 0), 0);
  return `S${String(max + 1).padStart(3, "0")}_SH01`;
}

function renderScenes() {
  const list = $("#scene-list");
  list.innerHTML = state.scenes.length ? state.scenes.map((s) => `
    <div class="scene-row">
      <span class="code">${esc(s.scene_code)}</span>
      ${pill(SCENE_STATUS, s.status)}
      <span class="muted small grow">${usd(s.spent_usd)}${s.engine ? " · " + esc(s.engine) : ""}</span>
      <button data-open-scene="${esc(s.id)}" data-to="3">Ses ve render</button>
      ${s.final_video_key ? `<button data-open-scene="${esc(s.id)}" data-to="4">İzle</button>` : ""}
    </div>`).join("") : `<div class="empty">Henüz sahne yok. Aşağıdan ilkini ekle.</div>`;
  const f = $("#form-scene");
  if (!f.scene_code.value || state.scenes.some((s) => s.scene_code === f.scene_code.value)) f.scene_code.value = nextSceneCode();
  $$(".scene-select").forEach((sel) => {
    sel.innerHTML = state.scenes.length ? state.scenes.map((s) =>
      `<option value="${esc(s.id)}" ${s.id === state.sceneId ? "selected" : ""}>${esc(s.scene_code)}</option>`).join("")
      : `<option value="">Önce sahne ekle</option>`;
  });
}

async function refreshSceneDetail() {
  const p = state.project;
  if (!state.sceneId) {
    ["#s3-status", "#s4-status"].forEach((s) => { $(s).innerHTML = ""; });
    $("#final-video").innerHTML = ""; $("#variations").innerHTML = "";
    return null;
  }
  const sc = await api("GET", `/scenes/${state.sceneId}`);
  const statusHtml = pill(SCENE_STATUS, sc.status) + (sc.last_error ? ` <span class="small muted">${esc(sc.last_error)}</span>` : "");
  $("#s3-status").innerHTML = statusHtml;
  $("#s4-status").innerHTML = statusHtml;
  const idx = state.scenes.findIndex((s) => s.id === sc.id);
  if (idx >= 0) Object.assign(state.scenes[idx], { status: sc.status, spent_usd: Number(sc.spent_usd), final_video_key: sc.final_video_key, engine: sc.engine });

  const f = $("#form-patch");
  if (!$("#scene-edit").open || f.dataset.scene !== sc.id) {
    f.dataset.scene = sc.id;
    f.description.value = sc.description ?? "";
    f.emotion.value = sc.emotion ?? "neutral";
    f.duration_s.value = sc.duration_s ?? "";
    f.action_complexity.value = sc.action_complexity ?? "";
    f.prompt.value = sc.prompt_spec?.prompt ?? "";
    f.negative.value = sc.prompt_spec?.negative ?? "";
    f.camera.value = JSON.stringify(sc.camera ?? {});
  }

  if (state.step === 4) {
    const fv = $("#final-video");
    if (sc.final_video_key) {
      if (fv.dataset.key !== sc.final_video_key) {
        const { url } = await api("GET", `/scenes/${sc.id}/final-url`);
        fv.dataset.key = sc.final_video_key;
        fv.innerHTML = `<video controls src="${esc(url)}"></video><p class="small"><a href="${esc(url)}" target="_blank">Videoyu indir</a></p>`;
      }
    } else {
      fv.dataset.key = "";
      fv.innerHTML = `<div class="empty">${BUSY.has(sc.status) ? "Render sürüyor, bitince video burada görünecek…" : "Bu sahnenin henüz final videosu yok. 3. adımdan render başlat."}</div>`;
    }
    const vs = await api("GET", `/scenes/${sc.id}/variations`);
    const refined = vs.filter((v) => v.refined_rank).length;
    const purged = vs.filter((v) => v.status === "purged").length;
    $("#variations").innerHTML = vs.length ? `<p class="small">${vs.length} varyasyon · ${refined} rafine · ${purged} imha edildi</p>
      <table><thead><tr><th>Tur</th><th>Dalga</th><th>Durum</th><th>CLIP</th><th>Skor</th><th>Sıra</th><th>Anomali</th><th>Maliyet</th></tr></thead><tbody>
      ${vs.map((v) => `<tr><td>${v.tier === "probe" ? "Test" : "Tam"}</td><td>${esc(v.wave_no)}</td><td>${esc(v.status)}</td>
        <td>${v.clip_score?.toFixed?.(3) ?? "–"}</td><td>${v.composite_score?.toFixed?.(3) ?? "–"}</td><td>${v.refined_rank ?? ""}</td>
        <td>${esc((v.anomalies || []).map((a) => a.type).join(", "))}</td><td>${usd(v.cost_usd)}</td></tr>`).join("")}</tbody></table>`
      : `<p class="muted small">Henüz varyasyon yok.</p>`;
    await refreshBudget();
  }
  if (p) schedulePoll(BUSY.has(sc.status));
  return sc;
}

async function refreshBudget() {
  if (!state.projectId) return;
  const b = await api("GET", `/projects/${state.projectId}/budget`);
  $("#b-bar").innerHTML = budgetBar(b.limit, b.spent, b.reserved) + `<p class="small muted">Kalan: ${usd(b.remaining)}</p>`;
  $("#b-ledger").innerHTML = b.ledger.length ? `<table><thead><tr><th>Sağlayıcı</th><th>Tür</th><th>Adet</th><th>Tutar</th></tr></thead><tbody>
    ${b.ledger.map((r) => `<tr><td>${esc(r.provider)}</td><td>${esc(r.kind)}</td><td>${r.count}</td><td>${usd(r.usd)}</td></tr>`).join("")}</tbody></table>`
    : `<p class="muted small">Henüz harcama yok.</p>`;
  $("#b-events").innerHTML = b.recent_events.length ? `<h4>Son olaylar</h4><ul class="list small">${b.recent_events.map((e) =>
    `<li><code>${esc(JSON.stringify(e))}</code></li>`).join("")}</ul>` : "";
  if (state.project) { Object.assign(state.project, { frozen: b.frozen, status: b.status, spent_usd: b.spent, reserved_usd: b.reserved }); renderProject(); }
}

function schedulePoll(active) {
  clearTimeout(state.poll);
  if (active) state.poll = setTimeout(() => refreshSceneDetail().then(() => { renderScenes(); }).catch(() => {}), 2000);
}

// ---------- gezinme ----------
function goto(step) {
  if (step !== 1 && !state.project) step = 1;
  state.step = step;
  $$(".step").forEach((s) => s.classList.toggle("active", s.dataset.step === String(step)));
  $$("#stepper button").forEach((b) => b.classList.toggle("active", b.dataset.step === String(step)));
  if (step >= 3) refreshSceneDetail().catch((e) => toast(e.message, true));
  if (step === 4 && !state.sceneId) refreshBudget().catch((e) => toast(e.message, true));
}

async function refreshAll() {
  renderPicker();
  await loadProject();
  renderProject();
  renderScenes();
  goto(state.step);
}

// ---------- olaylar ----------
function initSettingsFields() {
  $("#settings-fields").innerHTML = Object.entries(DEFAULT_SETTINGS).map(([k, [def, label]]) =>
    `<label>${esc(label)}<input name="set_${k}" placeholder="${esc(def)}" ${typeof def === "number" ? 'type="number" step="any"' : ""}></label>`).join("");
  $$(".emotion-select").forEach((sel) => {
    sel.insertAdjacentHTML("beforeend", Object.entries(EMOTIONS).map(([v, l]) => `<option value="${v}">${l}</option>`).join(""));
  });
}

async function createProject(title, budget, settings = {}) {
  const p = await api("POST", "/projects", { title, budget_limit_usd: budget, settings });
  rememberProject(p);
  return p;
}

function bind() {
  $("#stepper").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b && !b.disabled) goto(Number(b.dataset.step)); });
  document.addEventListener("click", (e) => {
    const g = e.target.closest("[data-goto]");
    if (g) goto(Number(g.dataset.goto));
    const o = e.target.closest("[data-open-scene]");
    if (o) { state.sceneId = o.dataset.openScene; renderScenes(); goto(Number(o.dataset.to)); }
  });

  $("#project-picker").addEventListener("change", async (e) => {
    state.projectId = e.target.value || null; store.set("current", state.projectId); state.sceneId = null;
    state.step = state.projectId ? state.step : 1;
    await refreshAll().catch((err) => toast(err.message, true));
  });
  $("#btn-new-project").addEventListener("click", () => { state.projectId = null; store.set("current", null); refreshAll(); });

  $("#form-project").addEventListener("submit", (e) => {
    e.preventDefault();
    const d = formData(e.target);
    const settings = {};
    for (const [k, [def]] of Object.entries(DEFAULT_SETTINGS)) {
      const v = d["set_" + k];
      if (v !== undefined && v !== "") settings[k] = typeof def === "number" ? Number(v) : v;
    }
    withBusy(e.submitter, async () => {
      await createProject(d.title, Number(d.budget_limit_usd), settings);
      e.target.reset();
      toast("Proje oluşturuldu.");
      state.step = 2;
      await refreshAll();
    });
  });

  $("#form-open").addEventListener("submit", (e) => {
    e.preventDefault();
    const id = formData(e.target).id.trim();
    withBusy(e.submitter, async () => { const p = await api("GET", `/projects/${id}`); rememberProject(p); await refreshAll(); });
  });

  $("#btn-demo").addEventListener("click", (e) => withBusy(e.currentTarget, async () => {
    const p = await createProject("Örnek: Çöken Köprü", 25, { full_n: 20 });
    const sc = await api("POST", `/projects/${p.id}/scenes`, {
      scene_code: "S001_SH01", seq_order: 101, emotion: "fear",
      description: "Kerem çöken köprünün kenarında dengesini kaybeder.",
      prompt_spec: { prompt: "[CHAR:Kerem] stumbling on a collapsing bridge", negative: "" },
    });
    await api("PUT", `/scenes/${sc.id}/audio`, { kind: "dialogue", text: "Dayan! Bırakma elimi!", emotion: "fear", start_offset_s: 0.5 });
    state.sceneId = sc.id;
    state.step = 4;
    toast("Örnek proje hazır, render başladı.");
    await refreshAll();
  }));

  $("#form-character").addEventListener("submit", (e) => {
    e.preventDefault();
    const d = formData(e.target);
    const body = { name: d.name, lora_scale: Number(d.lora_scale || 1) };
    for (const k of ["description", "lora_url", "lora_trigger", "voice_id"]) if (d[k]) body[k] = d[k];
    withBusy(e.submitter, async () => {
      const c = await api("POST", `/projects/${state.projectId}/characters`, body);
      store.set("chars." + state.projectId, [...store.get("chars." + state.projectId, []), c]);
      e.target.reset();
      toast(`${c.name} eklendi.`);
      renderProject();
    });
  });

  $("#form-scene").addEventListener("submit", (e) => {
    e.preventDefault();
    const f = e.target;
    const d = formData(f);
    const code = d.scene_code;
    const body = {
      scene_code: code,
      seq_order: numOrNull(d.seq_order) ?? Number(code.slice(1, 4)) * 100 + Number(code.slice(-2)),
      script_text: d.script_text || "", description: d.description || null, emotion: d.emotion,
      camera: { shot: d.shot, movement: d.movement, angle: d.angle, lens_mm: Number(d.lens_mm || 35) },
      action_complexity: Number(d.action_complexity || 0), duration_s: Number(d.duration_s || 5),
      prompt_spec: { prompt: d.prompt || "", negative: d.negative || "" },
      character_ids: $$('input[name="character_ids"]:checked', f).map((i) => i.value),
    };
    withBusy(e.submitter, async () => {
      const sc = await api("POST", `/projects/${state.projectId}/scenes`, body);
      f.reset();
      state.sceneId = sc.id;
      toast(`${sc.scene_code} eklendi. Şimdi ses ekleyip render et.`);
      await refreshAll();
    });
  });

  $$(".scene-select").forEach((sel) => sel.addEventListener("change", (e) => {
    state.sceneId = e.target.value; $("#s3-plan").textContent = ""; renderScenes();
    refreshSceneDetail().catch((err) => toast(err.message, true));
  }));

  const audioForm = $("#form-audio");
  const syncDialogue = () => $$(".dialogue-only", audioForm).forEach((el) => el.classList.toggle("hidden", audioForm.kind.value !== "dialogue"));
  audioForm.kind.addEventListener("change", () => {
    syncDialogue();
    if (audioForm.kind.value !== "dialogue" && audioForm.source.value === "dry_run") audioForm.source.value = "sample";
    if (audioForm.kind.value === "dialogue") audioForm.source.value = "dry_run";
  });
  syncDialogue();
  audioForm.addEventListener("submit", (e) => {
    e.preventDefault();
    if (!state.sceneId) return toast("Önce bir sahne seç.", true);
    const d = formData(audioForm);
    const body = { kind: d.kind, source: d.source, start_offset_s: Number(d.start_offset_s || 0), gain_db: Number(d.gain_db || 0) };
    if (d.kind === "dialogue" && d.text) body.text = d.text;
    if (d.emotion) body.emotion = d.emotion;
    if (d.character_id) body.character_id = d.character_id;
    if (d.file_key) body.file_key = d.file_key;
    withBusy(e.submitter, async () => {
      const r = await api("PUT", `/scenes/${state.sceneId}/audio`, body);
      showPlan(r.stages, `Ses sürüm ${r.version} kaydedildi.`);
      await refreshSceneDetail();
    });
  });

  $("#btn-render").addEventListener("click", (e) => {
    if (!state.sceneId) return toast("Önce bir sahne seç.", true);
    const force = $("#force-visual").checked;
    withBusy(e.currentTarget, async () => {
      const r = await api("POST", `/scenes/${state.sceneId}/render${force ? "?force_visual=true" : ""}`);
      showPlan(r.stages);
      await refreshSceneDetail();
    });
  });

  $("#form-patch").addEventListener("submit", (e) => {
    e.preventDefault();
    const d = formData(e.target);
    let camera;
    try { camera = d.camera ? JSON.parse(d.camera) : null; } catch { return toast("Kamera alanı geçerli JSON değil.", true); }
    const body = { description: d.description, emotion: d.emotion, camera,
      duration_s: numOrNull(d.duration_s), action_complexity: numOrNull(d.action_complexity),
      prompt_spec: { prompt: d.prompt, negative: d.negative } };
    withBusy(e.submitter, async () => {
      await api("PATCH", `/scenes/${state.sceneId}`, body);
      toast("Kaydedildi. Uygulamak için Render'a bas.");
      await refreshSceneDetail();
    });
  });

  const toggleKill = (btn) => withBusy(btn, async () => {
    const freeze = !state.project.frozen;
    const reason = freeze ? (prompt("Dondurma nedeni:", "manual") ?? null) : "";
    if (freeze && reason === null) return;
    await api("POST", `/projects/${state.projectId}/killswitch`, freeze ? { action: "freeze", reason: reason || "manual" } : { action: "resume" });
    toast(freeze ? "Proje donduruldu." : "Proje devam ediyor.");
    await refreshAll();
  });
  $("#btn-killswitch").addEventListener("click", (e) => toggleKill(e.currentTarget));
  $("#btn-banner-resume").addEventListener("click", (e) => toggleKill(e.currentTarget));

  $("#btn-tour").addEventListener("click", () => tour.start());
}

function showPlan(stages, prefix = "") {
  const names = stages.map((s) => STAGE_NAMES[s] || s);
  $("#s3-plan").textContent = `${prefix} ` + (names.length
    ? `Çalışacak aşamalar: ${names.join(" → ")}.`
    : "Değişiklik yok, hiçbir aşama çalışmadı (harcama yok).");
}

// ---------- tutorial ----------
const TOUR_STEPS = [
  { step: 1, target: ".stepper", title: "Hoş geldin!", text: "Hell Grind'da bir sahneyi videoya dönüştürmek 4 adım: Proje, Sahneler, Ses ve Render, Sonuç. Bu şeritten adımlar arasında geçebilirsin." },
  { step: 1, target: "#form-project", whenNoProject: true, title: "1. Proje oluştur", text: "Bir ad ve bütçe sınırı gir. Bütçe dolarsa üretim kendiliğinden durur, fazla para harcanmaz. İnce ayarlar 'Gelişmiş' altında." },
  { step: 1, target: "#btn-demo", whenNoProject: true, title: "Acelen mi var?", text: "Bu düğme sahnesi ve diyaloğu hazır bir örnek proje kurar ve render'ı başlatır. Denemek için en kısa yol." },
  { step: 1, target: "#characters-box", whenProject: true, title: "Karakterler", text: "İstersen karakter ekle; sonra sahnelere ve repliklere bağlayabilirsin. Zorunlu değil." },
  { step: 2, target: "#scene-create", title: "2. Sahne ekle", text: "Sahne kodu otomatik önerilir. Ne olduğunu ve duyguyu yazman yeterli; kamera ve süre 'Gelişmiş' altında." },
  { step: 3, target: "#audio-box", title: "3. Ses ekle", text: "Replik veya müzik ekle. Ses eklenince render kendiliğinden başlar. Sadece sesi değiştirirsen video yeniden üretilmez, para harcanmaz." },
  { step: 3, target: "#render-box", title: "Render", text: "Sahneyi düzenledikten sonra buradan render et. Değişiklik yoksa hiçbir şey çalışmaz." },
  { step: 4, target: "#result-box", title: "4. Sonucu izle", text: "Render bitince final video burada oynar. 'Varyasyonlar' altında kalite kontrolünün hangi denemeleri seçip hangilerini elediğini görebilirsin." },
  { step: 4, target: "#budget-box", title: "Bütçe ve Kill-Switch", text: "Harcamayı buradan izle. Bir şey ters giderse Kill-Switch tüm üretimi tek tıkla dondurur; sonra devam ettirebilirsin." },
  { step: 1, target: "#btn-tour", title: "Hepsi bu!", text: "Bu rehberi istediğin zaman ❓ Rehber düğmesiyle yeniden açabilirsin." },
];

const tour = {
  i: 0, steps: [], prevStep: 1,
  start() {
    const hasProject = !!state.project;
    this.steps = TOUR_STEPS.filter((s) => !(s.whenNoProject && hasProject) && !(s.whenProject && !hasProject));
    this.i = 0; this.prevStep = state.step;
    $("#tour").classList.remove("hidden");
    this.show();
  },
  end() {
    $("#tour").classList.add("hidden");
    store.set("tourDone", true);
    renderProject();
    goto(state.project ? this.prevStep : 1);
  },
  show() {
    const s = this.steps[this.i];
    // Adımlar proje yokken de gösterilebilsin diye bölümü doğrudan aç.
    $$(".step").forEach((el) => el.classList.toggle("active", el.dataset.step === String(s.step)));
    $$("#stepper button").forEach((b) => b.classList.toggle("active", b.dataset.step === String(s.step)));
    if (!state.project && s.step > 1) { $("#project-empty").classList.add("hidden"); }
    else if (!state.project) { $("#project-empty").classList.remove("hidden"); }
    const el = $(s.target);
    $("#tour-count").textContent = `${this.i + 1} / ${this.steps.length}`;
    $("#tour-title").textContent = s.title;
    $("#tour-text").textContent = s.text;
    $("#tour-prev").disabled = this.i === 0;
    $("#tour-next").textContent = this.i === this.steps.length - 1 ? "Bitir" : "İleri";
    el.scrollIntoView({ block: "center", behavior: "instant" });
    requestAnimationFrame(() => this.place(el));
  },
  place(el) {
    const r = el.getBoundingClientRect(), pad = 6;
    Object.assign($("#tour-spot").style, { top: `${r.top - pad}px`, left: `${r.left - pad}px`, width: `${r.width + pad * 2}px`, height: `${r.height + pad * 2}px` });
    const card = $("#tour-card"), ch = card.offsetHeight, cw = card.offsetWidth;
    let top = r.bottom + 14;
    if (top + ch > innerHeight - 10) top = Math.max(10, r.top - ch - 14);
    const left = Math.min(Math.max(10, r.left), innerWidth - cw - 10);
    Object.assign(card.style, { top: `${top}px`, left: `${left}px` });
  },
};

function bindTour() {
  $("#tour-next").addEventListener("click", () => { if (tour.i < tour.steps.length - 1) { tour.i++; tour.show(); } else tour.end(); });
  $("#tour-prev").addEventListener("click", () => { if (tour.i > 0) { tour.i--; tour.show(); } });
  $("#tour-skip").addEventListener("click", () => tour.end());
  document.addEventListener("keydown", (e) => {
    if ($("#tour").classList.contains("hidden")) return;
    if (e.key === "Escape") tour.end();
    if (e.key === "ArrowRight") $("#tour-next").click();
    if (e.key === "ArrowLeft") $("#tour-prev").click();
  });
  addEventListener("resize", () => { if (!$("#tour").classList.contains("hidden")) tour.show(); });
}

// ---------- başlat ----------
async function init() {
  initSettingsFields();
  bind();
  bindTour();
  fetch("/healthz").then((r) => r.json()).then((h) => {
    $("#health").className = "pill " + (h.dry_run ? "ok" : "run");
    $("#health").textContent = h.dry_run ? "Deneme modu · $0" : "Gerçek API · ücretli";
    $("#health").title = h.dry_run ? "DRY_RUN açık: ücretli API çağrılmaz" : "DRY_RUN kapalı: gerçek harcama yapılır";
  }).catch(() => { $("#health").className = "pill bad"; $("#health").textContent = "API'ye ulaşılamıyor"; });
  if (state.projectId) state.step = 2;
  await refreshAll().catch((e) => toast(e.message, true));
  if (!store.get("tourDone", false)) tour.start();
}

init();
