"use client";
/* Dizi › Sezon › Bölüm eylemleri (kırıntı menüleri ve Proje yöneticisi paylaşır): ağaç, yeni dizi / sezon / bölüm, bağımsız
   filmi diziye bağlama ve projeyi silme. Yeni oluşturulan bölüm açılır ve Senaryo sayfasına gidilir. */
import { useCallback } from "react";
import useSWR from "swr";
import { api, fetcher } from "./api";
import { POLL_MS, usePanel } from "./panel";
import type { SeriesItem } from "./types";

export function useSeriesActions() {
  const { projects, projectId, selectProject, setPage, ov, act } = usePanel();
  const { data: tree, mutate } = useSWR<SeriesItem[]>("/series", fetcher, { refreshInterval: POLL_MS * 5 });
  const current = projects?.find((p) => p.id === projectId);
  const budget = ov?.project.budget_limit_usd ?? 50;
  const openNew = useCallback(async (r: { project_id: string }) => { await mutate(); selectProject(r.project_id); setPage("script"); }, [mutate, selectProject, setPage]);

  const newEpisode = useCallback((seasonId: string, title: string) => act(async () =>
    openNew(await api.post<{ project_id: string }>(`/seasons/${seasonId}/episodes`, { title, budget_limit_usd: budget, settings: {} })), "global"),
  [act, budget, openNew]);
  const newSeries = useCallback((title: string) => act(async () => {
    const q = await api.post<SeriesItem>("/series", { title });
    await openNew(await api.post<{ project_id: string }>(`/seasons/${q.seasons[0].id}/episodes`, { title: "Bölüm 1", budget_limit_usd: budget, settings: {} }));
  }, "global"), [act, budget, openNew]);
  const newSeason = useCallback((seriesId: string) => act(async () => {
    const z = await api.post<{ id: string; number: number }>(`/series/${seriesId}/seasons`, {});
    await openNew(await api.post<{ project_id: string }>(`/seasons/${z.id}/episodes`, { title: "Bölüm 1", budget_limit_usd: budget, settings: {} }));
  }, "global"), [act, budget, openNew]);
  /** Açık bağımsız filmi dizinin son sezonuna bölüm olarak bağlar. */
  const attach = useCallback((q: SeriesItem) => act(async () => {
    const last = q.seasons[q.seasons.length - 1];
    await api.post(`/projects/${projectId}/episode`, { season_id: last.id });
    await mutate();
  }, "global"), [act, mutate, projectId]);
  /** Açık projeyi (ya da bölümü) onay sorarak kalıcı siler. */
  const remove = useCallback(async () => {
    if (!projectId || !current) return;
    if (!window.confirm(`"${current.title}" ${current.season_id ? "bölümü" : "projesi"} ve tüm sahneleri, varyasyonları ve kayıtları kalıcı olarak silinsin mi? Bu geri alınamaz. Servis geçmişi (harcama kaydı) silinmez.`)) return;
    if (await act(() => api.del(`/projects/${projectId}`), "global")) { await mutate(); selectProject(null); }
  }, [act, current, mutate, projectId, selectProject]);

  return { tree, mutate, current, newEpisode, newSeries, newSeason, attach, remove };
}
