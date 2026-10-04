"use client";
/* Varyasyon karşılaştırma: aynı çekimin 2–4 adayı yan yana, tek taşıma çubuğuyla eşzamanlı oynatılır; ölçümler
   (birleşik skor, stil/tarif uyumu, yüz benzerliği) tabloda karşılaştırılır ve havuzdaki aday tek tıkla seçilir. */
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { bestIndex, candidates, defaultSelection, deltaFrom, FRAME_S, MAX_COMPARE, measureRows, needsResync, parseIds, toggle } from "@/lib/compare";
import { ALIGN_TR, ANOMALY_TR } from "@/lib/i18n";
import { usePanel } from "@/lib/panel";
import { resultText, tileSpec, usd, variationTitle } from "@/lib/text";
import { timecode } from "@/lib/timeline";
import type { Scene, StageResult, Variation } from "@/lib/types";
import { Icon } from "./icons";
import { NoteBox, Panel, shortcutTarget } from "./ui";

type Mode = "grid" | "wipe";
const SPEEDS = [0.25, 0.5, 1];

/** Oynatıcıları tek bir saate bağlar: ilk oynatıcı lider, diğerleri kaydıkça ona hizalanır. */
function useSyncedPlayers(ids: string[]) {
  const players = useRef(new Map<string, HTMLVideoElement>());
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [speed, setSpeed] = useState(1);
  const [loop, setLoop] = useState(true);
  const lastPaint = useRef(0);
  const order = useRef(ids);
  order.current = ids;                                  // sıra değişince geri çağrılar yeniden kurulmasın

  const list = useCallback(() => order.current.map((id) => players.current.get(id)).filter((v): v is HTMLVideoElement => Boolean(v)), []);
  const leader = useCallback(() => list()[0], [list]);

  const bind = useCallback((id: string) => (el: HTMLVideoElement | null) => {
    if (el) {
      players.current.set(id, el);
      el.muted = true;
      el.playbackRate = speed;
    } else players.current.delete(id);
  }, [speed]);

  const seek = useCallback((t: number) => {
    const all = list();
    const max = Math.max(0, ...all.map((v) => v.duration || 0));
    const to = Math.min(Math.max(0, t), max || 0);
    for (const v of all) v.currentTime = Math.min(to, Math.max(0, (v.duration || to) - 0.001));
    setTime(to);
  }, [list]);

  const play = useCallback(() => {
    const all = list(), lead = all[0];
    if (!lead) return;
    if (lead.ended || lead.currentTime >= (lead.duration || 0) - 0.01) seek(0);
    for (const v of all) v.play().catch(() => { /* tarayıcı reddederse oynatıcı duraklamış kalır */ });
    setPlaying(true);
  }, [list, seek]);

  const pause = useCallback(() => {
    const all = list();
    for (const v of all) v.pause();
    setPlaying(false);
    if (all[0]) seek(all[0].currentTime);                 // durunca hepsi aynı karede
  }, [list, seek]);

  const step = useCallback((dt: number) => {
    const lead = leader();
    if (!lead) return;
    if (playing) pause();
    seek(lead.currentTime + dt);
  }, [leader, playing, pause, seek]);

  useEffect(() => { for (const v of list()) v.playbackRate = speed; }, [speed, list]);

  // Oynarken: süre ve konum çubuğu, kaymayı düzeltme, döngü.
  useEffect(() => {
    if (!playing) return;
    let raf = 0;
    const tick = (now: number) => {
      const all = list(), lead = all[0];
      if (lead) {
        for (const v of all.slice(1)) {
          if (needsResync(lead.currentTime, v.currentTime, v.duration || lead.duration || 0)) v.currentTime = Math.min(lead.currentTime, (v.duration || lead.currentTime) - 0.001);
        }
        if (lead.ended || (lead.duration && lead.currentTime >= lead.duration - 0.02)) {
          if (loop) { for (const v of all) { v.currentTime = 0; v.play().catch(() => {}); } }
          else { for (const v of all) v.pause(); setPlaying(false); }
        }
        if (now - lastPaint.current > 80) { lastPaint.current = now; setTime(lead.currentTime); }
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [playing, loop, list]);

  const onMeta = useCallback(() => {
    const d = Math.max(0, ...list().map((v) => v.duration || 0));
    setDuration(d);
    const lead = leader();
    if (lead) for (const v of list()) if (v !== lead && Math.abs(v.currentTime - lead.currentTime) > 0.001) v.currentTime = lead.currentTime;
  }, [list, leader]);

  // Seçim değişince yeni oynatıcılar baştan, duraklamış başlar.
  const key = ids.join(",");
  useEffect(() => {
    for (const v of list()) v.pause();
    setPlaying(false);
    setTime(list()[0]?.currentTime ?? 0);
  }, [key, list]);

  return { bind, play, pause, step, seek, playing, time, duration, speed, setSpeed, loop, setLoop, onMeta };
}

function fateOf(v: Variation): { text: string; cls: string } {
  const t = tileSpec(v);
  const cls = t.state === "rejected" || t.state === "failed" ? "bad" : t.state === "refined" ? "good" : "";
  return { text: t.fate.charAt(0).toLocaleUpperCase("tr") + t.fate.slice(1), cls };
}

function Slot({ v, idx, lead, bind, onMeta, solo, onSolo, onRemove, onPin, sc }: {
  v: Variation; idx: number; lead: number | null; sc: Scene; onPin: () => void;
  bind: (el: HTMLVideoElement | null) => void; onMeta: () => void; solo: boolean; onSolo: () => void; onRemove: () => void;
}) {
  const { busy, frozen, act, note, setVar, setPage } = usePanel();
  const fate = fateOf(v);
  const promote = () => act(async () => {
    onPin();                                 // yuvalar sıralamayla yer değiştirmesin: seçilen aday yerinde kalır
    const r = await api.post<StageResult>(`/scenes/${sc.id}/promote/${v.id}`);
    note("compare", `${v.rank}. sıradaki aday seçildi. ${resultText(r)}`, r.blocked ? "err" : "ok");
  }, "compare");
  const isLead = lead != null && v.score === lead;
  return (
    <article className={`cmp-slot${solo ? " focus" : ""}`} aria-label={`${idx + 1}. aday`}>
      <video ref={bind} src={v.video_url ?? undefined} poster={v.thumb_url ?? undefined} preload="auto" playsInline muted
        onLoadedMetadata={onMeta} onClick={onSolo} title={solo ? "Izgaraya dön" : "Tek başına büyüt"} />
      <div className="osd">
        <span className="mono">{idx + 1} · {variationTitle(v)}</span>
        <span className={`mono${isLead ? " lead" : ""}`}>{v.score != null ? v.score.toFixed(3) : "—"}{v.rank ? ` · #${v.rank}` : ""}</span>
      </div>
      <div className={`cmp-info${v.rank === 1 ? " use" : ""}`}>
        <span className={`cmp-fate ${fate.cls}`}>{fate.text}</span>
        <span className="faint mono small">seed {v.seed} · {usd(v.cost_usd)}{v.anomalies.length ? ` · ${v.anomalies.map((a) => ANOMALY_TR[a] || a).join(", ")}` : ""}
          {lead != null && v.score != null && v.score !== lead ? ` · ${deltaFrom(v.score, lead)}` : ""}</span>
        <div className="row">
          {v.rank === 1 ? <span className="tag ok">Kullanımda</span>
            : v.rank ? <button className="btn good" type="button" disabled={busy || frozen} onClick={promote}>Bunu kullan</button>
            : null}
          <button className="btn ghost" type="button" onClick={() => { setVar(v.id, sc.id); setPage("edit"); }}>Denetçide aç</button>
          <button className="btn ghost" type="button" onClick={onRemove} aria-label={`${idx + 1}. adayı karşılaştırmadan çıkar`}>Çıkar</button>
        </div>
      </div>
    </article>
  );
}

function Wipe({ a, b, bind, onMeta }: { a: Variation; b: Variation; bind: (id: string) => (el: HTMLVideoElement | null) => void; onMeta: () => void }) {
  const [cut, setCut] = useState(50);
  return (
    <div style={{ background: "#000", padding: "8px 0" }}>
      <div className="wipe" style={{ ["--cut" as string]: `${cut}%` }}>
        <video ref={bind(a.id)} src={a.video_url ?? undefined} poster={a.thumb_url ?? undefined} preload="auto" playsInline muted onLoadedMetadata={onMeta} />
        <video className="top" ref={bind(b.id)} src={b.video_url ?? undefined} poster={b.thumb_url ?? undefined} preload="auto" playsInline muted onLoadedMetadata={onMeta} />
        <span className="line" aria-hidden="true" />
        <span className="tagl a mono">1 · {variationTitle(a)}</span>
        <span className="tagl b mono">2 · {variationTitle(b)}</span>
      </div>
      <input className="wipe-range" type="range" min={0} max={100} step={0.5} value={cut} aria-label="Perde konumu: solda 1. aday, sağda 2. aday"
        onChange={(e) => setCut(Number(e.target.value))} />
    </div>
  );
}

export function ComparePage() {
  const { ov, scene, selectScene, compareRaw, setCompare } = usePanel();
  const [mode, setMode] = useState<Mode>("grid");
  const [solo, setSolo] = useState<number | null>(null);
  const sc = scene;
  const fromUrl = parseIds(compareRaw, sc);
  const ids = sc ? (fromUrl.length ? fromUrl : defaultSelection(sc)) : [];
  const shown = ids.map((id) => sc?.variations.find((v) => v.id === id)).filter((v): v is Variation => Boolean(v));
  const wipe = mode === "wipe" && shown.length >= 2;
  const active = wipe ? shown.slice(0, 2) : solo != null && shown[solo] ? [shown[solo]] : shown;
  const p = useSyncedPlayers(active.map((v) => v.id));

  // Klavye: Boşluk oynat/duraklat, ←/→ bir kare (Shift ile 1 sn), 1–4 o adayı tek başına büyüt, Esc ızgaraya dön.
  const keys = useRef({ p, count: shown.length, wipe });
  useEffect(() => { keys.current = { p, count: shown.length, wipe }; });
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const { p, count, wipe } = keys.current;
      const el = e.target as HTMLElement;
      if (!shortcutTarget(e)) return;
      if (e.key === " " && !el.closest("button, a")) { e.preventDefault(); if (p.playing) p.pause(); else p.play(); }
      else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        if (el.closest("[role=tablist]")) return;
        e.preventDefault();
        p.step((e.key === "ArrowLeft" ? -1 : 1) * (e.shiftKey ? 1 : FRAME_S));
      } else if (/^[1-4]$/.test(e.key) && !e.shiftKey && !wipe) {
        const i = Number(e.key) - 1;
        if (i < count) setSolo((cur) => (cur === i ? null : i));
      } else if (e.key === "Escape") setSolo(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  if (!ov || !sc) return null;
  const all = candidates(sc);
  const scores = active.map((v) => v.score);
  const leadIdx = bestIndex(scores);
  const lead = leadIdx != null ? scores[leadIdx] : null;
  const rows = measureRows(shown, ALIGN_TR);
  const set = (next: string[]) => { setSolo(null); setCompare(next); };

  return (
    <div className="page two">
      <section className="pnl" aria-labelledby="cmp-title">
        <header className="pnl-h">
          <h2 id="cmp-title">Karşılaştır</h2>
          <select aria-label="Çekim" value={sc.id} onChange={(e) => { selectScene(e.target.value); setCompare([]); setSolo(null); }}>
            {ov.scenes.map((x) => <option key={x.id} value={x.id}>{x.scene_code} · {candidates(x).length} aday</option>)}
          </select>
          <span className="meta">{all.length} izlenebilir aday · en çok {MAX_COMPARE} yan yana</span>
          <span className="grow" />
          <div className="seg" role="group" aria-label="Görünüm">
            <button type="button" aria-pressed={!wipe} onClick={() => setMode("grid")}>Izgara</button>
            <button type="button" aria-pressed={wipe} disabled={shown.length < 2} onClick={() => { setSolo(null); setMode("wipe"); }}>Perde (1 ↔ 2)</button>
          </div>
        </header>
        <div className="pnl-b" style={{ display: "flex", flexDirection: "column" }}>
          <div className="cmp-picker" role="group" aria-label="Karşılaştırılacak adaylar">
            {all.length ? all.map((v) => {
              const on = ids.includes(v.id), t = tileSpec(v);
              return (
                <button key={v.id} type="button" aria-pressed={on} data-rank={v.rank ?? undefined}
                  className={`cmp-cand${v.rank ? " refined" : ""}${t.state === "rejected" ? " rejected" : ""}`}
                  title={t.label} aria-label={`${t.label}${on ? " · seçili" : ""}`} onClick={() => set(toggle(ids, v.id))}>
                  {v.thumb_url ? <img src={v.thumb_url} alt="" loading="lazy" /> : <span className="ph" />}
                  <span>{v.tier === "probe" ? "T" : "Tam"}{v.wave_no + 1} · {v.score != null ? v.score.toFixed(3) : "—"}</span>
                </button>
              );
            }) : <p className="faint">Bu çekimde henüz izlenebilir klip yok.</p>}
          </div>
          {shown.length === 0 ? <p className="empty">Karşılaştırmak için yukarıdan aday seç.</p>
            : wipe ? <Wipe a={shown[0]} b={shown[1]} bind={p.bind} onMeta={p.onMeta} />
            : (
              <div className={`cmp-grid n${active.length}`}>
                {active.map((v) => {
                  const i = shown.indexOf(v);
                  return (
                    <Slot key={v.id} v={v} idx={i} lead={lead} sc={sc} bind={p.bind(v.id)} onMeta={p.onMeta}
                      solo={solo === i} onSolo={() => setSolo(solo === i ? null : i)} onRemove={() => set(ids.filter((x) => x !== v.id))} onPin={() => setCompare(ids)} />
                  );
                })}
              </div>
            )}
        </div>
        {shown.length > 0 && (
          <div className="transport" role="group" aria-label="Eşzamanlı oynatma" style={{ gridTemplateColumns: "auto auto minmax(80px, 1fr) auto auto" }}>
            <div className="btns">
              <button className="btn ghost icon-btn" type="button" onClick={() => p.step(-FRAME_S)} aria-label="Bir kare geri"><Icon.stepBack /></button>
              <button className="btn icon-btn" type="button" onClick={p.playing ? p.pause : p.play} aria-label={p.playing ? "Duraklat" : "Oynat"}>{p.playing ? <Icon.pause /> : <Icon.play />}</button>
              <button className="btn ghost icon-btn" type="button" onClick={() => p.step(FRAME_S)} aria-label="Bir kare ileri"><Icon.stepFwd /></button>
              <button className="btn ghost icon-btn" type="button" aria-label="Döngü" aria-pressed={p.loop} onClick={() => p.setLoop(!p.loop)}><Icon.loop /></button>
            </div>
            <span className="tc time">{timecode(p.time)}<small>/ {timecode(p.duration)}</small></span>
            <input className="scrub" type="range" min={0} max={p.duration || 0} step={FRAME_S} value={Math.min(p.time, p.duration || 0)}
              aria-label="Konum" onChange={(e) => { if (p.playing) p.pause(); p.seek(Number(e.target.value)); }} />
            <label className="check">Hız
              <select value={p.speed} onChange={(e) => p.setSpeed(Number(e.target.value))}>
                {SPEEDS.map((x) => <option key={x} value={x}>{String(x).replace(".", ",")}×</option>)}
              </select>
            </label>
            <span className="faint tiny"><kbd>Boşluk</kbd> <kbd>←</kbd><kbd>→</kbd>{!wipe && <> <kbd>1</kbd>–<kbd>{Math.min(4, shown.length)}</kbd> <kbd>Esc</kbd></>}</span>
          </div>
        )}
      </section>
      <Panel title="Ölçümler" meta={shown.length ? `${shown.length} aday` : ""}>
        {shown.length > 0 && (
          <table className="tbl">
            <thead><tr><th>Ölçüm</th>{shown.map((v, i) => <th key={v.id} className="r mono">{i + 1}{v.rank ? ` · #${v.rank}` : ""}</th>)}</tr></thead>
            <tbody>
              {rows.map((r) => {
                const top = r.best != null ? r.values[r.best] : null;
                return (
                  <tr key={r.key}>
                    <td>{r.label}</td>
                    {r.values.map((x, i) => (
                      <td key={i} className={`r mono${r.best === i ? " best" : ""}`}>
                        {x != null ? x.toFixed(3).replace(".", ",") : "—"}
                        {r.best != null && r.best !== i && x != null && <small>{deltaFrom(x, top)}</small>}
                      </td>
                    ))}
                  </tr>
                );
              })}
              <tr><td>Anomaliler</td>{shown.map((v) => <td key={v.id} className="r small">{v.anomalies.map((a) => ANOMALY_TR[a] || a).join(", ") || "—"}</td>)}</tr>
              <tr><td>Maliyet</td>{shown.map((v) => <td key={v.id} className="r mono">{usd(v.cost_usd)}</td>)}</tr>
              <tr><td>Seed</td>{shown.map((v) => <td key={v.id} className="r mono small">{v.seed}</td>)}</tr>
            </tbody>
          </table>
        )}
        <div className="pad stack">
          {rows.length === 1 && shown.length > 0 && (
            <p className="faint small">Ayrıntılı ölçüm (stil, tarif ve yüz uyumu) gerçek kalite skorlayıcısıyla gelir; ücretsiz modda yalnızca birleşik skor vardır.</p>
          )}
          <p className="faint small">Rafine havuzdaki bir aday buradan seçilebilir; elenenler yalnızca incelenir. Kurgu sayfasında aday şeridinde <kbd>Shift</kbd>+tık ile de aday eklenir.</p>
          <NoteBox id="compare" />
        </div>
      </Panel>
    </div>
  );
}
