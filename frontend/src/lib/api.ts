/* API istemcisi. Bütün çağrılar bu nesneden geçer: backend/tests/test_panel_next.py, panelin çağırdığı her yolun
   arka uçta o yöntemle var olduğunu `api.<yöntem>(\`...\`)` kalıbından okuyarak denetler. */

import { sessionId, waitForConsent } from "./consent";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

interface ErrorBody { detail?: string | { msg?: string }[] }

export function detailOf(data: unknown): string {
  const d = (data as ErrorBody | null)?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((x) => (x.msg || "").replace(/^Value error, /, "")).filter(Boolean).join("; ");
  return "";
}

async function request<T>(method: string, path: string, body?: unknown, file?: File, retried = false): Promise<T> {
  const headers: Record<string, string> = { "X-HG-Session": sessionId() };
  let payload: BodyInit | undefined;
  if (file) {
    headers["Content-Type"] = file.type || "application/octet-stream";
    payload = file;
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const res = await fetch("/api" + path, { method, headers, body: payload });
  let data: unknown = null;
  try { data = await res.json(); } catch { /* gövde yok */ }
  const consent = (data as { consent?: { id?: string } } | null)?.consent;
  if (res.status === 428 && consent?.id && !retried) {
    // Ücretli ek işlem izin bekliyor: izin penceresinde karar verilene dek beklenir, izin verilirse istek yinelenir.
    if (await waitForConsent(consent.id)) return request<T>(method, path, body, file, true);
    throw new ApiError("İzin verilmedi: ücretli işlem yapılmadı.", 428);
  }
  if (!res.ok) {
    const fallback = file ? `Yükleme başarısız (${res.status})` : `İstek başarısız (${res.status})`;
    throw new ApiError(detailOf(data) || fallback, res.status);
  }
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body),
  del: <T>(path: string) => request<T>("DELETE", path),
  /** Ham gövdeyle dosya yükleme (multipart değil): arka uç dosyayı akış olarak alır. */
  upload: <T>(path: string, file: File) => request<T>("POST", path, undefined, file),
};

/** SWR için okuyucu: anahtar, /api önekinden sonraki yoldur. */
export const fetcher = <T>(path: string) => api.get<T>(path);
