"use client";
/* Storyboard sayfası (storyboard yazılımları gibi): sekans sekans kare ızgarası; her karede kamera, süre, diyalog,
   yönetmen notu ve onay. Üst araç çubuğu kareleri hazırlar, PDF verir, taslak ve iyileştirme geçişlerini başlatır. */
import { api } from "@/lib/api";
import { BOARD_TR, MOVE_TR, SHOT_TR } from "@/lib/i18n";
import { usePanel } from "@/lib/panel";
import { passText } from "@/lib/text";
import type { DispatchAll, Scene } from "@/lib/types";
import { NoteBox, useSynced } from "./ui";

const TONE: Record<string, string> = { approved: "ok", changes_requested: "bad", pending: "" };

function BoardCard({ sc }: { sc: Scene }) {
  const { busy, act, note, selectScene, setPage } = usePanel();
  const [text, setText] = useSynced(sc.board_note || "", `${sc.id}:${sc.board_note ?? ""}`);   // yazılan not yoklamayla ezilmez
  const cam = sc.camera || {};
  const lines = sc.tracks.filter((t) => t.kind === "dialogue");
  const decide = (status: string) => act(async () => {
    const r = await api.post<{ board_status: string; board_note: string | null }>(`/scenes/${sc.id}/board`, { status, note: text });
    note("board", `${sc.scene_code}: ${BOARD_TR[r.board_status]}${r.board_note ? ": " + r.board_note : ""}`, r.board_status === "changes_requested" ? "err" : "ok");
  }, "board");
  return (
    <article className={`board ${sc.board_status}`} aria-label={`${sc.scene_code} storyboard karesi`}>
      <div className="frame">
        {sc.board_url ? <img src={sc.board_url} alt={`${sc.scene_code} storyboard karesi`} loading="lazy" /> : <div className="ph">Kare yok</div>}
        <span className="code">{sc.scene_code}</span>
        <button className="open" type="button" onClick={() => { selectScene(sc.id); setPage("edit"); }}>Kurguda aç</button>
      </div>
      <div className="row">
        <span className="dim small grow">{SHOT_TR[cam.shot ?? ""] || cam.shot || ""}, {MOVE_TR[cam.movement ?? ""] || cam.movement || ""} · <span className="mono">{Number(sc.duration_s).toFixed(1)} sn</span></span>
        <span className={`st ${TONE[sc.board_status] ?? ""}`}>{BOARD_TR[sc.board_status] || sc.board_status}</span>
      </div>
      <p className="small">{sc.description}</p>
      <p className="dim small">
        {lines.length ? lines.map((t, i) => <span key={t.id}>{i > 0 && <br />}<b>{t.character || "Anlatıcı"}:</b> {t.text}</span>) : "Diyalog yok."}
      </p>
      <textarea aria-label={`${sc.scene_code} yönetmen notu`} placeholder="Yönetmen notu" maxLength={2000} value={text} onChange={(e) => setText(e.target.value)} />
      <div className="row">
        <button className="btn good" type="button" disabled={busy || !sc.board_url} onClick={() => decide("approved")}>Onayla</button>
        <button className="btn danger" type="button" disabled={busy} onClick={() => decide("changes_requested")}>Değişiklik iste</button>
        {sc.board_status !== "pending" && <button className="btn ghost" type="button" disabled={busy} onClick={() => decide("pending")}>Geri al</button>}
      </div>
    </article>
  );
}

export function BoardPage() {
  const { ov, projectId, busy, frozen, act, note } = usePanel();
  if (!ov) return null;
  const scenes = ov.scenes;
  const approved = scenes.filter((x) => x.board_status === "approved").length;
  const flagged = scenes.filter((x) => x.upgrade_requested).length;
  const drafts = scenes.filter((x) => x.cut_tier === "draft").length;
  const required = Boolean(ov.project.settings.require_board_approval);
  const groups = ov.sequences.map((q) => ({ id: q.id, title: `${q.code} · ${q.title}`, scenes: scenes.filter((s) => s.sequence_id === q.id) }));
  const loose = scenes.filter((s) => !ov.sequences.some((q) => q.id === s.sequence_id));
  if (loose.length) groups.push({ id: "none", title: "Sekanssız", scenes: loose });
  return (
    <div className="page">
      <section className="pnl" aria-labelledby="board-title">
        <header className="pnl-h">
          <h2 id="board-title">Storyboard</h2>
          <span className="meta">{approved}/{scenes.length} kare onaylı · {drafts} taslak çekim · {flagged} iyileştirilecek</span>
          <span className="grow" />
          <button className="btn" type="button" disabled={busy || frozen} onClick={() => act(async () => {
            const r = await api.post<{ queued: string[] }>(`/projects/${projectId}/storyboard`);
            note("board", r.queued.length ? `Kareler hazırlanıyor: ${r.queued.join(", ")}.` : "Bütün kareler güncel.", "ok");
          }, "board")}>Kareleri hazırla</button>
          <a className="btn" href={`/api/projects/${projectId}/storyboard.pdf`} download="storyboard.pdf">PDF indir</a>
          <span className="vsep" />
          <label className="check">
            <input type="checkbox" checked={required} disabled={busy} onChange={(e) => {
              const on = e.target.checked;
              void act(async () => {
                await api.patch(`/projects/${projectId}`, { settings: { require_board_approval: on } });
                note("board", on ? "Onaylanmamış karesi olan çekimde görüntü üretilmez." : "Kare onayı artık zorunlu değil.", "ok");
              }, "board");
            }} /> Görüntü üretimi için kare onayı zorunlu
          </label>
          <span className="vsep" />
          <button className="btn primary" type="button" disabled={busy || frozen} onClick={() => act(async () => {
            const r = await api.post<DispatchAll>(`/projects/${projectId}/draft-pass`);
            note("board", passText(r, "Taslak geçiş") + " Son çekim bitince kaba kurgu kendiliğinden birleşir.", "ok");
          }, "board")}>Taslak geçişi başlat</button>
          <button className="btn" type="button" disabled={busy || frozen || !flagged} onClick={() => act(async () => {
            const r = await api.post<DispatchAll>(`/projects/${projectId}/upgrade-pass`);
            note("board", passText(r, "Tam tura alınan"), "ok");
          }, "board")}>{flagged ? `İyileştirme geçişi (${flagged} çekim)` : "İyileştirme geçişi"}</button>
        </header>
        <div className="pnl-b">
          <div className="pad stack">
            <p className="faint small">
              Revizyonu en ucuz aşamada yap: her çekimin karesini onayla ya da not bırakıp değişiklik iste. Taslak geçiş yalnızca test
              turuyla kaba kurguyu çıkarır; zayıf çekimleri Kurgu sayfasında &quot;İyileştir&quot; ile işaretleyip yalnızca onları tam turla üretirsin.
            </p>
            <NoteBox id="board" />
          </div>
          {groups.filter((g) => g.scenes.length).map((g) => (
            <section key={g.id}>
              <h3 className="board-seq">{g.title}</h3>
              <div className="boards">{g.scenes.map((sc) => <BoardCard key={sc.id} sc={sc} />)}</div>
            </section>
          ))}
        </div>
      </section>
    </div>
  );
}
