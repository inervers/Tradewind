import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, apiUrl } from "../api";
import ConfirmDialog from "../components/ConfirmDialog";
import { Elapsed, FadeIn, useToast } from "../components/effects";
import { CheckIcon, PenIcon, PhotoIcon, TrashIcon, UploadIcon, XIcon } from "../components/Icons";
import { resumeTask, watchTask } from "../tasks";
import type {
  ConfigState, PhotoLibraryPhoto, PhotoLibraryStore, PhotoScanResult, PhotoTaskState,
} from "../types";

type ScanPhoto = { filename: string; data_base64: string };
type PendingPhoto = ScanPhoto & { id: string };
type PreviewPhoto = { url: string; title: string };
type PendingMenu = { photoId: string; x: number; y: number };

const STANDARD_LIMIT = 8;
const PROVIDER_LABELS: Record<string, string> = { glm: "智谱 GLM", qwen: "阿里云 Qwen", volc: "火山豆包" };

function fileToDataUrl(file: File | Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error("读取照片失败"));
    reader.readAsDataURL(file);
  });
}

function photoKey(storeId: string, photoId: string): string {
  return `${storeId}:${photoId}`;
}

function ResultBadge({ result }: { result?: PhotoScanResult }) {
  if (!result) return <span className="photo-status neutral">待识别</span>;
  if (result.error) return <span className="photo-status error">识别失败</span>;
  if (result.has_device) return <span className="photo-status device">发现仪器</span>;
  return <span className="photo-status neutral">未发现仪器</span>;
}

export default function PhotosTab() {
  const [mode, setMode] = useState<"library" | "upload">("library");
  const [config, setConfig] = useState<ConfigState>({ has_key: false });
  const [stores, setStores] = useState<PhotoLibraryStore[]>([]);
  const [activeStoreId, setActiveStoreId] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [pending, setPending] = useState<PendingPhoto[]>([]);
  const [task, setTask] = useState<PhotoTaskState | null>(null);
  const [taskId, setTaskId] = useState("");
  const [busy, setBusy] = useState(false);
  const [loadingLibrary, setLoadingLibrary] = useState(true);
  const [preview, setPreview] = useState<PreviewPhoto | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [storeDeleteOpen, setStoreDeleteOpen] = useState(false);
  const [storeBusy, setStoreBusy] = useState(false);
  const [editingStore, setEditingStore] = useState(false);
  const [storeName, setStoreName] = useState("");
  const [pendingMenu, setPendingMenu] = useState<PendingMenu | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const unsubscribeRef = useRef<(() => void) | null>(null);
  const toast = useToast();

  const loadLibrary = useCallback(async () => {
    setLoadingLibrary(true);
    try {
      const data = await api.photos.library();
      setStores(data.stores);
      setActiveStoreId((current) => data.stores.some((store) => store.store_id === current)
        ? current
        : data.stores[0]?.store_id || "");
      setSelected((current) => {
        const valid = new Set(data.stores.flatMap((store) => store.photos.map((photo) => photoKey(store.store_id, photo.photo_id))));
        return new Set([...current].filter((key) => valid.has(key)));
      });
    } catch {
      toast.push("照片库读取失败，请确认后端已启动", "error");
    } finally {
      setLoadingLibrary(false);
    }
  }, [toast.push]);

  const handleTask = useCallback((next: PhotoTaskState) => {
    setTask(next);
    setBusy(next.status === "running");
    if (next.status === "error") toast.push(next.error || "照片识别失败", "error");
    if (next.status === "done") toast.push(`识别完成：${next.results.filter((item) => item.has_device).length} 张发现仪器`, "ok");
  }, [toast.push]);

  useEffect(() => {
    api.getConfig().then(setConfig).catch(() => {});
    loadLibrary();
    const unsubscribe = resumeTask("photos", handleTask, api.photos.task);
    unsubscribeRef.current = unsubscribe;
    return () => unsubscribeRef.current?.();
  }, [handleTask, loadLibrary]);

  useEffect(() => {
    const closeMenu = () => setPendingMenu(null);
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setPendingMenu(null);
    };
    document.addEventListener("click", closeMenu);
    document.addEventListener("scroll", closeMenu, true);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("click", closeMenu);
      document.removeEventListener("scroll", closeMenu, true);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, []);

  const activeStore = stores.find((store) => store.store_id === activeStoreId) || null;
  const selectedLibraryPhotos = useMemo(() => {
    const items: { store: PhotoLibraryStore; photo: PhotoLibraryPhoto }[] = [];
    for (const store of stores) {
      for (const photo of store.photos) {
        if (selected.has(photoKey(store.store_id, photo.photo_id))) items.push({ store, photo });
      }
    }
    return items;
  }, [selected, stores]);
  const allActiveSelected = Boolean(activeStore?.photos.length)
    && activeStore!.photos.every((photo) => selected.has(photoKey(activeStore!.store_id, photo.photo_id)));

  const togglePhoto = useCallback((key: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
  }, []);

  const toggleActiveStore = useCallback(() => {
    if (!activeStore) return;
    setSelected((current) => {
      const next = new Set(current);
      const keys = activeStore.photos.map((photo) => photoKey(activeStore.store_id, photo.photo_id));
      if (keys.every((key) => next.has(key))) keys.forEach((key) => next.delete(key));
      else keys.forEach((key) => next.add(key));
      return next;
    });
  }, [activeStore]);

  const beginTask = useCallback(async (images: ScanPhoto[]) => {
    if (!config.vision?.configured) {
      toast.push("请先到设置页配置视觉识别 API Key", "error");
      return;
    }
    if (!images.length) {
      toast.push("请先选择照片", "error");
      return;
    }
    setBusy(true);
    setTask(null);
    try {
      const response = await api.photos.start(images.slice(0, STANDARD_LIMIT));
      if (!response.task_id) throw new Error(response.error || "启动失败");
      setTaskId(response.task_id);
      unsubscribeRef.current?.();
      unsubscribeRef.current = watchTask("photos", response.task_id, handleTask, api.photos.task);
    } catch (error) {
      setBusy(false);
      toast.push(error instanceof Error ? error.message : "启动失败", "error");
    }
  }, [config.vision?.configured, handleTask, toast.push]);

  const scanLibrarySelection = useCallback(async () => {
    if (!selectedLibraryPhotos.length) return;
    if (selectedLibraryPhotos.length > STANDARD_LIMIT) {
      toast.push(`标准模式每批最多 ${STANDARD_LIMIT} 张，请减少选择后再识别`, "error");
      return;
    }
    setBusy(true);
    try {
      const images = await Promise.all(selectedLibraryPhotos.map(async ({ store, photo }) => {
        const response = await fetch(apiUrl(photo.url));
        if (!response.ok) throw new Error(`读取 ${photo.filename} 失败`);
        return {
          filename: `${store.name}_${photo.filename}`,
          data_base64: await fileToDataUrl(await response.blob()),
        };
      }));
      await beginTask(images);
    } catch (error) {
      setBusy(false);
      toast.push(error instanceof Error ? error.message : "读取照片失败", "error");
    }
  }, [beginTask, selectedLibraryPhotos, toast.push]);

  const onFiles = useCallback(async (files: FileList | null) => {
    if (!files?.length) return;
    const selectedFiles = Array.from(files);
    try {
      const next = await Promise.all(selectedFiles.map(async (file) => ({
        id: `${file.name}:${file.size}:${file.lastModified}`,
        filename: file.name,
        data_base64: await fileToDataUrl(file),
      })));
      setPending((current) => {
        const existing = new Set(current.map((photo) => photo.id));
        const unique = next.filter((photo) => !existing.has(photo.id));
        const combined = [...current, ...unique];
        if (combined.length > STANDARD_LIMIT) {
          toast.push(`标准模式最多保留 ${STANDARD_LIMIT} 张，其余照片未加入`, "info");
        } else if (unique.length < next.length) {
          toast.push("重复选择的照片已自动跳过", "info");
        }
        return combined.slice(0, STANDARD_LIMIT);
      });
      setTask(null);
    } catch {
      toast.push("照片读取失败", "error");
    } finally {
      if (fileRef.current) fileRef.current.value = "";
    }
  }, [toast.push]);

  const removePendingPhoto = useCallback((photoId: string) => {
    setPending((current) => current.filter((photo) => photo.id !== photoId));
    setPendingMenu(null);
    setTask(null);
  }, []);

  const beginRenameStore = useCallback(() => {
    if (!activeStore) return;
    setStoreName(activeStore.name);
    setEditingStore(true);
  }, [activeStore]);

  const saveStoreName = useCallback(async () => {
    if (!activeStore || !storeName.trim()) {
      toast.push("店铺名称不能为空", "error");
      return;
    }
    setStoreBusy(true);
    try {
      const response = await api.photos.renameStore(activeStore.store_id, storeName.trim());
      if (!response.ok) throw new Error(response.error || "重命名失败");
      setEditingStore(false);
      setActiveStoreId(response.store_id || "");
      setSelected(new Set());
      await loadLibrary();
      toast.push("店铺名称已更新", "ok");
    } catch (error) {
      toast.push(error instanceof Error ? error.message : "重命名失败", "error");
    } finally {
      setStoreBusy(false);
    }
  }, [activeStore, loadLibrary, storeName, toast.push]);

  const removeActiveStore = useCallback(async () => {
    if (!activeStore) return;
    setStoreBusy(true);
    try {
      const response = await api.photos.removeStore(activeStore.store_id);
      if (!response.ok) throw new Error(response.error || "删除店铺失败");
      setStoreDeleteOpen(false);
      setSelected(new Set());
      await loadLibrary();
      toast.push(response.warning || `已删除店铺及 ${response.removed} 张照片`, response.warning ? "info" : "ok");
    } catch (error) {
      toast.push(error instanceof Error ? error.message : "删除店铺失败", "error");
    } finally {
      setStoreBusy(false);
    }
  }, [activeStore, loadLibrary, toast.push]);

  const removeLibraryPhotos = useCallback(async () => {
    setDeleteBusy(true);
    try {
      const response = await api.photos.remove(selectedLibraryPhotos.map(({ store, photo }) => ({
        store_id: store.store_id, photo_id: photo.photo_id,
      })));
      toast.push(`已删除 ${response.removed} 张照片`, "ok");
      setDeleteOpen(false);
      setSelected(new Set());
      await loadLibrary();
    } catch (error) {
      toast.push(error instanceof Error ? error.message : "删除失败", "error");
    } finally {
      setDeleteBusy(false);
    }
  }, [loadLibrary, selectedLibraryPhotos, toast.push]);

  const cancelTask = useCallback(async () => {
    if (!taskId) return;
    try {
      const response = await api.photos.cancel(taskId);
      if (!response.ok) toast.push(response.error || "取消失败", "error");
    } catch {
      toast.push("取消失败", "error");
    }
  }, [taskId, toast.push]);

  const deviceResults = task?.results.filter((result) => result.has_device) || [];
  const provider = config.vision?.provider || "glm";

  return (
    <>
      <FadeIn>
        <div className="page-head">
          <div className="page-title">照片库 <span className="seal">图片抽查</span></div>
          <div className="page-desc">按店铺查看、整理爬虫照片，或导入本地照片筛选仪器；标准模式每批最多 {STANDARD_LIMIT} 张。</div>
        </div>

        <div className="photo-mode-row">
          <div className="photo-mode-tabs" role="tablist" aria-label="照片来源">
            <button className={mode === "library" ? "active" : ""} onClick={() => setMode("library")}>爬虫图库</button>
            <button className={mode === "upload" ? "active" : ""} onClick={() => setMode("upload")}>导入照片</button>
          </div>
          <div className={`photo-vision-summary${config.vision?.configured ? " ready" : ""}`}>
            <span className="status-dot ok" />
            {config.vision?.configured
              ? `${PROVIDER_LABELS[config.vision?.effective_provider || provider] || config.vision?.effective_provider || provider} · ${config.vision?.effective_model || config.vision?.model || "默认模型"}`
              : "视觉模型未配置"}
          </div>
        </div>

        {mode === "library" ? (
          <div className="photo-library-layout">
            <aside className="card photo-store-panel">
              <div className="card-title"><span>店铺</span><span className="badge badge-neutral">{stores.length}</span></div>
              <div className="photo-store-list">
                {stores.map((store) => (
                  <button key={store.store_id} className={`photo-store-item${store.store_id === activeStoreId ? " active" : ""}`} onClick={() => setActiveStoreId(store.store_id)}>
                    <span title={store.name}>{store.name}</span><b>{store.count}</b>
                  </button>
                ))}
                {!loadingLibrary && !stores.length && <div className="photo-store-empty">暂无爬虫照片</div>}
              </div>
            </aside>

            <section className="card photo-grid-panel">
              <div className="photo-toolbar">
                <div>
                  {editingStore ? (
                    <div className="photo-store-editor">
                      <input className="input" value={storeName} maxLength={80} autoFocus onChange={(event) => setStoreName(event.target.value)} onKeyDown={(event) => {
                        if (event.key === "Enter") saveStoreName();
                        if (event.key === "Escape") setEditingStore(false);
                      }} />
                      <button className="btn btn-primary btn-sm" disabled={storeBusy} onClick={saveStoreName}>保存</button>
                      <button className="btn btn-ghost btn-sm" disabled={storeBusy} onClick={() => setEditingStore(false)}>取消</button>
                    </div>
                  ) : <div className="card-title" style={{ marginBottom: 2 }}><span>{activeStore?.name || "照片"}</span></div>}
                  <div className="field-hint">{selected.size ? `已选择 ${selected.size} 张` : "点击勾选照片进行管理或识别"}</div>
                </div>
                <div className="photo-toolbar-actions">
                  {activeStore && !editingStore ? <button className="btn btn-ghost btn-sm" disabled={busy} onClick={beginRenameStore}><PenIcon size={14} /> 编辑店名</button> : null}
                  {activeStore && !editingStore ? <button className="btn btn-ghost btn-sm photo-store-delete" disabled={busy} onClick={() => setStoreDeleteOpen(true)}><TrashIcon size={14} /> 删除店铺</button> : null}
                  {activeStore?.photos.length ? <button className="btn btn-ghost btn-sm" onClick={toggleActiveStore}>{allActiveSelected ? "取消本店全选" : "本店全选"}</button> : null}
                  <button className="btn btn-danger btn-sm" disabled={!selected.size || busy} onClick={() => setDeleteOpen(true)}><TrashIcon size={15} /> 删除</button>
                  <button className="btn btn-primary btn-sm" disabled={!selected.size || selected.size > STANDARD_LIMIT || busy} onClick={scanLibrarySelection}>
                    {busy ? <>识别中 <Elapsed active /></> : `识别已选（${selected.size}/${STANDARD_LIMIT}）`}
                  </button>
                </div>
              </div>
              {activeStore?.photos.length ? (
                <div className="photo-grid">
                  {activeStore.photos.map((photo) => {
                    const key = photoKey(activeStore.store_id, photo.photo_id);
                    const checked = selected.has(key);
                    return (
                      <article key={photo.photo_id} className={`photo-tile${checked ? " selected" : ""}`}>
                        <button className="photo-image-button" onClick={() => setPreview({ url: apiUrl(photo.url), title: `${activeStore.name} · ${photo.filename}` })}>
                          <img src={apiUrl(photo.url)} alt={`${activeStore.name} ${photo.filename}`} loading="lazy" />
                        </button>
                        <button className="photo-check" aria-label={checked ? "取消选择" : "选择照片"} onClick={() => togglePhoto(key)}>
                          {checked ? <CheckIcon size={15} /> : null}
                        </button>
                        <div className="photo-tile-foot"><span title={photo.filename}>{photo.filename}</span><small>{Math.max(1, Math.round(photo.size / 1024))} KB</small></div>
                      </article>
                    );
                  })}
                </div>
              ) : (
                <div className="empty photo-empty"><PhotoIcon size={30} /><div className="empty-title">还没有可查看的照片</div><div className="empty-desc">在爬虫页开启“保存识别照片”后运行 Maps 爬虫，照片会按店铺出现在这里。</div></div>
              )}
            </section>
          </div>
        ) : (
          <section className="card photo-upload-panel">
            <div className="photo-upload-bar">
              <div>
                <div className="card-title"><span>导入照片筛选</span></div>
                <div className="field-hint">支持 JPG、PNG、WebP；为控制费用，每批最多 {STANDARD_LIMIT} 张，每张只识别一次。</div>
              </div>
              <div className="photo-toolbar-actions">
                <input ref={fileRef} hidden type="file" accept="image/*" multiple onChange={(event) => onFiles(event.target.files)} />
                <button className="btn btn-ghost" onClick={() => fileRef.current?.click()}><UploadIcon size={16} /> 选择照片</button>
                <button className="btn btn-primary" disabled={!pending.length || busy} onClick={() => beginTask(pending)}>
                  {busy ? <>识别中 <Elapsed active /></> : `开始识别（${pending.length}/${STANDARD_LIMIT}）`}
                </button>
              </div>
            </div>
            {pending.length ? (
              <div className="photo-grid">
                {pending.map((photo, index) => {
                  const result = task?.results.find((item) => item.filename === photo.filename);
                  return (
                    <article key={photo.id} className={`photo-tile${result?.has_device ? " has-device" : ""}${result?.error ? " has-error" : ""}`} onContextMenu={(event) => {
                      event.preventDefault();
                      setPendingMenu({ photoId: photo.id, x: event.clientX, y: event.clientY });
                    }}>
                      <button className="photo-image-button" onClick={() => setPreview({ url: photo.data_base64, title: photo.filename })}>
                        <img src={photo.data_base64} alt={photo.filename} />
                      </button>
                      <ResultBadge result={result} />
                      <div className="photo-tile-foot">
                        <span title={photo.filename}>{photo.filename}</span>
                        <small>{result?.provider
                          ? `${PROVIDER_LABELS[result.provider] || result.provider} · ${result.model}`
                          : "右键可删除"}</small>
                      </div>
                    </article>
                  );
                })}
              </div>
            ) : (
              <button className="photo-dropzone" onClick={() => fileRef.current?.click()}><UploadIcon size={28} /><span>选择需要筛选的照片</span><small>一次最多 {STANDARD_LIMIT} 张，更多照片请分批处理</small></button>
            )}
          </section>
        )}

        {task && (
          <section className="card photo-results-panel">
            <div className="photo-results-head">
              <div className="card-title"><span>识别结果</span></div>
              <div className="photo-progress-copy">{task.status === "running" ? `正在处理 ${task.done}/${task.total}` : `已处理 ${task.done}/${task.total}`}</div>
              {task.status === "running" && <button className="btn btn-danger btn-sm" onClick={cancelTask}>停止识别</button>}
            </div>
            {deviceResults.length ? (
              <div className="photo-device-list">
                {deviceResults.map((result, index) => (
                  <button key={`${result.filename}-${index}`} className="photo-device-row" onClick={() => result.preview_url && setPreview({ url: apiUrl(result.preview_url), title: result.filename })}>
                    <span><b>{result.filename}</b><small>{result.provider ? `${PROVIDER_LABELS[result.provider] || result.provider} · ${result.model}` : ""}</small></span>
                    <span>{result.devices.map((device) => `${device.device}${device.brand ? `（${device.brand}）` : ""}`).join("、")}</span>
                    <span className="badge badge-ok">{Math.round(result.confidence * 100)}%</span>
                  </button>
                ))}
              </div>
            ) : task.status === "running" ? (
              <div className="field-hint">模型正在逐张检查，切换到其他 Tab 不会中断。</div>
            ) : (
              <div className="field-hint">本批照片未发现明确可见的仪器；识别失败的图片可查看状态后重新提交。</div>
            )}
            {task.results.some((item) => item.error) && (
              <details className="photo-failed-details"><summary>查看失败项（{task.results.filter((item) => item.error).length}）</summary>{task.results.filter((item) => item.error).map((item) => <div key={item.filename}>{item.filename}：{item.error}</div>)}</details>
            )}
          </section>
        )}
      </FadeIn>

      {deleteOpen && <ConfirmDialog title={`删除选中的 ${selected.size} 张照片？`} desc="照片会从本地爬虫目录永久删除，无法从客户名单中恢复。" busy={deleteBusy} onCancel={() => setDeleteOpen(false)} onConfirm={removeLibraryPhotos} />}
      {storeDeleteOpen && activeStore && <ConfirmDialog title={`删除店铺“${activeStore.name}”？`} desc={`将永久删除该店铺目录中的 ${activeStore.count} 张照片，其他店铺不受影响。`} busy={storeBusy} onCancel={() => setStoreDeleteOpen(false)} onConfirm={removeActiveStore} />}
      {pendingMenu && createPortal(
        <button className="photo-context-delete" style={{ left: pendingMenu.x, top: pendingMenu.y }} onClick={(event) => {
          event.stopPropagation();
          removePendingPhoto(pendingMenu.photoId);
        }}><TrashIcon size={14} /> 删除这张照片</button>, document.body,
      )}
      {preview && createPortal(
        <div className="photo-preview-mask" onClick={() => setPreview(null)}>
          <div className="photo-preview-card" role="dialog" aria-modal="true" aria-label={preview.title} onClick={(event) => event.stopPropagation()}>
            <button className="photo-preview-close" onClick={() => setPreview(null)} aria-label="关闭预览"><XIcon size={19} /></button>
            <img src={preview.url} alt={preview.title} />
            <div>{preview.title}</div>
          </div>
        </div>, document.body,
      )}
      {toast.el}
    </>
  );
}
