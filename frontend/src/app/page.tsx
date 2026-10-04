"use client";
import { Suspense } from "react";
import { SWRConfig } from "swr";
import { AppFrame } from "@/components/AppFrame";
import { PanelProvider } from "@/lib/panel";

/* Tek sayfa: görünüm durumu (proje, sekme, sahne, varyasyon, karşılaştırma) URL'nin sorgu dizgesinde tutulur;
   bağlantı paylaşılınca aynı görünüm açılır. */
export default function Page() {
  return (
    <SWRConfig value={{ dedupingInterval: 500, errorRetryInterval: 2000, shouldRetryOnError: true }}>
      <Suspense fallback={<p className="loading">Stüdyo yükleniyor…</p>}>
        <PanelProvider>
          <AppFrame />
        </PanelProvider>
      </Suspense>
    </SWRConfig>
  );
}
