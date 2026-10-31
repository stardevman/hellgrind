"use client";
/* Çekimin sağ tık menüsü (çekim havuzu ve zaman çizelgesi): denetçiye gitmeden sık kullanılan eylemler. Klavyeyle de
   açılır (menü tuşu ya da Shift+F10); oklar gezer, Esc kapatır. X kısayolu seçili çekimi filmden çıkarır ya da geri alır. */
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type MouseEvent as ReactMouseEvent } from "react";
import { api } from "@/lib/api";
import { usePanel, type Page } from "@/lib/panel";
import { resultText } from "@/lib/text";
import type { Scene, StageResult } from "@/lib/types";
import { useDismiss } from "./ui";

type Act = ReturnType<typeof usePanel>["act"];
type Note = ReturnType<typeof usePanel>["note"];

/** Çekimi filmden çıkarır ya da geri alır (video ve kayıtlar durur; filmi yeniden birleştirince görünür). */
export const toggleCut = (sc: Scene, act: Act, note: Note) => act(async () => {
  const r = await api.post<{ in_cut: boolean }>(`/scenes/${sc.id}/cut`, { included: sc.in_cut === false });
  note("inspector", r.in_cut ? `${sc.scene_code} filme geri alındı. Filmi yeniden birleştirince görünür.`
    : `${sc.scene_code} filmden çıkarıldı; videosu silinmedi. Filmi yeniden birleştirince export'ta yer almaz.`, "ok");
}, "inspector");

interface Item { label: string; hint?: string; run: () => void; danger?: boolean; disabled?: boolean; sep?: boolean }

/** Sağ tık menüsünü açan işleyici ve menünün kendisi. */
export function useShotMenu(): [(e: ReactMouseEvent, sc: Scene) => void, React.ReactNode] {
  const [at, setAt] = useState<{ sc: Scene; x: number; y: number } | null>(null);
  const open = useCallback((e: ReactMouseEvent, sc: Scene) => {
    e.preventDefault();
    let { clientX: x, clientY: y } = e;
    if (!x && !y) {                                             // klavyeyle açıldı: öğenin altına
      const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
      x = r.left + 8; y = r.bottom;
    }
    setAt({ sc, x, y });
  }, []);
  return [open, at ? <ShotMenu key={`${at.sc.id}:${at.x}:${at.y}`} {...at} close={() => setAt(null)} /> : null];
}

function ShotMenu({ sc: stale, x, y, close }: { sc: Scene; x: number; y: number; close: () => void }) {
  const { ov, projectId, selectScene, setPage, act, note, busy, frozen, setAssembling } = usePanel();
  const sc = ov?.scenes.find((s) => s.id === stale.id) ?? stale;
  const box = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState({ left: x, top: y });
  useDismiss(true, close, box);
  useLayoutEffect(() => {                                       // ekrandan taşmasın
    const el = box.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    setPos({ left: Math.max(4, Math.min(x, window.innerWidth - r.width - 4)),
             top: Math.max(4, y + r.height > window.innerHeight - 4 ? y - r.height : y) });
    el.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus();
  }, [x, y]);
  useEffect(() => {
    const away = () => close();
    window.addEventListener("resize", away);
    window.addEventListener("scroll", away, true);
    return () => { window.removeEventListener("resize", away); window.removeEventListener("scroll", away, true); };
  }, [close]);

  const go = (page: Page) => { selectScene(sc.id); setPage(page); };
  const items: Item[] = [
    { label: "İzleyicide aç", run: () => selectScene(sc.id) },
    { label: sc.in_cut === false ? "Filme geri al" : "Filmden çıkar", hint: "X", disabled: busy,
      run: () => void toggleCut(sc, act, note) },
  ];
  if (sc.cut_tier === "draft" || sc.upgrade_requested) {
    items.push({ label: sc.upgrade_requested ? "İyileştirme işaretini kaldır" : "İyileştir olarak işaretle", disabled: busy,
      run: () => void act(async () => {
        const r = await api.post<{ upgrade_requested: boolean }>(`/scenes/${sc.id}/upgrade`, { requested: !sc.upgrade_requested });
        note("inspector", r.upgrade_requested ? `${sc.scene_code} iyileştirme geçişine alınacak.` : `${sc.scene_code} işareti kaldırıldı.`, "ok");
      }, "inspector") });
  }
  items.push(
    { label: "Videoyu yeni sekmede aç", disabled: !sc.final_url, run: () => sc.final_url && window.open(sc.final_url, "_blank", "noopener") },
    { label: "Çekim kodunu kopyala", run: () => void navigator.clipboard?.writeText(sc.scene_code)
        .then(() => note("inspector", `${sc.scene_code} panoya kopyalandı.`, "ok"), () => undefined) },
    { label: "Storyboard karesine git", sep: true, run: () => go("board") },
    { label: "Renk ayarına git", run: () => go("color") },
    { label: "Filmi yeniden birleştir", sep: true, disabled: busy || !ov?.scenes.some((s) => s.status === "done" && s.in_cut !== false),
      run: () => void act(async () => {
        await api.post(`/projects/${projectId}/assemble`);
        setAssembling({ before: ov?.timeline?.url ?? null, at: Date.now() });
        note("inspector", "Film birleştirmeye alındı.", "ok");
      }, "inspector") },
    { label: "Bu sahneyi baştan üret…", danger: true, disabled: busy || frozen,
      run: () => {
        if (!window.confirm(`${sc.scene_code} baştan üretilsin mi? Ücretli modda para harcar; kuru çalıştırma planı onay isteyebilir.`)) return;
        void act(async () => {
          const r = await api.post<StageResult>(`/scenes/${sc.id}/render?force_visual=true`);
          note("inspector", resultText(r), r.blocked ? "err" : "ok");
        }, "inspector");
      } },
  );

  const keys = (e: React.KeyboardEvent) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const all = [...(box.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? [])];
    const i = all.indexOf(document.activeElement as HTMLButtonElement);
    all[(i + (e.key === "ArrowDown" ? 1 : -1) + all.length) % all.length]?.focus();
  };
  return (
    <div ref={box} className="menu ctx" role="menu" aria-label={`${sc.scene_code} çekimi`} style={pos} onKeyDown={keys}
      onContextMenu={(e) => e.preventDefault()}>
      <div className="ctx-h mono">{sc.scene_code}</div>
      {items.map((it) => (
        <div key={it.label} role="none">
          {it.sep && <hr className="ctx-sep" />}
          <button type="button" role="menuitem" className={it.danger ? "danger" : undefined} disabled={it.disabled}
            onClick={() => { close(); it.run(); }}>
            <span className="mi-t">{it.label}</span>
            {it.hint && <kbd className="ctx-k">{it.hint}</kbd>}
          </button>
        </div>
      ))}
    </div>
  );
}
