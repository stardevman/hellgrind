"use client";
/* Ses sayfası (ses-önce akışı): sektörde diyalog animasyondan önce kaydedilir; çekim süresi ve tempo sesten çıkar. Solda
   replikler sekans ve çekim başlıkları altında (karakter, metin, kaynak rozeti, dalga formu, oynat, ölçülen süre ile
   çekimdeki yeri; fark 0,5 sn'yi aşarsa amber uyarı). Sağda toplu yükleme: S001_SH01_2.wav biçimindeki dosyalar repliğe
   kendiliğinden eşlenir, eşlenemeyenler elle atanır. Sekansın sesi kilitlenince replik düzenleme kapanır ve görüntüsü
   üretilmemiş çekimlerin süresi sesten hesaplanır; kilit ve kilidin açılması onay ister. Planlayıcı değişmez: görüntüsü
   üretilmiş çekimde süre farkı yalnızca uyarıdır. */
import { useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import useSWR from "swr";
import { api, fetcher } from "@/lib/api";
import { POLL_MS, usePanel } from "@/lib/panel";
import { clock } from "@/lib/text";
import type { VoiceLine, VoiceLockResult, VoiceSequence, VoiceShot, VoiceState, VoiceUpload } from "@/lib/types";
import { assignable, DRIFT_TEXT, matchFiles, secTr, sourceBadge, uploadBody, type Matched } from "@/lib/voice";
import { Icon } from "./icons";
import { NoteBox, Panel, Section } from "./ui";

/** Repliğin dalga formu (sunucunun tepe değerleri); seslendirilmemişse boş çizgi. */
function Wave({ line }: { line: VoiceLine }) {
  const { data } = useSWR<{ peaks: number[] }>(line.url ? `/audio-tracks/${line.track_id}/peaks?f=${encodeURIComponent(line.url)}` : null,
    fetcher, { revalidateOnFocus: false });
  const peaks = data?.peaks ?? [];
  return (
    <svg className={`wave${line.final ? " final" : ""}`} viewBox={`0 0 ${Math.max(1, peaks.length)} 20`} preserveAspectRatio="none"
      role="img" aria-label={line.url ? `${line.character || "Anlatıcı"} repliğinin dalga formu` : "Ses yok"}>
      {peaks.length ? peaks.map((p, i) => <rect key={i} x={i + 0.15} width={0.7} y={10 - Math.max(0.4, p * 9.5)} height={Math.max(0.8, p * 19)} />)
        : <line x1="0" y1="10" x2="1" y2="10" />}
    </svg>
  );
}

function LineRow({ line, locked, playing, onPlay, onUpload }: {
  line: VoiceLine; locked: boolean; playing: boolean; onPlay: () => void; onUpload: (file: File) => void;
}) {
  const { busy } = usePanel();
  const badge = sourceBadge(line);
  return (
    <li className={`vl-row${line.drift ? " drift" : ""}`}>
      <button type="button" className="btn ghost icon-only" disabled={!line.url} onClick={onPlay}
        aria-label={playing ? "Durdur" : `${line.character || "Anlatıcı"} repliğini oynat`} title={line.url ? "Oynat" : "Henüz ses yok"}>
        {playing ? <Icon.pause /> : <Icon.play />}
      </button>
      <span className="vl-who">{line.character || "Anlatıcı"}</span>
      <span className="vl-text">{line.text}</span>
      <span className={`tag ${badge.tone}`} title={badge.title}>{badge.text}</span>
      <Wave line={line} />
      <span className={`vl-dur mono${line.drift ? " warn" : ""}`} title={line.drift ? DRIFT_TEXT : "Ölçülen ses süresi / repliğin çekimdeki yeri"}>
        {secTr(line.duration_s)} <span className="faint">/ {secTr(line.slot_s)}</span>
      </span>
      <label className={`btn ghost vl-up${locked ? " disabled" : ""}`} title={locked ? "Sekansın sesi kilitli" : "Bu repliğin final kaydını yükle"}>
        Kayıt yükle
        <input type="file" accept=".wav,.mp3,.m4a,.aac,.flac,.ogg,audio/*" disabled={locked || busy} hidden
          onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; if (f) onUpload(f); }} />
      </label>
      {line.drift && <span className="vl-warn" role="note">{DRIFT_TEXT}</span>}
    </li>
  );
}

function ShotBlock({ shot, locked, playing, play, upload }: {
  shot: VoiceShot; locked: boolean; playing: string | null; play: (l: VoiceLine) => void; upload: (l: VoiceLine, shot: VoiceShot, f: File) => void;
}) {
  const { selectScene, setPage } = usePanel();
  return (
    <div className="vs">
      <div className={`vs-h${shot.drift ? " drift" : ""}`}>
        <button type="button" className="mono vs-code" onClick={() => { selectScene(shot.scene_id); setPage("edit"); }} title="Kurguda aç">{shot.scene_code}</button>
        <span className="faint small vs-head">{shot.heading}</span>
        <span className="grow" />
        <span className="mono small" title="Çekim süresi">çekim {secTr(shot.duration_s)}</span>
        {shot.duration_source === "voice" && <span className="tag info" title="Süre sekansın ses kilidinde ölçülen sesten hesaplandı">sesten</span>}
        {shot.voice_s != null && <span className="mono small faint" title="Giriş payı + replikler + çıkış payı">ses {secTr(shot.voice_s)}</span>}
        {shot.has_video && <span className="tag" title="Görüntüsü üretildi: süre sesten yeniden hesaplanmaz">görüntü var</span>}
      </div>
      {shot.drift && <p className="vs-warn" role="note">{shot.has_video ? shot.drift_text : `Kilitlenince çekim süresi sesten hesaplanır (${secTr(shot.voice_clamped)}).`}</p>}
      {shot.lines.length ? (
        <ol className="vl">{shot.lines.map((l) => (
          <LineRow key={l.track_id} line={l} locked={locked} playing={playing === l.track_id} onPlay={() => play(l)} onUpload={(f) => upload(l, shot, f)} />
        ))}</ol>
      ) : <p className="faint small vs-none">Diyalog yok.</p>}
    </div>
  );
}

function LockDialog({ seq, onCancel, onConfirm }: { seq: VoiceSequence; onCancel: () => void; onConfirm: () => void }) {
  const cancel = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    cancel.current?.focus();
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") onCancel(); };
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [onCancel]);
  const fresh = seq.shots.filter((x) => !x.has_video && x.lines.length).length;
  const rendered = seq.shots.filter((x) => x.has_video && x.lines.length).length;
  return (
    <div className="modal-back" onMouseDown={(e) => { if (e.target === e.currentTarget) onCancel(); }}>
      <div className="modal" role="alertdialog" aria-modal="true" aria-labelledby="vlock-t" aria-describedby="vlock-d">
        <h2 id="vlock-t"><Icon.lock />{seq.locked ? `${seq.code} ses kilidi açılsın mı?` : `${seq.code} sesi kilitlensin mi?`}</h2>
        <p id="vlock-d" className="faint small">
          {seq.locked
            ? "Replikler yeniden düzenlenebilir ve yüklenebilir. Çekim süreleri ve replik zamanlaması olduğu gibi kalır."
            : `Replik düzenleme kapanır. Görüntüsü henüz üretilmemiş ${fresh} çekimin süresi ve replik zamanlaması ölçülen sesten hesaplanır (storyboard karesi çizilmişse yeniden çizilir).${rendered ? ` Görüntüsü üretilmiş ${rendered} çekim değişmez; süre farkı varsa yalnızca uyarı kalır.` : ""}`}
        </p>
        <div className="row">
          <span className="grow" />
          <button ref={cancel} type="button" className="btn ghost" onClick={onCancel}>Vazgeç</button>
          <button type="button" className="btn primary" onClick={onConfirm}>{seq.locked ? "Kilidi aç" : "Sesi kilitle"}</button>
        </div>
      </div>
    </div>
  );
}

/** Toplu yükleme: sürükle-bırak ya da dosya seç; adı biçime uyanlar eşlenir, kalanlar elle atanır. */
function BulkUpload({ state, send }: { state: VoiceState; send: (items: Matched[]) => Promise<void> }) {
  const { busy } = usePanel();
  const [files, setFiles] = useState<File[]>([]);
  const [over, setOver] = useState(false);
  const [manual, setManual] = useState<Record<number, string>>({});
  const { matched, unmatched } = useMemo(() => matchFiles(files, state), [files, state]);
  const options = useMemo(() => assignable(state), [state]);
  const add = (list: FileList | null) => { if (list?.length) { setFiles((f) => [...f, ...Array.from(list)]); } };
  const drop = (e: DragEvent) => { e.preventDefault(); setOver(false); add(e.dataTransfer.files); };
  const assigned: Matched[] = unmatched.flatMap((u, i) => {
    const o = options.find((x) => x.value === manual[i]);
    return o ? [{ file: u.file, line: o.line, shot: o.shot }] : [];
  });
  const go = async (items: Matched[]) => {
    await send(items);
    setFiles((f) => f.filter((file) => !items.some((m) => m.file === file)));
    setManual({});
  };
  return (
    <div className="stack">
      <label className={`drop${over ? " over" : ""}`} data-tour="voice-upload"
        onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)} onDrop={drop}>
        <Icon.voice />
        <b>Kayıtları buraya sürükle</b>
        <span className="faint small">ya da tıklayıp seç · birden çok dosya · S001_SH01_2.wav biçimi repliğe kendiliğinden eşlenir</span>
        <input type="file" multiple accept=".wav,.mp3,.m4a,.aac,.flac,.ogg,audio/*" hidden onChange={(e) => { add(e.target.files); e.target.value = ""; }} />
      </label>
      {matched.length > 0 && (
        <div className="stack">
          <h3 className="small">Eşlenen ({matched.length})</h3>
          <ul className="vu-list">{matched.map((m) => (
            <li key={m.file.name + m.line.track_id}><span className="mono">{m.file.name}</span><Icon.chevron />
              <span><b className="mono">{m.shot.scene_code} · {m.line.n}</b> {m.line.character || "Anlatıcı"}: {m.line.text}</span></li>
          ))}</ul>
          <button type="button" className="btn primary" disabled={busy} onClick={() => void go(matched)}>Eşlenenleri yükle ({matched.length})</button>
        </div>
      )}
      {unmatched.length > 0 && (
        <div className="stack">
          <h3 className="small">Eşlenemeyen ({unmatched.length})</h3>
          <ul className="vu-list">{unmatched.map((u, i) => (
            <li key={u.file.name + i} className="col">
              <span><span className="mono">{u.file.name}</span> <span className="faint small">· {u.reason}</span></span>
              <select aria-label={`${u.file.name} hangi repliğe`} value={manual[i] ?? ""} onChange={(e) => setManual({ ...manual, [i]: e.target.value })}>
                <option value="">Replik seç…</option>
                {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </li>
          ))}</ul>
          <div className="row">
            <button type="button" className="btn" disabled={busy || !assigned.length} onClick={() => void go(assigned)}>Atananları yükle ({assigned.length})</button>
            <button type="button" className="btn ghost" onClick={() => { setFiles((f) => f.filter((file) => !unmatched.some((u) => u.file === file))); setManual({}); }}>Listeyi temizle</button>
          </div>
        </div>
      )}
    </div>
  );
}

export function VoicePage() {
  const { ov, projectId, busy, frozen, act, note } = usePanel();
  const { data: st, mutate } = useSWR<VoiceState>(projectId ? `/projects/${projectId}/voice` : null, fetcher, { refreshInterval: POLL_MS });
  const [playing, setPlaying] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<VoiceSequence | null>(null);
  const audio = useRef<HTMLAudioElement | null>(null);
  useEffect(() => () => audio.current?.pause(), []);
  if (!st || !ov) return <p className="loading">Yükleniyor…</p>;

  const play = (line: VoiceLine) => {
    if (!line.url) return;
    const a = audio.current ?? (audio.current = new Audio());
    if (playing === line.track_id) { a.pause(); setPlaying(null); return; }
    a.src = line.url;
    a.onended = () => setPlaying(null);
    void a.play().then(() => setPlaying(line.track_id)).catch(() => setPlaying(null));
  };
  const send = (items: Matched[]) => act(async () => {
    const done: string[] = [];
    for (const m of items) {
      const up = await api.upload<VoiceUpload>(`/projects/${projectId}/voice/upload?filename=${encodeURIComponent(m.file.name)}`, m.file);
      await api.put(`/scenes/${m.shot.scene_id}/audio`, uploadBody(m.line, up.key, up.filename));
      done.push(`${m.shot.scene_code} · ${m.line.n}`);
    }
    await mutate();
    note("voice", `Final kayıt yüklendi: ${done.join(", ")}. Görüntü yeniden üretilmez; üretilmiş çekimlerde yalnızca ses ve montaj çalışır.`, "ok");
  }, "voice").then(() => undefined);
  const lockSeq = (q: VoiceSequence) => act(async () => {
    const r = await api.post<VoiceLockResult>(`/sequences/${q.id}/voice-lock`, { locked: !q.locked });
    await mutate();
    note("voice", r.locked
      ? `${r.code} sesi kilitlendi.${r.retimed?.length ? ` Süresi sesten hesaplanan: ${r.retimed.join(", ")}.` : ""}${r.drift?.length ? ` Görüntüsü üretilmiş ve süresi farklı (uyarı): ${r.drift.join(", ")}.` : ""}${r.clamped?.length ? ` 2–10 sn sınırına çekilen: ${r.clamped.join(", ")}.` : ""}`
      : `${r.code} ses kilidi açıldı; replikler düzenlenebilir.`, "ok");
  }, "voice");
  const c = st.counts;
  return (
    <div className="page voice" data-tour="voice">
      <Panel title="Replikler" meta={`${c.lines} replik · ${c.final} final · ${c.temp} geçici${c.drift ? ` · ${c.drift} süre farkı` : ""}`}
        tools={<button type="button" className="btn" disabled={busy || frozen || c.unmeasured === 0} onClick={() => act(async () => {
          await api.post(`/projects/${projectId}/voice/temp`);
          note("voice", "Geçici sesler kuyruğa alındı (ücretsiz modda espeak); bitince süreler ölçülür.", "ok");
        }, "voice")} title="Seslendirilmemiş repliklerin geçici sesini üretir">{c.unmeasured ? `Geçici sesleri üret (${c.unmeasured})` : "Geçici sesler hazır"}</button>}>
        <div className="pad"><NoteBox id="voice" /></div>
        {st.sequences.map((q) => (
          <section key={q.id ?? "none"} className="vq">
            <header className="vq-h">
              <h3>{q.code ? `${q.code} · ${q.title}` : q.title}</h3>
              {q.locked && <span className="tag ok" title={q.locked_at ? `Kilit: ${clock(q.locked_at)}` : ""}><Icon.lock />Kilitli</span>}
              <span className="faint small">{q.final}/{q.lines} final</span>
              <span className="grow" />
              {q.id && (
                <button type="button" className={`btn${q.locked ? " ghost" : ""}`} disabled={busy} data-tour="voice-lock"
                  onClick={() => setConfirm(q)}>{q.locked ? "Kilidi aç" : "Sesi kilitle"}</button>
              )}
            </header>
            {q.shots.map((shot) => (
              <ShotBlock key={shot.scene_id} shot={shot} locked={q.locked} playing={playing} play={play}
                upload={(line, sh, file) => void send([{ file, line, shot: sh }])} />
            ))}
          </section>
        ))}
      </Panel>
      <Panel title="Toplu yükleme" meta={st.voice_first ? "ses-önce açık" : ""}>
        <div className="pad stack">
          <BulkUpload state={st} send={send} />
          <p className="faint small">
            Ölçülen süre ile repliğin çekimdeki yeri karşılaştırılır; fark {String(st.pads.drift).replace(".", ",")} saniyeyi aşarsa
            amber uyarı çıkar. Kilitte çekim süresi: {String(st.pads.in).replace(".", ",")} sn giriş payı + replikler (aralarında {String(st.pads.gap).replace(".", ",")} sn)
            + {String(st.pads.out).replace(".", ",")} sn çıkış payı. Görüntüsü üretilmiş çekim yeniden üretilmez.
          </p>
        </div>
        <div className="adv"><Section title="Ses-önce ayarı" meta={st.voice_first ? "açık" : "kapalı"} open={false}>
          <div className="stack">
            <label className="check">
              <input type="checkbox" checked={st.voice_first} disabled={busy} onChange={(e) => {
                const on = e.target.checked;
                void act(async () => {
                  await api.patch(`/projects/${projectId}`, { settings: { voice_first: on } });
                  await mutate();
                  note("voice", on ? "Ses-önce açık: sesi kilitlenmemiş sekansta görüntü üretilmez." : "Ses-önce kapalı: görüntü ses kilidini beklemez.", "ok");
                }, "voice");
              }} /> Ses kilitlenmeden görüntü üretme (voice_first)
            </label>
            <p className="faint small">Açıkken Yapılacaklar listesi final sesi bekleyen replikleri ve kilitlenmemiş sekansları gösterir.</p>
          </div>
        </Section></div>
      </Panel>
      {confirm && <LockDialog seq={confirm} onCancel={() => setConfirm(null)} onConfirm={() => { const q = confirm; setConfirm(null); void lockSeq(q); }} />}
    </div>
  );
}
