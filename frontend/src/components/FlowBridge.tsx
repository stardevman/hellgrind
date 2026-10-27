"use client";
/* Google Flow köprüsü: Flow'un API'si yok. Panel çekim başına paket verir (istem, kareler, malzemeler, kredi tahmini);
   yönetmen klipleri Flow'da üretir, dosya adına çekim kodunu koyar ve burada toplu yükler. Her klip dış çekim olarak
   girer: görüntü üretilmez, anomali kontrolü, ses, renk ve montaj çalışır. Hiçbir ücretli servis çağrılmaz. */
import { useState } from "react";
import { api } from "@/lib/api";
import { FLOW_QUALITY, matchClips, type FlowQuality } from "@/lib/flow";
import { usePanel } from "@/lib/panel";
import type { Scene, StageResult } from "@/lib/types";

export function FlowBridge({ scenes }: { scenes: Scene[] }) {
  const { projectId, busy, frozen, act, note } = usePanel();
  const [quality, setQuality] = useState<FlowQuality>("fast");
  const [redo, setRedo] = useState(false);
  const unfinished = scenes.filter((x) => x.status !== "done").length;
  const all = redo || !unfinished;                   // bitmiş proje Flow ile yeniden üretilirken bütün çekimler
  const open = all ? scenes.length : unfinished;
  const q = FLOW_QUALITY[quality];

  const upload = (files: File[]) => act(async () => {
    const m = matchClips(files, scenes);
    const done: string[] = [];
    const failed: string[] = [];
    for (const { file, code, sceneId } of m.matched) {
      note("flow", `Yükleniyor: ${code} (${done.length + failed.length + 1}/${m.matched.length})`);
      try {
        await api.upload<StageResult>(`/scenes/${sceneId}/external?filename=${encodeURIComponent(file.name)}`, file);
        done.push(code);
      } catch (e) {
        failed.push(`${code}: ${(e as Error).message}`);
      }
    }
    const lines = [
      done.length ? `${done.length} klip dış çekim olarak yüklendi: ${done.join(", ")}. Ses ve montaj kuyruğa girdi.` : "Hiçbir klip yüklenmedi.",
      failed.length ? `Yüklenemeyen: ${failed.join("; ")}.` : "",
      m.unknown.length ? `Çekim kodu bulunamadı (dosya adına ör. S001_SH01 yaz): ${m.unknown.join(", ")}.` : "",
      m.duplicate.length ? `Aynı çekime ikinci dosya, atlandı: ${m.duplicate.join(", ")}.` : "",
    ].filter(Boolean);
    note("flow", lines.join(" "), failed.length || !done.length ? "err" : "ok");
  }, "flow");

  return (
    <div className="form-row" aria-label="Google Flow köprüsü">
      <b>Google Flow</b>
      <label className="field">Model
        <select value={quality} disabled={busy} onChange={(e) => setQuality(e.target.value as FlowQuality)}>
          {Object.entries(FLOW_QUALITY).map(([k, v]) => <option key={k} value={k}>{v.label} · {v.credits} kredi</option>)}
        </select>
      </label>
      <a className="btn" href={`/api/projects/${projectId}/flow.zip?quality=${quality}&scope=${all ? "all" : "open"}`} aria-disabled={!open}
        title="Her çekim için istem, başlangıç/bitiş kareleri, malzeme görselleri ve BENIOKU.txt">Flow paketi indir</a>
      {unfinished > 0 && unfinished < scenes.length && (
        <label className="check"><input type="checkbox" checked={redo} onChange={(e) => setRedo(e.target.checked)} /> Bitmiş çekimler de</label>
      )}
      <label className="btn" aria-disabled={busy || frozen} title="Flow'dan indirdiğin klipleri seç; dosya adındaki çekim koduyla (ör. S001_SH01.mp4) eşlenir">
        Flow kliplerini yükle
        <input type="file" accept="video/mp4,video/quicktime,video/webm,.mp4,.mov,.webm" multiple hidden disabled={busy || frozen}
          onChange={(e) => { const f = Array.from(e.currentTarget.files ?? []); e.currentTarget.value = ""; if (f.length) void upload(f); }} />
      </label>
      <span className="faint small">{open} {all ? "çekim" : "bitmemiş çekim"} · tahmini {open * q.credits} kredi (çıktı başına). Flow kredisi abonelikten düşer, bütçeye yazılmaz.</span>
    </div>
  );
}
