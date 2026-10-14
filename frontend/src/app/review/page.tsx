"use client";
import { Suspense } from "react";
import { ReviewView } from "@/components/ReviewView";

/* Müşteri inceleme sayfası: /panel/review/?token=… (statik dışa aktarımda dinamik yol yok; belirteç sorgu dizgesinde).
   Stüdyonun çerçevesi, bütçesi ve yönetici modu bu sayfada yüklenmez. */
export default function ReviewPage() {
  return (
    <Suspense fallback={<main className="review"><p className="review-msg">Yükleniyor…</p></main>}>
      <ReviewView />
    </Suspense>
  );
}
