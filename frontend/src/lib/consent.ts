/* Ücretli ek işlemler için izin (arka uç: app/budget/consent.py). Video üretimi dışındaki her ücretli çağrı (dudak
   senkronu, görsel, seslendirme, LLM ile metin, upscale) önce yönetmenin iznini ister. Arka planda bekleyen istekler
   izin penceresinde (components/ConsentDialog.tsx) listelenir; istek içinde izin bekleyen çağrı 428 döner ve api.ts
   pencerede karar verilene dek bekleyip izin verilirse isteği bir kez yineler. */

export interface ConsentRequest {
  id: string;
  project_id: string;
  kind: "lipsync" | "image" | "voice" | "llm" | "upscale" | "other";
  kind_tr: string;
  target: string;
  label: string;
  usd: number;
  scene_id: string | null;
  scene_code: string | null;
  asset_id: string | null;
  created_at: number;
  resume: { type: "scene" | "task" } | null;
}

export interface ConsentState {
  paid_mode: boolean;
  pending: ConsentRequest[];
  session: { active: boolean; expires_in_s: number };
  kinds: Record<string, string>;
}

const SESSION_KEY = "hg-session";
let fallbackSession: string | null = null;

function newId(): string {
  const c = globalThis.crypto as Crypto | undefined;
  if (c?.randomUUID) return c.randomUUID();
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}

/** Panel oturumunun kimliği (sekme başına; sekme kapanınca biter). "Bu oturum için tümüne izin ver" buna bağlıdır;
    panelin her isteği X-HG-Session başlığıyla izni tazeler. */
export function sessionId(): string {
  try {
    let id = sessionStorage.getItem(SESSION_KEY);
    if (!id) { id = newId(); sessionStorage.setItem(SESSION_KEY, id); }
    return id;
  } catch {
    fallbackSession = fallbackSession || newId();
    return fallbackSession;
  }
}

type Waiter = (allowed: boolean) => void;
const waiters = new Map<string, { since: number; fns: Waiter[] }>();
const listeners = new Set<() => void>();
/** Yeni bekleme, yoklamanın eski yanıtıyla düşürülmesin: bu süreden genç bekleme listede yoksa da korunur. */
export const STALE_AFTER_MS = 10_000;

/** 428 alan istek: pencere bu isteğe karar verene dek bekler. true: izin verildi (istek yinelenir). */
export function waitForConsent(id: string): Promise<boolean> {
  return new Promise((resolve) => {
    const w = waiters.get(id) || { since: Date.now(), fns: [] };
    w.fns.push(resolve);
    waiters.set(id, w);
    listeners.forEach((fn) => fn());
  });
}

/** Pencerenin kararı: bekleyen istekleri sonuçlandırır. */
export function settleConsent(id: string, allowed: boolean): void {
  const w = waiters.get(id);
  waiters.delete(id);
  w?.fns.forEach((fn) => fn(allowed));
}

/** Artık beklemeyen (süresi dolmuş ya da başka sekmede karar verilmiş) isteklerin beklemelerini "izin yok" diye bırakır. */
export function dropStaleWaiters(pendingIds: string[], now = Date.now()): void {
  for (const [id, w] of Array.from(waiters.entries())) {
    if (!pendingIds.includes(id) && now - w.since > STALE_AFTER_MS) settleConsent(id, false);
  }
}

export function hasWaiter(id: string): boolean { return waiters.has(id); }

/** Yeni bir 428 beklemesi başlayınca pencere hemen tazelensin (2 sn'lik yoklamayı beklemeden). */
export function onConsentWait(fn: () => void): () => void {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

export function totalUsd(list: ConsentRequest[]): number {
  return list.reduce((sum, r) => sum + (Number(r.usd) || 0), 0);
}

/** Reddedince ne olacağı (türe göre). */
export function denyEffect(list: ConsentRequest[]): string {
  const lipsync = list.some((r) => r.kind === "lipsync");
  const other = list.some((r) => r.kind !== "lipsync");
  if (lipsync && !other) return "Reddedersen dudak senkronu yapılmaz; çekim sesiyle birlikte senkronsuz biter, ücret doğmaz.";
  if (lipsync) return "Reddedersen işlem yapılmaz: dudak senkronu olmadan çekim senkronsuz biter, diğer işlemlerin çekimi hata olarak işaretlenir.";
  return "Reddedersen işlem yapılmaz ve ücret doğmaz; çekimi yeniden üretirsen izin yeniden sorulur.";
}
