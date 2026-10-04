"use client";
import { api } from "@/lib/api";
import { BOARD_TR, MOVE_TR, SHOT_TR } from "@/lib/i18n";
import { usePanel } from "@/lib/panel";
import { passText } from "@/lib/text";
import type { DispatchAll, Scene } from "@/lib/types";
import { groupBySequence } from "./ScenesPane";
import { NoteBox, useSynced } from "./ui";

function BoardCard({ sc }: { sc: Scene }) {
  const { busy, act, note } = usePanel();
  const [text, setText] = useSynced(sc.board_note || "", `${sc.id}:${sc.board_note ?? ""}`);   // yazılan not yoklamayla ezilmez
  const cam = sc.camera || {};
  const lines = sc.tracks.filter((t) => t.kind === "dialogue");
  const decide = (status: string) => act(async () => {
    const r = await api.post<{ board_status: string; board_note: string | null }>(`/scenes/${sc.id}/board`, { status, note: text });
    note("board", `${BOARD_TR[r.board_status]}${r.board_note ? ": " + r.board_note : ""}`, r.board_status === "changes_requested" ? "err" : "ok");
  }, "board");
  return (
    <article className="board">
      {sc.board_url ? <img src={sc.board_url} alt={`${sc.scene_code} storyboard karesi`} loading="lazy" /> : <div className="ph">Kare yok</div>}
      <div className="card-head">
        <h3>{sc.scene_code}</h3>
        <span className="muted">{SHOT_TR[cam.shot ?? ""] || cam.shot || ""}, {MOVE_TR[cam.movement ?? ""] || cam.movement || ""} · {Number(sc.duration_s).toFixed(1)} sn</span>
        <span className="spacer" />
        <b className={`bstatus ${sc.board_status}`}>{BOARD_TR[sc.board_status] || sc.board_status}</b>
      </div>
      <p className="muted small">
        {lines.length ? lines.map((t, i) => <span key={t.id}>{i > 0 && <br />}{t.character || "Anlatıcı"}: {t.text}</span>) : "Diyalog yok."}
      </p>
      <label className="field">Not<textarea maxLength={2000} value={text} onChange={(e) => setText(e.target.value)} /></label>
      <div className="actions-row">
        <button className="btn small good" type="button" disabled={busy || !sc.board_url} onClick={() => decide("approved")}>Onayla</button>
        <button className="btn small danger" type="button" disabled={busy} onClick={() => decide("changes_requested")}>Değişiklik iste</button>
        {sc.board_status !== "pending" && <button className="btn small" type="button" disabled={busy} onClick={() => decide("pending")}>Geri al</button>}
      </div>
    </article>
  );
}

export function BoardPane() {
  const { ov, projectId, busy, frozen, act, note } = usePanel();
  if (!ov) return null;
  const scenes = ov.scenes;
  const approved = scenes.filter((x) => x.board_status === "approved").length;
  const flagged = scenes.filter((x) => x.upgrade_requested).length;
  const drafts = scenes.filter((x) => x.cut_tier === "draft").length;
  const required = Boolean(ov.project.settings.require_board_approval);
  return (
    <section className="panel" aria-labelledby="board-title">
      <header>
        <h2 id="board-title">Storyboard</h2>
        <span className="muted">{approved}/{scenes.length} kare onaylı · {drafts} taslak çekim · {flagged} iyileştirilecek</span>
      </header>
      <div className="body">
        <p className="muted">
          Revizyonu en ucuz aşamada yap: her çekimin karesini onayla ya da not bırakıp değişiklik iste. Sonra taslak geçiş yalnızca
          test turuyla kaba kurguyu çıkarır; zayıf çekimleri &quot;İyileştir&quot; ile işaretleyip yalnızca onları tam turla üretirsin.
        </p>
        <div className="toolbar mt12">
          <button className="btn" type="button" disabled={busy || frozen} onClick={() => act(async () => {
            const r = await api.post<{ queued: string[] }>(`/projects/${projectId}/storyboard`);
            note("board", r.queued.length ? `Kareler hazırlanıyor: ${r.queued.join(", ")}.` : "Bütün kareler güncel.", "ok");
          }, "board")}>Kareleri hazırla</button>
          <a className="btn" href={`/api/projects/${projectId}/storyboard.pdf`} download="storyboard.pdf">PDF indir</a>
          <label className="check">
            <input type="checkbox" checked={required} disabled={busy} onChange={(e) => {
              const on = e.target.checked;
              void act(async () => {
                await api.patch(`/projects/${projectId}`, { settings: { require_board_approval: on } });
                note("board", on ? "Onaylanmamış karesi olan çekimde görüntü üretilmez." : "Kare onayı artık zorunlu değil.", "ok");
              }, "board");
            }} /> Görüntü üretimi için kare onayı zorunlu
          </label>
          <span className="spacer" />
          <button className="btn primary" type="button" disabled={busy || frozen} onClick={() => act(async () => {
            const r = await api.post<DispatchAll>(`/projects/${projectId}/draft-pass`);
            note("board", passText(r, "Taslak geçiş") + " Son çekim bitince kaba kurgu kendiliğinden birleşir.", "ok");
          }, "board")}>Taslak geçişi başlat</button>
          <button className="btn" type="button" disabled={busy || frozen || !flagged} onClick={() => act(async () => {
            const r = await api.post<DispatchAll>(`/projects/${projectId}/upgrade-pass`);
            note("board", passText(r, "Tam tura alınan"), "ok");
          }, "board")}>{flagged ? `İyileştirme geçişi (${flagged} çekim)` : "İyileştirme geçişi"}</button>
        </div>
        <NoteBox id="board" className="mt12" />
        {groupBySequence(scenes, ov.sequences).map((g) => (
          <section key={g.id}>
            <h3 className="seq-title">{g.q ? `${g.q.code} · ${g.q.title}` : "Sekanssız"}</h3>
            <div className="boards">{g.scenes.map((sc) => <BoardCard key={sc.id} sc={sc} />)}</div>
          </section>
        ))}
      </div>
    </section>
  );
}
