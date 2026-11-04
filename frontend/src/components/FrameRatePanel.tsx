"use client";
/* Ayarlar › Kare hızı: projenin kare hızı kilidi. Kilit açıkken her çekim renk aşamasında bu hıza çevrilir (kare tutma da
   bu hız üzerinde yapılır); kaba kurgu, film, teslim şartnamesi ve EDL/OTIO aynı hızı kullanır. Değişiklikte bitmiş
   çekimlerde yalnızca renk ve montaj çalışır; görüntü motoru çağrılmaz. */
import useSWR from "swr";
import { api, fetcher } from "@/lib/api";
import { POLL_MS, usePanel } from "@/lib/panel";
import type { FpsLockState } from "@/lib/types";
import { NoteBox, Panel } from "./ui";

const fpsText = (f: string) => f.replace(".", ",");

export function FrameRatePanel() {
  const { projectId, busy, frozen, act, note } = usePanel();
  const { data: st, mutate } = useSWR<FpsLockState>(projectId ? `/projects/${projectId}/fps-lock` : null, fetcher,
    { refreshInterval: POLL_MS * 5 });
  if (!projectId) return <Panel title="Kare hızı"><p className="pad faint">Önce bir proje aç.</p></Panel>;
  if (!st) return <Panel title="Kare hızı"><p className="loading">Yükleniyor…</p></Panel>;
  const choose = (fps: string | null) => act(async () => {
    const r = await api.post<{ fps: string | null; regraded: string[] }>(`/projects/${projectId}/fps-lock`, { fps });
    note("fps", `${r.fps ? `Kare hızı ${fpsText(r.fps)} fps'e kilitlendi` : "Kare hızı kilidi kaldırıldı"}. Yalnızca renk ve montaj `
      + `çalışır; görüntü motoru çağrılmadı. Yeniden işlenen: ${r.regraded.join(", ") || "yok (henüz biten çekim yok)"}.`, "ok");
    void mutate();
  }, "fps");
  const off = st.sources.filter((x) => !x.matches);
  return (
    <Panel title="Kare hızı" meta={st.fps ? `${fpsText(st.fps)} fps kilitli` : "kilit yok"} tour="fps-lock">
      <div className="stack pad">
        <div className="seg" role="group" aria-label="Kare hızı kilidi">
          <button type="button" aria-pressed={!st.fps} disabled={busy || frozen} onClick={() => void choose(null)}>Kilit yok</button>
          {Object.entries(st.options).map(([f, label]) => (
            <button key={f} type="button" aria-pressed={st.fps === f} disabled={busy || frozen} title={label}
              onClick={() => void choose(f)}>{fpsText(f)} fps</button>
          ))}
        </div>
        <p className="faint small">
          Çizgi film için önerilen: <b>24 fps</b>. Video modelleri (H3, Seedance, Veo) zaten 24 fps üretir; çekimler dönüştürülmeden
          birebir kalır. 12 çizim/sn el çizimi görünümü için Stil sayfasındaki <b>On twos</b> anahtarını aç: kare tutma bu
          hız üzerinde yapılır. 25 fps yalnızca müşteri Türkiye / Avrupa televizyonu istiyorsa: 24 fps kaynakta saniyede bir
          kare tekrarlanır.
        </p>
        <p className="faint small">
          Kilit açıkken her çekim renk aşamasında bu hıza çevrilir (süre korunur); kaba kurgu, film, teslim şartnamesi ve
          EDL/OTIO aynı hızı kullanır. Teslim şartnamesinin kare hızı kilitten gelir.
        </p>
        {st.sources.length > 0 && (
          <div className="stack">
            <h3 className="small">Biten çekimlerin kaynak kare hızı</h3>
            <ul className="list">
              {st.sources.map((x) => (
                <li key={x.fps}>
                  <span className="mono">{x.fps === "?" ? "okunamadı" : `${fpsText(x.fps)} fps`}</span>
                  {" · "}{x.shots.length} çekim
                  {st.fps && <span className={`tag ${x.matches ? "ok" : "warn"}`} style={{ marginLeft: 6 }}>{x.matches ? "kilitle aynı" : "dönüştürülür"}</span>}
                  <span className="faint small mono" style={{ marginLeft: 6 }}>{x.shots.slice(0, 6).join(", ")}{x.shots.length > 6 ? "…" : ""}</span>
                </li>
              ))}
            </ul>
            {!st.fps && st.sources.length > 1 && <p className="faint small">Çekimler farklı hızlarda: kilit, filmde ritmin karışmasını önler.</p>}
            {st.fps && off.length > 0 && <p className="faint small">{off.reduce((n, x) => n + x.shots.length, 0)} çekim renk aşamasında {fpsText(st.fps)} fps'e çevrilir.</p>}
          </div>
        )}
        {frozen && <p className="faint small">Üretim durduruldu: önce devam ettir.</p>}
        <NoteBox id="fps" />
      </div>
    </Panel>
  );
}
