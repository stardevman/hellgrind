"use client";
/* Ücretli ek işlem izni (lib/consent.ts): video üretimi dışındaki ücretli bir çağrı (dudak senkronu, görsel,
   seslendirme, LLM ile metin, upscale) izin bekliyorsa pencere açılır. İzin ver: listelenen işlemler; Bu oturum için
   tümüne izin ver: panel bu sekmede açık kaldıkça bütün ücretli ek işlemler sormadan yapılır; Reddet: işlem yapılmaz. */
import { useEffect, useState } from "react";
import useSWR from "swr";
import { api, fetcher } from "@/lib/api";
import {
  denyEffect, dropStaleWaiters, onConsentWait, sessionId, settleConsent, totalUsd, type ConsentRequest, type ConsentState,
} from "@/lib/consent";
import { POLL_MS, usePanel } from "@/lib/panel";
import { Icon } from "./icons";

const usd = (n: number) => `${n.toFixed(2).replace(".", ",")} $`;

export function ConsentDialog() {
  const { projects, projectId, refresh } = usePanel();
  const { data, mutate } = useSWR<ConsentState>("/consent", fetcher, { refreshInterval: POLL_MS });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => onConsentWait(() => { void mutate(); }), [mutate]);
  useEffect(() => { if (data) dropStaleWaiters((data.pending ?? []).map((r) => r.id)); }, [data]);
  const pending = data?.pending ?? [];
  if (!pending.length) return null;

  const title = (r: ConsentRequest) => projects?.find((p) => p.id === r.project_id)?.title;
  const decide = async (how: "once" | "session" | "deny") => {
    setBusy(true); setError("");
    try {
      if (how === "session") {
        await api.post(`/consent/${pending[0].id}/grant`, { scope: "session", session_id: sessionId() });
        pending.forEach((r) => settleConsent(r.id, true));
      } else {
        for (const r of pending) {
          try {
            if (how === "once") await api.post(`/consent/${r.id}/grant`, { scope: "once" });
            else await api.post(`/consent/${r.id}/deny`, {});
          } catch { /* başka sekmede karar verilmiş ya da süresi dolmuş */ }
          settleConsent(r.id, how === "once");
        }
      }
      await mutate();
      refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const total = totalUsd(pending);
  return (
    <div className="modal-back">
      <div className="modal consent" role="alertdialog" aria-modal="true" aria-labelledby="consent-t" aria-describedby="consent-d">
        <h2 id="consent-t"><Icon.lock />Ücretli işlem için izin</h2>
        <p id="consent-d" className="faint small">
          Video üretimi dışında ücretli bir işlem yapılacak. İzin vermeden çağrı yapılmaz ve bütçeden para ayrılmaz.
        </p>
        <ul className="consent-list">
          {pending.map((r) => (
            <li key={r.id}>
              <b>{r.kind_tr}</b>
              <span className="mono">{r.scene_code || (r.asset_id ? "varlık" : "proje")}</span>
              {r.project_id !== projectId && title(r) && <span className="faint">{title(r)}</span>}
              <span className="grow" />
              <span className="mono">~{usd(Number(r.usd) || 0)}</span>
            </li>
          ))}
        </ul>
        {pending.length > 1 && <p className="small">Toplam yaklaşık <b className="mono">{usd(total)}</b></p>}
        <p className="faint small">{denyEffect(pending)}</p>
        {error && <div className="note err" role="alert">{error}</div>}
        <div className="row wrap">
          <button type="button" className="btn ghost" disabled={busy} onClick={() => void decide("deny")}>Reddet</button>
          <span className="grow" />
          <button type="button" className="btn" disabled={busy} onClick={() => void decide("session")}
            title="Bu sekmede panel açık kaldıkça video dışı ücretli işlemler sormadan yapılır (Ayarlar › Bütçe ve durum'dan kaldırılabilir)">
            Bu oturum için tümüne izin ver
          </button>
          <button type="button" className="btn primary" disabled={busy} onClick={() => void decide("once")}>
            {pending.length > 1 ? `${pending.length} işleme izin ver` : "İzin ver"}
          </button>
        </div>
      </div>
    </div>
  );
}
