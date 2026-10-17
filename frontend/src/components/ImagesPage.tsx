"use client";
/* Görsel stüdyosu: varlık görselini dışarıya çıkmadan üretme alanı. Solda istem (metinden ya da kaynaklardan düzenleme,
   boy, adet, stil kılavuzu), ortada galeri, sağda seçili görselin ayrıntısı ve "Varlığa ata". Atanan görsel varlığın
   referansı olur (Varlıklar'daki yükleme ile aynı yol: sürüm artar, varlık taslağa döner, onay akışı yeniden işler).
   Ücretsiz modda yerel taslak çizilir ve fal.ai fiyatı sanal olarak bütçeden düşer; ücretli modda Qwen-Image-2512
   (metinden) ve Qwen-Image-Edit-2511 (kaynaklardan) fal.ai'de çalışır. */
import { useEffect, useMemo, useState, type FormEvent } from "react";
import useSWR from "swr";
import { api, fetcher } from "@/lib/api";
import { REF_TR, TYPE_TR } from "@/lib/i18n";
import { POLL_MS, usePanel } from "@/lib/panel";
import { usd, usdExact } from "@/lib/text";
import type { Asset, StudioImage, StudioKind, StudioMode, StudioOptions, StudioSourceIn } from "@/lib/types";
import { Lightbox, type Shown } from "./AssetsPage";
import { Icon } from "./icons";
import { NoteBox, Panel, Section } from "./ui";

const KIND_TR: Record<StudioKind, string> = { free: "Serbest", character: "Karakter", location: "Mekân", prop: "Nesne" };
const STATE_TR: Record<StudioImage["status"], string> = { queued: "Sırada", running: "Çiziliyor", done: "Hazır", failed: "Üretilemedi" };
const RATIO: Record<string, string> = { square: "1:1", landscape: "16:9", portrait: "9:16", classic: "4:3", classic_portrait: "3:4" };
const SOURCE_KINDS = ["still", "expressions", "turnaround"] as const;
type Filter = "all" | "favorite" | "assigned";

interface Picked { id: string; label: string; thumb: string | null; src: StudioSourceIn }
interface Draft { mode: StudioMode; prompt: string; aspect: string; kind: StudioKind; count: number; assetId: string; applyStyle: boolean; sources: Picked[]; seed: string }
const EMPTY: Draft = { mode: "text", prompt: "", aspect: "square", kind: "free", count: 2, assetId: "", applyStyle: true, sources: [], seed: "" };

const assetThumb = (a: Asset) => a.references.still?.thumb_url ?? a.references.turnaround?.frame_urls?.[0] ?? a.references.expressions?.thumb_url ?? null;

/** Varlıkların düzenlemeye kaynak olabilecek görsel referansları (durağan, ifade sayfası, dönüşün ilk karesi). */
function assetSources(assets: Asset[]): Picked[] {
  return assets.flatMap((a) => SOURCE_KINDS.filter((k) => a.references[k]).map((k) => {
    const r = a.references[k];
    return { id: `${a.id}:${k}`, label: `${a.name} · ${REF_TR[k]}`, thumb: k === "turnaround" ? r.frame_urls?.[0] ?? null : r.thumb_url ?? null, src: { asset_id: a.id, kind: k } };
  }));
}

const imageSource = (x: StudioImage): Picked => ({ id: x.id, label: `Görsel ${x.id.slice(0, 4)}`, thumb: x.thumb_url, src: { image_id: x.id } });

function Prompt({ draft, set, options, assets, onSubmit }: {
  draft: Draft; set: (d: Partial<Draft>) => void; options: StudioOptions | undefined; assets: Asset[]; onSubmit: () => void;
}) {
  const { busy, frozen, meta } = usePanel();
  const target = assets.find((a) => a.id === draft.assetId);
  const pool = useMemo(() => assetSources(assets), [assets]);
  const per = options?.estimates[draft.mode]?.[draft.aspect] ?? 0;
  const size = options?.aspects.find((x) => x.id === draft.aspect);
  const max = options?.max_sources ?? 3;
  const missing = !draft.prompt.trim() && !target ? "Ne çizileceğini yaz ya da bir varlık seç." : draft.mode === "edit" && !draft.sources.length ? "Kaynaktan düzenleme için en az bir kaynak görsel ekle." : "";
  const submit = (e: FormEvent) => { e.preventDefault(); onSubmit(); };
  const addSource = (id: string) => {
    const p = pool.find((x) => x.id === id);
    if (p && !draft.sources.some((x) => x.id === id) && draft.sources.length < max) set({ sources: [...draft.sources, p] });
  };
  return (
    <form className="pad stack studio-form" onSubmit={submit} data-tour="images-prompt">
      <div className="seg" role="group" aria-label="Üretim kipi">
        <button type="button" aria-pressed={draft.mode === "text"} onClick={() => set({ mode: "text" })} title="Qwen-Image-2512: yalnızca istemden">Metinden</button>
        <button type="button" aria-pressed={draft.mode === "edit"} onClick={() => set({ mode: "edit" })} title="Qwen-Image-Edit-2511: kaynak görselleri istemle değiştirir ya da birleştirir">Kaynaktan düzenle</button>
      </div>
      <label className="field">Ne için
        <select value={draft.assetId} onChange={(e) => set({ assetId: e.target.value })} aria-label="Hedef varlık">
          <option value="">Serbest görsel</option>
          {(["character", "location", "prop"] as const).map((t) => {
            const list = assets.filter((a) => a.type === t);
            return list.length ? <optgroup key={t} label={TYPE_TR[t]}>{list.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}</optgroup> : null;
          })}
        </select>
      </label>
      {target ? (
        <p className="small faint">{target.name} için: varlığın tarifi ve değişmez özellikleri isteme eklenir; istemi boş bırakırsan Varlıklar&apos;daki referans görseli istemi kullanılır.</p>
      ) : (
        <div className="field">Tür
          <div className="seg" role="group" aria-label="Görselin türü">
            {(Object.keys(KIND_TR) as StudioKind[]).map((k) => <button key={k} type="button" aria-pressed={draft.kind === k} onClick={() => set({ kind: k })}>{KIND_TR[k]}</button>)}
          </div>
        </div>
      )}
      <label className="field">İstem
        <textarea rows={6} value={draft.prompt} maxLength={2000} onChange={(e) => set({ prompt: e.target.value })} aria-label="İstem"
          placeholder={draft.mode === "edit" ? "ör. image 1'deki karakteri image 2'deki mekânda, tezgâha yaslanmış çiz" : "ör. Yağmurlu gecede eski sarı bir taksi, ıslak asfaltta neon yansımaları"} />
      </label>
      {meta && !meta.dry_run && <p className="small faint">Türkçe yazabilirsin: istem modele gitmeden yerel olarak İngilizceye çevrilir, adlar korunur.</p>}
      {draft.mode === "edit" && (
        <div className="field">Kaynaklar (istemde image 1, image 2, image 3)
          {draft.sources.length > 0 && (
            <ul className="studio-src">
              {draft.sources.map((x, i) => (
                <li key={x.id}>{x.thumb ? <img src={x.thumb} alt="" /> : <span className="ph" />}
                  <span className="grow"><b>image {i + 1}</b> · {x.label}</span>
                  <button type="button" className="btn ghost icon-only" aria-label={`${x.label} kaynağını çıkar`} onClick={() => set({ sources: draft.sources.filter((y) => y.id !== x.id) })}><Icon.close /></button>
                </li>
              ))}
            </ul>
          )}
          {draft.sources.length < max ? (
            <select value="" onChange={(e) => addSource(e.target.value)} aria-label="Varlık referansı ekle">
              <option value="">{pool.length ? "Varlık referansı ekle…" : "Referanslı varlık yok"}</option>
              {pool.filter((p) => !draft.sources.some((x) => x.id === p.id)).map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
            </select>
          ) : <small className="faint">En çok {max} kaynak.</small>}
          <small className="faint">Galerideki bir görseli de &quot;Kaynak yap&quot; ile ekleyebilirsin.</small>
        </div>
      )}
      <div className="field"><span>Boy{size && <span className="faint"> · {size.width}×{size.height}</span>}</span>
        <div className="seg" role="group" aria-label="Boy">
          {(options?.aspects ?? []).map((x) => <button key={x.id} type="button" aria-pressed={draft.aspect === x.id} title={x.label} onClick={() => set({ aspect: x.id })}>{RATIO[x.id] ?? x.label}</button>)}
        </div>
      </div>
      <div className="field">Adet
        <div className="seg" role="group" aria-label="Adet">
          {Array.from({ length: options?.max_count ?? 4 }, (_, i) => i + 1).map((n) => <button key={n} type="button" aria-pressed={draft.count === n} onClick={() => set({ count: n })}>{n}</button>)}
        </div>
      </div>
      <label className="check"><input type="checkbox" checked={draft.applyStyle} onChange={(e) => set({ applyStyle: e.target.checked })} />Stil kılavuzunu uygula (ışık dili, palet, türün doku kuralı)</label>
      <label className="field adv">Tohum (boşsa her görsel farklı)
        <input type="number" min={0} max={2147483647} value={draft.seed} onChange={(e) => set({ seed: e.target.value })} aria-label="Tohum" />
      </label>
      <p className="small dim">
        {draft.count} görsel · {usd(per * draft.count)}
        {meta?.dry_run ? " (ücretsiz mod: yerel taslak çizilir, fiyat sanal olarak düşer)" : ""}
        <span className="adv faint"> · {options?.models[draft.mode]}</span>
      </p>
      {options && !options.ready && <p className="small warn">{options.reason}</p>}
      <button className="btn primary lg" type="submit" disabled={busy || frozen || !options?.ready || Boolean(missing)} title={missing || undefined}>Üret</button>
      {frozen && <p className="small warn">Proje durduruldu: üretime devam ettirince görsel üretebilirsin.</p>}
      <NoteBox id="images" />
    </form>
  );
}

function Card({ x, current, assets, onPick }: { x: StudioImage; current: boolean; assets: Asset[]; onPick: () => void }) {
  const names = [...new Set(x.assignments.map((y) => y.name))];
  const target = assets.find((a) => a.id === x.target_asset_id);
  return (
    <button type="button" className="card studio-card" aria-current={current} onClick={onPick} aria-label={`${x.prompt || target?.name || "Görsel"} (${STATE_TR[x.status]})`}>
      {x.thumb_url ? <img className="th fit" src={x.thumb_url} alt="" loading="lazy" />
        : <span className={`th ${x.status === "failed" ? "bad" : "work"}`}>{STATE_TR[x.status]}</span>}
      <h3>{x.prompt || (target ? `${target.name} referansı` : "Görsel")}</h3>
      <span className="row small faint">
        {x.favorite && <span className="tag warn" title="Seçildi">★</span>}
        {names.length > 0 && <span className="tag ok" title="Varlığa atandı">{names.join(", ")}</span>}
        {x.mode === "edit" && <span className="tag">düzenleme</span>}
        <span className="grow" />{RATIO[x.aspect]}{x.status === "done" ? ` · ${usd(x.cost_usd)}` : ""}
      </span>
    </button>
  );
}

function Detail({ x, assets, onReuse, onSource, canSource }: {
  x: StudioImage; assets: Asset[]; onReuse: (x: StudioImage) => void; onSource: (x: StudioImage) => void; canSource: boolean;
}) {
  const { busy, act, note, projectId, gotoAsset } = usePanel();
  const [shown, setShown] = useState<Shown | null>(null);
  const [assetId, setAssetId] = useState(x.target_asset_id ?? "");
  const [kind, setKind] = useState<"still" | "expressions">("still");
  const [newType, setNewType] = useState(x.kind === "free" ? "prop" : x.kind);
  const [newName, setNewName] = useState("");
  const target = assets.find((a) => a.id === assetId);
  const editable = assets.filter((a) => a.status !== "locked");
  const ready = x.status === "done" && Boolean(x.url);
  const engine = x.engine === "local" ? "yerel taslak" : x.mode === "edit" ? "Qwen-Image-Edit" : "Qwen-Image";
  const assign = (id: string, name: string, k: "still" | "expressions") => act(async () => {
    await api.post(`/images/${x.id}/assign`, { asset_id: id, kind: k });
    note("images-detail", `${name}: ${REF_TR[k].toLocaleLowerCase("tr")} olarak atandı. Varlık taslakta; Varlıklar sayfasında durağanı onayla.`, "ok");
  }, "images-detail");
  const patch = (body: { favorite?: boolean; hidden?: boolean }) => act(() => api.patch(`/images/${x.id}`, body), "images-detail");
  const createAndAssign = (e: FormEvent) => {
    e.preventDefault();
    void act(async () => {
      const a = await api.post<Asset>(`/projects/${projectId}/assets`, { type: newType, name: newName });
      await api.post(`/images/${x.id}/assign`, { asset_id: a.id, kind: "still" });
      setNewName("");
      setAssetId(a.id);
      note("images-detail", `${a.name} eklendi ve bu görsel durağan referansı oldu (taslak).`, "ok");
    }, "images-detail");
  };
  return (
    <section className="pnl" aria-label="Görselin ayrıntısı">
      <header className="pnl-h"><h2>Ayrıntı</h2><span className="meta">{x.width}×{x.height}</span><span className="grow" />
        <span className={`tag ${x.status === "done" ? "ok" : x.status === "failed" ? "bad" : ""}`}>{STATE_TR[x.status]}</span></header>
      <div className="pnl-b">
        <div className="pad stack">
          {ready ? (
            <button type="button" className="zoom big" title="Tam boyutu aç" onClick={() => setShown({ src: x.url as string, name: x.prompt.slice(0, 60) || "Görsel" })}>
              <img src={x.thumb_url ?? x.url ?? undefined} alt={x.prompt || "Üretilen görsel"} />
            </button>
          ) : <div className={`studio-ph ${x.status === "failed" ? "bad" : "work"}`}>{STATE_TR[x.status]}</div>}
          {x.error && <p className="small warn">{x.error}</p>}
          {x.prompt && <p>{x.prompt}</p>}
          <p className="small faint">{engine} · tohum {x.seed} · {usdExact(x.cost_usd)}{x.apply_style ? " · stil kılavuzuyla" : ""}</p>
          {x.sources.length > 0 && (
            <ul className="studio-src">{x.sources.map((s, i) => <li key={i}>{s.thumb_url ? <img src={s.thumb_url} alt="" /> : <span className="ph" />}<span><b>image {i + 1}</b> · {s.label}</span></li>)}</ul>
          )}
          <div className="row">
            <button className="btn" type="button" disabled={busy} aria-pressed={x.favorite} onClick={() => patch({ favorite: !x.favorite })}>{x.favorite ? "★ Seçildi" : "☆ Seç"}</button>
            <button className="btn" type="button" disabled={busy} onClick={() => onReuse(x)} title="İstemi ve ayarları forma alır; yeni tohumla">Benzerini üret</button>
            {ready && <button className="btn" type="button" disabled={!canSource} onClick={() => onSource(x)} title="Kaynaktan düzenleme kipinde image olarak ekler">Kaynak yap</button>}
            {ready && <a className="btn" href={x.url as string} download={`gorsel-${x.id.slice(0, 8)}.png`}><Icon.download />İndir</a>}
            <button className="btn ghost" type="button" disabled={busy} onClick={() => patch({ hidden: !x.hidden })}>{x.hidden ? "Geri al" : "Gizle"}</button>
          </div>
          <NoteBox id="images-detail" />
        </div>
        {ready && (
          <Section title="Varlığa ata" meta={x.assignments.length ? `${x.assignments.length}` : ""}>
            <div className="stack" data-tour="images-assign">
              <div className="form-row">
                <label className="field grow">Varlık
                  <select value={assetId} onChange={(e) => setAssetId(e.target.value)} aria-label="Atanacak varlık">
                    <option value="">Seç…</option>
                    {editable.map((a) => <option key={a.id} value={a.id}>{a.name} ({TYPE_TR[a.type]})</option>)}
                  </select>
                </label>
                <label className="field">Referans
                  <select value={kind} onChange={(e) => setKind(e.target.value as "still" | "expressions")} aria-label="Referans türü">
                    <option value="still">{REF_TR.still}</option>
                    {target?.type === "character" && <option value="expressions">{REF_TR.expressions}</option>}
                  </select>
                </label>
                <button className="btn primary" type="button" disabled={busy || !target} onClick={() => target && assign(target.id, target.name, target.type === "character" ? kind : "still")}>Ata</button>
              </div>
              {target?.references[kind] && <p className="small faint">{target.name} varlığının mevcut {REF_TR[kind].toLocaleLowerCase("tr")} referansı geçmişe taşınır; sürüm artar.</p>}
              <form className="form-row" onSubmit={createAndAssign}>
                <label className="field">Yeni varlık
                  <select value={newType} onChange={(e) => setNewType(e.target.value as Asset["type"])} aria-label="Yeni varlığın türü">
                    <option value="prop">Nesne</option><option value="location">Mekân</option><option value="character">Karakter</option>
                  </select>
                </label>
                <label className="field grow">Ad<input value={newName} maxLength={100} required onChange={(e) => setNewName(e.target.value)} aria-label="Yeni varlığın adı" /></label>
                <button className="btn" type="submit" disabled={busy || !newName.trim()}>Ekle ve ata</button>
              </form>
              {x.assignments.length > 0 && (
                <ul className="list small">
                  {x.assignments.slice().reverse().map((y) => (
                    <li key={`${y.asset_id}-${y.at}`}><button type="button" onClick={() => gotoAsset(y.asset_id)} title="Varlıklar sayfasında aç">
                      {y.name} · {REF_TR[y.kind] ?? y.kind}<span className="count">v{y.version}</span></button></li>
                  ))}
                </ul>
              )}
            </div>
          </Section>
        )}
        <Section title="Modele giden istem" open={false}>
          <p className="small mono-wrap">{x.composed_prompt || "—"}</p>
          {x.negative && <p className="small faint mono-wrap">Negatif: {x.negative}</p>}
        </Section>
      </div>
      {shown && <Lightbox shown={shown} onClose={() => setShown(null)} />}
    </section>
  );
}

export function ImagesPage() {
  const { projectId, act, note, assetId: urlAsset } = usePanel();
  const { data: options } = useSWR<StudioOptions>("/images/options", fetcher);
  const { data: assets } = useSWR<Asset[]>(projectId ? `/projects/${projectId}/assets` : null, fetcher, { refreshInterval: POLL_MS });
  const [showHidden, setShowHidden] = useState(false);
  const { data: images, mutate } = useSWR<StudioImage[]>(projectId ? `/projects/${projectId}/images${showHidden ? "?hidden=true" : ""}` : null, fetcher, { refreshInterval: POLL_MS });
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const set = (d: Partial<Draft>) => setDraft((x) => ({ ...x, ...d }));
  const [filter, setFilter] = useState<Filter>("all");
  const [sel, setSel] = useState<string | null>(null);
  const all = assets ?? [];
  // Varlıklar sayfasından "Görsel stüdyosunda üret" ile gelinince o varlık hedef olur.
  useEffect(() => { if (urlAsset && all.some((a) => a.id === urlAsset)) setDraft((x) => (x.assetId ? x : { ...x, assetId: urlAsset })); }, [urlAsset, all]);

  const list = (images ?? []).filter((x) => filter === "all" || (filter === "favorite" ? x.favorite : x.assignments.length > 0));
  const selected = list.find((x) => x.id === sel) ?? list[0];
  const pending = (images ?? []).filter((x) => x.status === "queued" || x.status === "running").length;

  const generate = () => act(async () => {
    const body = {
      prompt: draft.prompt, mode: draft.mode, aspect: draft.aspect, kind: draft.kind, count: draft.count,
      asset_id: draft.assetId || null, apply_style: draft.applyStyle, sources: draft.mode === "edit" ? draft.sources.map((x) => x.src) : [],
      seed: draft.seed === "" ? null : Number(draft.seed),
    };
    const r = await api.post<{ images: StudioImage[]; estimate_usd: number }>(`/projects/${projectId}/images`, body);
    setFilter("all");
    setSel(r.images[0]?.id ?? null);
    void mutate();
    note("images", `${r.images.length} görsel kuyruğa alındı (≈ ${usd(r.estimate_usd)}). Bitenler galeriye düşer.`, "ok");
  }, "images");
  const reuse = (x: StudioImage) => {
    const pool = assetSources(all);
    const sources = x.sources.map((s): Picked | undefined => s.image_id
      ? { id: s.image_id, label: s.label, thumb: s.thumb_url, src: { image_id: s.image_id } }
      : pool.find((p) => p.id === `${s.asset_id}:${s.kind}`)).filter((p): p is Picked => Boolean(p));
    setDraft({ ...draft, mode: x.mode, prompt: x.prompt, aspect: x.aspect, kind: x.kind, assetId: x.target_asset_id ?? "", applyStyle: x.apply_style, sources, seed: "" });
    note("images", "İstem ve ayarlar forma alındı; Üret yeni tohumlarla çizer.", "");
  };
  const toSource = (x: StudioImage) => {
    if (draft.sources.some((s) => s.id === x.id)) { set({ mode: "edit" }); return; }
    set({ mode: "edit", sources: [...draft.sources, imageSource(x)].slice(0, options?.max_sources ?? 3) });
    note("images", "Görsel kaynak olarak eklendi (Kaynaktan düzenle).", "");
  };
  const count = (f: Filter) => (images ?? []).filter((x) => f === "all" || (f === "favorite" ? x.favorite : x.assignments.length > 0)).length;
  return (
    <div className="page three" data-tour="images">
      <Panel title="Görsel üret" meta={options ? (options.engine === "local" ? "yerel taslak" : "fal.ai") : ""}>
        <Prompt draft={draft} set={set} options={options} assets={all} onSubmit={generate} />
      </Panel>
      <Panel title="Galeri" meta={images ? `${list.length} görsel${pending ? ` · ${pending} çiziliyor` : ""}` : ""}
        tools={<div className="row">
          <div className="seg" role="group" aria-label="Süzgeç">
            {([["all", "Tümü"], ["favorite", "Seçilenler"], ["assigned", "Atananlar"]] as [Filter, string][]).map(([k, v]) => (
              <button key={k} type="button" aria-pressed={filter === k} onClick={() => setFilter(k)}>{v} {count(k)}</button>
            ))}
          </div>
          <label className="check"><input type="checkbox" checked={showHidden} onChange={(e) => setShowHidden(e.target.checked)} />Gizlenenler</label>
        </div>}>
        {!images ? <p className="loading">Yükleniyor…</p> : list.length ? (
          <div className="grid-cards">
            {list.map((x) => <Card key={x.id} x={x} current={selected?.id === x.id} assets={all} onPick={() => setSel(x.id)} />)}
          </div>
        ) : (
          <p className="empty">{images.length ? "Bu süzgece uyan görsel yok." : "Henüz görsel yok. Soldan bir istem yaz ya da bir varlık seç ve Üret'e bas; üretilen görseli tek tıkla bir varlığa referans olarak atayabilirsin."}</p>
        )}
      </Panel>
      {selected ? <Detail key={selected.id} x={selected} assets={all} onReuse={reuse} onSource={toSource}
        canSource={draft.sources.length < (options?.max_sources ?? 3) || draft.sources.some((s) => s.id === selected.id)} />
        : <Panel title="Ayrıntı"><p className="empty">Görsel seçin.</p></Panel>}
    </div>
  );
}
