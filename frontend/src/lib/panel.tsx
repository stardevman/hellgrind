"use client";
/* Panelin ortak durumu: veriler (SWR ile 2 saniyede bir yoklanır), URL'de tutulan görünüm (proje, sayfa, çekim,
   varyasyon, karşılaştırma, varlık) ve eylemler (meşgul bayrağı, notlar, eylemden sonra anında yenileme). */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useSearchParams } from "next/navigation";
import useSWR, { useSWRConfig } from "swr";
import { api, fetcher } from "./api";
import type { AdminState, Meta, Overview, ProjectListItem, Scene, Sequence } from "./types";

export const POLL_MS = 2000;
/** Sayfalar, üretim sırasıyla (kurgu yazılımlarındaki sayfa çubuğu gibi). */
export const PAGES = ["script", "style", "assets", "board", "edit", "compare", "color", "deliver"] as const;
/** Sayfa çubuğunda olmayan, alt banttan açılan sayfalar. */
export const EXTRA_PAGES = ["settings", "history"] as const;
export type Page = (typeof PAGES)[number] | (typeof EXTRA_PAGES)[number];
const ALL_PAGES: readonly string[] = [...PAGES, ...EXTRA_PAGES];
/** Projeden bağımsız sayfalar: proje yokken ya da çekimsiz projede de açılır. */
export const isExtraPage = (p: string): p is (typeof EXTRA_PAGES)[number] => (EXTRA_PAGES as readonly string[]).includes(p);
/** Servis geçmişinin URL'de tutulan süzgeçleri (sayfa yenilenince ve bağlantıyla paylaşılınca korunur). */
export const HISTORY_PARAMS = { project: "hp", service: "hs", operation: "ho", mode: "hm", status: "hst", days: "hd", call: "hc" } as const;
export type HistoryFilter = Partial<Record<keyof typeof HISTORY_PARAMS, string | null>>;
/** Çekimi olmayan projede açılamayan sayfalar: senaryo sayfasına düşülür. */
export const NEEDS_SHOTS: ReadonlySet<Page> = new Set(["board", "edit", "compare", "color", "deliver"]);
export type NoteKind = "ok" | "err" | "";
export interface Note { text: string; kind: NoteKind }
export type FilmKind = "final" | "draft";

const STORE_KEY = "hg-project";              // eski panelle aynı anahtar: son açılan proje iki panelde de hatırlanır
const ADMIN_KEY = "hg-advanced";           // Basit/Gelişmiş anahtarının eski adı: "Gelişmiş" seçenler yönetici görünümünde açar
export const TOUR_DONE_KEY = "hg-tour-done";
function recall(): string | null { try { return localStorage.getItem(STORE_KEY); } catch { return null; } }
function store(id: string | null) {
  try { if (id) localStorage.setItem(STORE_KEY, id); else localStorage.removeItem(STORE_KEY); } catch { /* depolama kapalı */ }
}
export function readFlag(key: string): boolean { try { return localStorage.getItem(key) === "1"; } catch { return false; } }
export function writeFlag(key: string, on: boolean) { try { if (on) localStorage.setItem(key, "1"); else localStorage.removeItem(key); } catch { /* depolama kapalı */ } }

interface PanelState {
  meta: Meta | undefined;
  projects: ProjectListItem[] | undefined;
  projectId: string | null;
  ov: Overview | undefined;
  offline: boolean;
  creating: boolean;
  setCreating: (on: boolean) => void;
  selectProject: (id: string | null) => void;

  page: Page;
  setPage: (p: Page) => void;
  assetId: string | null;
  selectAsset: (id: string | null) => void;
  scene: Scene | undefined;
  sequence: Sequence | undefined;
  selectScene: (id: string) => void;
  varId: string | null;
  setVar: (id: string | null, sceneId?: string) => void;
  compareRaw: string | null;
  openCompare: (sceneId: string, ids: string[]) => void;
  setCompare: (ids: string[]) => void;
  history: HistoryFilter;
  /** Servis geçmişinin süzgeçlerini değiştirir; `open` ile geçmiş sayfası açılır (diğer süzgeçler sıfırlanır). */
  setHistory: (f: HistoryFilter, open?: boolean) => void;

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

  /** Yönetici modu: Ayarlar (ücretli servisler, mod, fiyat tablosu), servis geçmişi, motor, plan, yükseltme ve elle düzeltme
      yalnızca açıkken görünür. Şifre tanımlıysa (ADMIN_PASSWORD) sunucu girişi ister; seçim tarayıcıda hatırlanır. */
  admin: boolean;
  /** Sunucunun yönetici durumu: şifre gerekli mi, bu tarayıcı girişli mi. */
  adminState: AdminState | undefined;
  /** Yönetici moduna geçer (şifre gerekiyorsa giriş penceresini açar) ya da kullanıcı moduna döner (oturumu kapatır). */
  setAdmin: (on: boolean) => void;
  /** Şifreyle giriş; başarılıysa yönetici modu açılır. Hata mesajı fırlatır. */
  login: (password: string) => Promise<void>;
  loginOpen: boolean;
  setLoginOpen: (on: boolean) => void;
  /** Rehber turu açık mı; ilk açılışta kendiliğinden başlar, bitince tarayıcıda "görüldü" olarak kalır. */
  tour: boolean;
  setTour: (on: boolean) => void;
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
  const [wantAdmin, setWantAdmin] = useState(false);
  const [loginOpen, setLoginOpen] = useState(false);
  const [tour, setTour] = useState(false);
  useEffect(() => { setRemembered(recall()); setWantAdmin(readFlag(ADMIN_KEY)); }, []);

  // Yönetici modu = tarayıcının seçimi + sunucunun izni (şifre yoksa her zaman, varsa geçerli giriş çereziyle). Oturum
  // süresi dolunca sunucu izni kalkar ve panel kendiliğinden kullanıcı moduna döner.
  const { data: adminState, mutate: mutateAdmin } = useSWR<AdminState>("/admin", fetcher, { refreshInterval: POLL_MS * 15 });
  const admin = wantAdmin && Boolean(adminState?.admin);
  const chooseAdmin = useCallback((on: boolean) => { writeFlag(ADMIN_KEY, on); setWantAdmin(on); }, []);
  const setAdmin = useCallback((on: boolean) => {
    if (!on) {
      chooseAdmin(false);
      if (adminState?.required) void api.post<AdminState>("/admin/logout").then((st) => mutateAdmin(st, false)).then(() => mutate(() => true));
      return;
    }
    if (!adminState) return;                             // durum henüz gelmedi: şifre gerekip gerekmediği bilinmiyor
    if (adminState.admin) chooseAdmin(true);
    else setLoginOpen(true);
  }, [adminState, chooseAdmin, mutateAdmin, mutate]);
  const login = useCallback(async (password: string) => {
    const st = await api.post<AdminState>("/admin/login", { password });
    await mutateAdmin(st, false);
    chooseAdmin(true);
    setLoginOpen(false);
  }, [chooseAdmin, mutateAdmin]);
  useEffect(() => { document.body.dataset.admin = admin ? "1" : "0"; }, [admin]);

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
  // Üst bandın proje listesi (durum, harcama) her olayda tazelenir: üretim bitince eski değerde kalmasın.
  const freshness = ov ? `${ov.project.id}:${ov.project.spent_usd}:${ov.project.display_status ?? ov.project.status}:${ov.events?.[0]?.ts ?? ""}` : "";
  useEffect(() => { if (freshness) void mutate("/projects"); }, [freshness, mutate]);
  const offline = Boolean(ovError && !(ovError as { status?: number }).status) || Boolean(listError && !(listError as { status?: number }).status);

  const pageParam = search.get("page") as Page | null;
  const page: Page = pageParam && ALL_PAGES.includes(pageParam) ? pageParam : "edit";
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
    update({ p: id, s: null, v: null, cmp: null, a: null }, true);
  }, [update]);
  const setPage = useCallback((x: Page) => {
    if (x !== "script") setCreating(false);
    update({ page: x === "edit" ? null : x }, true);
  }, [update]);
  const selectAsset = useCallback((id: string | null) => update({ a: id }), [update]);
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
    update({ page: "compare", s: sceneId, cmp: ids.join(",") || null }, true), [update]);
  const setCompare = useCallback((ids: string[]) => update({ cmp: ids.join(",") || null }), [update]);
  const history: HistoryFilter = Object.fromEntries(Object.entries(HISTORY_PARAMS).map(([k, q]) => [k, search.get(q)]));
  const setHistory = useCallback((f: HistoryFilter, open = false) => {
    const keys = open ? (Object.keys(HISTORY_PARAMS) as (keyof typeof HISTORY_PARAMS)[]) : (Object.keys(f) as (keyof typeof HISTORY_PARAMS)[]);
    update({ ...(open ? { page: "history" } : {}), ...Object.fromEntries(keys.map((k) => [HISTORY_PARAMS[k], f[k] ?? null])) }, open);
  }, [update]);

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

  const gotoAsset = useCallback((id: string) => { setFlash(id); update({ page: "assets", a: id }, true); }, [update]);
  const clearFlash = useCallback(() => setFlash(null), []);

  const value: PanelState = {
    meta, projects, projectId, ov: ov && ov.project.id === projectId ? ov : undefined, offline, creating,
    setCreating, selectProject, page, setPage, assetId: search.get("a"), selectAsset, scene, sequence, selectScene, varId, setVar,
    compareRaw: search.get("cmp"), openCompare, setCompare, history, setHistory,
    busy, frozen: Boolean(ov?.project.frozen), act, notes, note, refresh,
    flashAsset, gotoAsset, clearFlash, filmKind, setFilmKind, assembling, setAssembling,
    admin, adminState, setAdmin, login, loginOpen, setLoginOpen, tour, setTour,
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
