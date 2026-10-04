"use client";
/* Panelin ortak durumu: veriler (SWR ile 2 saniyede bir yoklanır), URL'de tutulan görünüm (proje, sekme, sahne,
   varyasyon, karşılaştırma) ve eylemler (meşgul bayrağı, notlar, eylemden sonra anında yenileme). */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useSearchParams } from "next/navigation";
import useSWR, { useSWRConfig } from "swr";
import { fetcher } from "./api";
import type { Meta, Overview, ProjectListItem, Scene, Sequence } from "./types";

export const POLL_MS = 2000;
export const TABS = ["scenes", "compare", "assets", "board", "style", "efficiency"] as const;
export type Tab = (typeof TABS)[number];
export type NoteKind = "ok" | "err" | "";
export interface Note { text: string; kind: NoteKind }
export type FilmKind = "final" | "draft";

const STORE_KEY = "hg-project";              // eski panelle aynı anahtar: son açılan proje iki panelde de hatırlanır
function recall(): string | null { try { return localStorage.getItem(STORE_KEY); } catch { return null; } }
function store(id: string | null) {
  try { if (id) localStorage.setItem(STORE_KEY, id); else localStorage.removeItem(STORE_KEY); } catch { /* depolama kapalı */ }
}

interface PanelState {
  meta: Meta | undefined;
  projects: ProjectListItem[] | undefined;
  projectId: string | null;
  ov: Overview | undefined;
  offline: boolean;
  creating: boolean;
  setCreating: (on: boolean) => void;
  selectProject: (id: string | null) => void;

  tab: Tab;
  setTab: (t: Tab) => void;
  scene: Scene | undefined;
  sequence: Sequence | undefined;
  selectScene: (id: string) => void;
  varId: string | null;
  setVar: (id: string | null, sceneId?: string) => void;
  compareRaw: string | null;
  openCompare: (sceneId: string, ids: string[]) => void;
  setCompare: (ids: string[]) => void;

  busy: boolean;
  frozen: boolean;
  /** Eylemi çalıştırır (aynı anda tek eylem); hata notKey notuna yazılır. Başarılıysa true döner. */
  act: (fn: () => Promise<unknown>, noteKey: string) => Promise<boolean>;
  notes: Record<string, Note>;
  note: (key: string, text: string, kind?: NoteKind) => void;
  refresh: () => void;

  flashAsset: string | null;
  gotoAsset: (id: string) => void;
  clearFlash: () => void;
  filmKind: FilmKind;
  setFilmKind: (k: FilmKind) => void;
  assembling: { before: string | null; at: number } | null;
  setAssembling: (a: { before: string | null; at: number } | null) => void;
}

const Ctx = createContext<PanelState | null>(null);

export function usePanel(): PanelState {
  const v = useContext(Ctx);
  if (!v) throw new Error("usePanel, PanelProvider içinde kullanılmalı");
  return v;
}

/** URL'yi Next'in yönlendiricisini çağırmadan günceller (statik dışa aktarımda sunucuya gidilmez). */
function writeUrl(params: URLSearchParams, push: boolean) {
  const qs = params.toString();
  const url = qs ? `?${qs}` : window.location.pathname;
  if (push) window.history.pushState(null, "", url);
  else window.history.replaceState(null, "", url);
}

export function PanelProvider({ children }: { children: ReactNode }) {
  const search = useSearchParams();
  const { mutate } = useSWRConfig();
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notes, setNotes] = useState<Record<string, Note>>({});
  const [flashAsset, setFlash] = useState<string | null>(null);
  const [filmKind, setFilmKind] = useState<FilmKind>("final");
  const [assembling, setAssembling] = useState<PanelState["assembling"]>(null);
  const [remembered, setRemembered] = useState<string | null>(null);
  useEffect(() => { setRemembered(recall()); }, []);

  const { data: meta } = useSWR<Meta>("/meta", fetcher, { revalidateOnFocus: false });
  const { data: projects, error: listError } = useSWR<ProjectListItem[]>("/projects", fetcher, { refreshInterval: POLL_MS * 5 });

  // Proje: URL > son açılan > en yeni. Silinmiş bir projeye işaret eden değer yok sayılır. "Yeni proje" formu
  // açıkken seçili proje değişmez; vazgeçilince kalınan yere dönülür.
  const urlProject = search.get("p");
  const projectId = useMemo(() => {
    for (const id of [urlProject, remembered]) if (id && (!projects || projects.some((p) => p.id === id))) return id;
    return projects?.[0]?.id ?? null;
  }, [urlProject, remembered, projects]);

  const { data: ov, error: ovError } = useSWR<Overview>(projectId ? `/projects/${projectId}/overview` : null, fetcher,
    { refreshInterval: POLL_MS, keepPreviousData: false, revalidateOnFocus: true });
  const offline = Boolean(ovError && !(ovError as { status?: number }).status) || Boolean(listError && !(listError as { status?: number }).status);

  const tabParam = search.get("tab") as Tab | null;
  const tab: Tab = tabParam && (TABS as readonly string[]).includes(tabParam) ? tabParam : "scenes";
  const sceneParam = search.get("s");
  const scene = ov?.scenes.find((x) => x.id === sceneParam) ?? ov?.scenes[0];
  const sequence = scene ? ov?.sequences.find((q) => q.id === scene.sequence_id) : undefined;
  const varParam = search.get("v");
  const varId = scene && varParam && scene.variations.some((v) => v.id === varParam) ? varParam : null;

  const update = useCallback((changes: Record<string, string | null>, push = false) => {
    const params = new URLSearchParams(window.location.search);
    for (const [k, v] of Object.entries(changes)) { if (v) params.set(k, v); else params.delete(k); }
    writeUrl(params, push);
  }, []);

  const selectProject = useCallback((id: string | null) => {
    store(id);
    setRemembered(id);
    setCreating(false);
    setNotes({});
    update({ p: id, s: null, v: null, cmp: null }, true);
  }, [update]);
  const setTab = useCallback((t: Tab) => update({ tab: t === "scenes" ? null : t }, true), [update]);
  const selectScene = useCallback((id: string) => {
    if (id === scene?.id) return;
    setNotes((n) => ({ ...n, inspector: { text: "", kind: "" } }));
    update({ s: id, v: null });
  }, [scene?.id, update]);
  const setVar = useCallback((id: string | null, sceneId?: string) => {
    setNotes((n) => ({ ...n, inspector: { text: "", kind: "" } }));
    update({ v: id, ...(sceneId ? { s: sceneId } : {}) });
  }, [update]);
  const openCompare = useCallback((sceneId: string, ids: string[]) =>
    update({ tab: "compare", s: sceneId, cmp: ids.join(",") || null }, true), [update]);
  const setCompare = useCallback((ids: string[]) => update({ cmp: ids.join(",") || null }), [update]);

  const note = useCallback((key: string, text: string, kind: NoteKind = "") =>
    setNotes((n) => ({ ...n, [key]: { text, kind } })), []);
  const refresh = useCallback(() => { void mutate(() => true); }, [mutate]);

  const busyRef = useRef(false);
  const act = useCallback(async (fn: () => Promise<unknown>, noteKey: string) => {
    if (busyRef.current) return false;
    busyRef.current = true;
    setBusy(true);
    try { await fn(); return true; }
    catch (e) { note(noteKey, (e as Error).message, "err"); return false; }
    finally { busyRef.current = false; setBusy(false); refresh(); }
  }, [note, refresh]);

  const gotoAsset = useCallback((id: string) => { setFlash(id); update({ tab: "assets" }, true); }, [update]);
  const clearFlash = useCallback(() => setFlash(null), []);

  const value: PanelState = {
    meta, projects, projectId, ov: ov && ov.project.id === projectId ? ov : undefined, offline, creating,
    setCreating, selectProject, tab, setTab, scene, sequence, selectScene, varId, setVar,
    compareRaw: search.get("cmp"), openCompare, setCompare,
    busy, frozen: Boolean(ov?.project.frozen), act, notes, note, refresh,
    flashAsset, gotoAsset, clearFlash, filmKind, setFilmKind, assembling, setAssembling,
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
