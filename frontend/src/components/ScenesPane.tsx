"use client";
import { memo, useEffect, useRef, useState, type MouseEvent } from "react";
import { api } from "@/lib/api";
import { parseIds, playable, toggle } from "@/lib/compare";
import { EMOTION_COLOR, TYPE_TR, ASTATUS_TR } from "@/lib/i18n";
import { usePanel } from "@/lib/panel";
import { clock, deltaText, dispatchText, eventText, resultText, tileSpec, usd } from "@/lib/text";
import type { DispatchAll, Scene, SceneAssetRef, Sequence, StageResult, Variation } from "@/lib/types";
import { Inspector } from "./Inspector";
import { NoteBox, StatusPill, UploadForm } from "./ui";

/* ---------- film şeridi ---------- */

const Tile = memo(function Tile({ v, prev, selected, picked, onPick }: {
  v: Variation; prev?: Variation; selected: boolean; picked: boolean; onPick: (v: Variation, e: MouseEvent) => void;
}) {
  const t = tileSpec(v, prev);
  const cls = ["tile", t.state === "passed" ? "" : t.state, t.tierStart ? "tier-start" : "", selected ? "selected" : "",
    picked ? "compare-pick" : ""].filter(Boolean).join(" ");
  const label = t.label + (picked ? " · karşılaştırmada" : "");
  return (
    <button type="button" className={cls} aria-pressed={selected} aria-label={label} title={label} onClick={(e) => onPick(v, e)}>
      {v.thumb_url && <img src={v.thumb_url} alt="" loading="lazy" decoding="async" />}
      {v.revision ? <span className="rev">R{v.revision}</span> : null}
      {picked ? <span className="cmp">K</span> : null}
      {v.rank ? <span className="rank">{v.rank}</span> : null}
    </button>
  );
});

export function MissingLinks({ list }: { list: SceneAssetRef[] }) {
  const { gotoAsset } = usePanel();
  return (
    <>
      {list.map((m, i) => (
        <span key={m.id ?? m.name}>
          {i > 0 && ", "}
          {m.id
            ? <button type="button" className="linkbtn" onClick={() => gotoAsset(m.id!)}
                title={`${TYPE_TR[m.type]} · ${m.status ? ASTATUS_TR[m.status] : "kaydı yok"}`}>{m.name}</button>
            : m.name}
        </span>
      ))}
    </>
  );
}

function SceneRow({ sc }: { sc: Scene }) {
  const { scene, selectScene, varId, setVar, compareRaw, setCompare, act, note } = usePanel();
  const selected = scene?.id === sc.id;
  const picked = selected ? parseIds(compareRaw, sc) : [];
  const onPick = (v: Variation, e: MouseEvent) => {
    if (e.shiftKey && playable(v)) {                       // Shift+tık: karşılaştırmaya ekle / çıkar
      if (!selected) { selectScene(sc.id); setCompare([v.id]); }
      else setCompare(toggle(picked, v.id));
      return;
    }
    setVar(selected && varId === v.id ? null : v.id, sc.id);
  };
  const error = sc.last_error && !["done", "blocked"].includes(sc.status) ? sc.last_error.split("\n")[0] : "";
  const upgrade = () => act(async () => {
    const r = await api.post<{ upgrade_requested: boolean }>(`/scenes/${sc.id}/upgrade`, { requested: !sc.upgrade_requested });
    note("inspector", r.upgrade_requested ? `${sc.scene_code} iyileştirme geçişine alınacak.` : `${sc.scene_code} işareti kaldırıldı.`, "ok");
  }, "inspector");
  return (
    <article className={`scene${selected ? " selected" : ""}`}>
      <div className="scene-head">
        <button type="button" className="scene-pick" aria-label={`${sc.scene_code} sahnesini seç`} onClick={() => selectScene(sc.id)}>
          {sc.scene_code}
        </button>
        <StatusPill status={sc.status} />
        <span className={`tier ${sc.cut_tier || ""}`}>{sc.cut_tier === "draft" ? "Taslak" : sc.cut_tier === "final" ? "Final" : ""}</span>
        {(sc.cut_tier === "draft" || sc.upgrade_requested) && (
          <button type="button" className="btn small upgrade-btn" aria-pressed={sc.upgrade_requested} onClick={upgrade}>
            {sc.upgrade_requested ? "İyileştirilecek" : "İyileştir"}
          </button>
        )}
      </div>
      <div className="scene-info">
        <p className="desc">{sc.description}</p>
        <p className="head">
          {sc.heading}
          {error && <> · <span className="warn">{error}</span></>}
          {sc.status === "blocked" && sc.missing.length > 0 && <> · <span className="warn">Eksik referans:</span> <MissingLinks list={sc.missing} /></>}
        </p>
      </div>
      <div className="spend num muted" title="Sahne harcaması">{usd(sc.spent_usd)}</div>
      <div className="strip" aria-label={`${sc.scene_code} film şeridi`}>
        {sc.variations.length
          ? sc.variations.map((v, i) => (
            <Tile key={v.id} v={v} prev={sc.variations[i - 1]} selected={selected && varId === v.id}
              picked={picked.includes(v.id)} onPick={onPick} />
          ))
          : <span className="muted none">Henüz klip yok</span>}
      </div>
    </article>
  );
}

function SeqHead({ q, count }: { q: Sequence | null; count: number }) {
  if (!q) return <header className="seq-head"><h3>Sekanssız</h3><span className="muted">{count} çekim</span></header>;
  return (
    <header className="seq-head">
      <h3>{q.code} · {q.title}</h3>
      <span className="muted num">
        {q.shots} çekim · {q.done} hazır · {usd(q.spent_usd)} harcandı · {q.budget_cap_usd != null ? `tavan ${usd(q.budget_cap_usd)}` : "tavan yok"}
      </span>
      {q.status === "capped" && <span className="astatus capped">Tavan doldu</span>}
    </header>
  );
}

/** Sahneleri sekanslarına göre gruplar; sekansı olmayanlar en sonda "Sekanssız" altında toplanır. */
export function groupBySequence(scenes: Scene[], seqs: Sequence[]) {
  const groups = seqs.map((q) => ({ id: q.id, q: q as Sequence | null, scenes: scenes.filter((sc) => sc.sequence_id === q.id) }));
  const loose = scenes.filter((sc) => !seqs.some((q) => q.id === sc.sequence_id));
  if (loose.length) groups.push({ id: "none", q: null, scenes: loose });
  return groups.filter((g) => g.scenes.length);
}

export function RetryBlocked({ noteKey }: { noteKey: string }) {
  const { ov, projectId, busy, frozen, act, note } = usePanel();
  if (!ov?.scenes.some((x) => x.status === "blocked")) return null;
  return (
    <button className="btn small" type="button" disabled={busy || frozen} onClick={() => act(async () => {
      const r = await api.post<DispatchAll>(`/projects/${projectId}/render-all`);
      const x = dispatchText(r);
      note(noteKey, x.text, x.bad ? "err" : "ok");
    }, noteKey)}>Engellenen çekimleri yeniden dene</button>
  );
}

function SceneList() {
  const { ov } = usePanel();
  if (!ov) return null;
  const done = ov.scenes.filter((s) => s.status === "done").length;
  return (
    <section className="panel" aria-labelledby="scenes-title">
      <header>
        <h2 id="scenes-title">Sahneler</h2>
        <span className="muted num">{ov.sequences.length} sekans · {ov.scenes.length} sahne · {done} hazır</span>
        <span className="spacer" />
        <RetryBlocked noteKey="inspector" />
      </header>
      <div>
        {groupBySequence(ov.scenes, ov.sequences).map((g) => (
          <section className="seq-group" key={g.id}>
            <SeqHead q={g.q} count={g.scenes.length} />
            <div className="seq-rows">{g.scenes.map((sc) => <SceneRow key={sc.id} sc={sc} />)}</div>
          </section>
        ))}
      </div>
      <div className="legend" aria-label="Film şeridi göstergeleri">
        <span><i className="refined" />rafine havuzda</span>
        <span><i className="rejected" />kalite kontrolünde elendi</span>
        <span><i className="retired" />havuza giremedi</span>
        <span><i className="working" />üretiliyor</span>
        <span><kbd>Shift</kbd>+tık: karşılaştırmaya ekle</span>
      </div>
    </section>
  );
}

/* ---------- film ---------- */

function Film() {
  const { ov, projectId, filmKind, setFilmKind, assembling, setAssembling, busy, frozen, act, note, setVar, selectScene } = usePanel();
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [now, setNow] = useState(0);
  const draft = filmKind === "draft";
  const film = (draft ? ov?.draft_timeline : ov?.timeline) ?? null;
  const waiting = Boolean(assembling && !(film && film.url !== assembling.before) && Date.now() - assembling.at < 90000);
  useEffect(() => { if (assembling && !waiting) setAssembling(null); }, [assembling, waiting, setAssembling]);   // yeni film geldi ya da süre doldu
  if (!ov) return null;
  const done = draft ? ov.scenes.filter((s) => s.draft_url || s.status === "done").length : ov.scenes.filter((s) => s.status === "done").length;
  const clips = film?.cut?.clips ?? [];
  const bands = film?.cut?.sequences ?? [];
  const p = ov.project;
  const report = p.continuity, jumps = report ? report.pairs.filter((x) => x.jump) : [];
  const jumpsTo = new Set(jumps.map((j) => j.to));
  const byCode = Object.fromEntries(ov.scenes.map((s) => [s.scene_code, s.id]));

  const assemble = () => {
    const before = film?.url ?? null;
    void act(async () => {
      if (draft) await api.post(`/projects/${projectId}/assemble?kind=draft`);
      else await api.post(`/projects/${projectId}/assemble`);
      setAssembling({ before, at: Date.now() });
      note("film", draft ? "Kaba kurgu birleştirmeye alındı." : "Film birleştirmeye alındı.", "ok");
    }, "film");
  };
  const seek = (inS: number, code: string) => {
    const v = videoRef.current;
    if (v) { v.currentTime = inS + 0.05; v.play().catch(() => {}); }
    if (byCode[code]) { selectScene(byCode[code]); setVar(null); }
  };

  return (
    <section className="panel" aria-labelledby="film-title">
      <header>
        <h2 id="film-title">Film</h2>
        <div className="seg" role="group" aria-label="Film türü">
          <button type="button" aria-pressed={draft} onClick={() => setFilmKind("draft")}>Kaba kurgu</button>
          <button type="button" aria-pressed={!draft} onClick={() => setFilmKind("final")}>Final</button>
        </div>
        <span className="spacer" />
        <button className="btn primary" type="button" disabled={done === 0 || waiting || busy} onClick={assemble}>
          {waiting ? "Birleştiriliyor…" : draft ? `Kaba kurguyu birleştir (${done} sahne)` : `Tamamlanan ${done} sahneyi birleştir`}
        </button>
      </header>
      <div className="body">
        <div className="media">
          {film
            ? <video key={film.url} ref={videoRef} src={film.url} controls preload="metadata" playsInline
                onTimeUpdate={(e) => setNow(e.currentTarget.currentTime)} />
            : <p className="none">Henüz film yok. Sahneler tamamlanınca birleştirin.</p>}
        </div>
        <div className="form-row mt10">
          <button className="btn small" type="button" disabled={busy || frozen}
            onClick={() => act(async () => { await api.post(`/projects/${projectId}/continuity`); note("film", "Süreklilik denetimi kuyruğa alındı.", "ok"); }, "film")}>
            Sürekliliği denetle
          </button>
          <UploadForm label="Film LUT'u (.cube)" accept=".cube" button="LUT yükle"
            onUpload={(file) => act(async () => {
              const r = await api.upload<{ lut: { size: number }; regraded: string[] }>(`/projects/${projectId}/lut?filename=${encodeURIComponent(file.name)}`, file);
              note("film", `LUT yüklendi (${r.lut.size}³); yeniden renklendirilen: ${r.regraded.join(", ") || "yok"}.`, "ok");
            }, "film")} />
          {p.lut && (
            <button className="btn small" type="button" disabled={busy} onClick={() => act(async () => {
              const r = await api.del<{ regraded: string[] }>(`/projects/${projectId}/lut`);
              note("film", `LUT kaldırıldı; yeniden renklendirilen: ${r.regraded.join(", ") || "yok"}.`, "ok");
            }, "film")}>LUT&apos;u kaldır</button>
          )}
          <span className="muted">{p.lut ? "Film LUT'u etkin." : ""}</span>
        </div>
        <div className="jumps" aria-live="polite">
          {!report ? <p className="muted">Süreklilik henüz denetlenmedi; film birleşince kendiliğinden denetlenir.</p>
            : jumps.length ? jumps.map((j) => (
              <div className="jump" key={`${j.from}-${j.to}`}>
                <b>{j.from} → {j.to}: renk sıçraması</b>
                <span>{deltaText(j.delta)}</span>
                {j.suggestion && (
                  <button className="btn small" type="button" disabled={busy || frozen} onClick={() => act(async () => {
                    const r = await api.post<StageResult>(`/scenes/${j.to_id}/grade`, j.suggestion);
                    note("film", "Önerilen renk uygulandı: " + resultText(r) + " Süreklilik, montaj bitince yeniden ölçülür.", "ok");
                  }, "film")}>Öneriyi uygula</button>
                )}
              </div>
            ))
            : <p className="muted">Renk sıçraması yok ({report.pairs.length} geçiş denetlendi).</p>}
        </div>
        <div className="seqbar" aria-label="Sekanslar">
          {bands.map((b) => (
            <span key={b.code} style={{ flex: `${Math.max(0.1, b.out_s - b.in_s)} 1 0` }} title={`${b.code} · ${b.title}`}>{b.code} · {b.title}</span>
          ))}
        </div>
        <div className="cutbar" aria-label="Kesme listesi">
          {clips.map((c) => (
            <button key={c.scene_code} type="button"
              className={`cut${jumpsTo.has(c.scene_code) ? " jump-in" : ""}${now >= c.in_s && now < c.out_s ? " active" : ""}`}
              style={{ flex: `${Math.max(0.1, c.out_s - c.in_s)} 1 0`, background: EMOTION_COLOR[c.emotion || ""] || "#A39E95" }}
              title={`${c.scene_code} · ${c.description || ""}`} aria-label={`${c.scene_code} sahnesine git`}
              onClick={() => seek(c.in_s, c.scene_code)}>{c.scene_code}</button>
          ))}
        </div>
        <NoteBox id="film" className="mt10" />
      </div>
    </section>
  );
}

function Events() {
  const { ov } = usePanel();
  if (!ov) return null;
  const codes = Object.fromEntries(ov.scenes.map((s) => [s.id, s.scene_code]));
  return (
    <section className="panel" aria-labelledby="events-title">
      <header><h2 id="events-title">Olay akışı</h2></header>
      <ol className="events">
        {ov.events.length ? ov.events.map((e, i) => {
          const x = eventText(e, codes);
          return <li key={`${e.ts}-${i}`}><time>{clock(e.ts)}</time><span className={x.c || ""}>{x.t}</span></li>;
        }) : <li><span className="muted" /><span className="muted">Henüz olay yok.</span></li>}
      </ol>
    </section>
  );
}

export function ScenesPane() {
  return (
    <>
      <div className="layout">
        <SceneList />
        <Inspector />
      </div>
      <div className="film">
        <Film />
        <Events />
      </div>
    </>
  );
}
