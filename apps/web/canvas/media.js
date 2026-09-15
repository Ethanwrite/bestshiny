/**
 * Media for node cards: thumbnails and full media for asset ids, fetched with
 * the session cookie and handed out as object URLs.
 *
 * Mirrors app.js `resolveAssetThumbnail` / `resolveAssetMedia`: galleries read
 * the derived thumbnail, the viewer and the video player read the original
 * through the authenticated storage route. Each asset resolves once; `clear()`
 * revokes every URL (sign-out), and a resolution that lands after a clear
 * revokes its own URL instead of leaking it into the next session.
 */
import { API_BASE, api, fetchBlobUrl } from "./api.js";

function storagePath(asset) {
  const url = asset?.public_url || "";
  const viaStorage = Boolean(asset?.storage_key) && (!url || (() => {
    try { return new URL(url, location.href).pathname.includes("/v1/storage/"); }
    catch (_error) { return false; }
  })());
  if (!viaStorage) return null;
  return `/v1/storage/${asset.storage_key.split("/").map(encodeURIComponent).join("/")}`;
}

export class MediaCache {
  constructor() {
    this.thumbs = new Map();
    this.full = new Map();
    this.urls = new Set();
    this.generation = 0;
  }

  _track(result, generation) {
    if (!result) return null;
    if (result.revocable) {
      if (generation !== this.generation) {
        URL.revokeObjectURL(result.url);
        return null;
      }
      this.urls.add(result.url);
    }
    return result;
  }

  _remember(map, key, promise) {
    map.set(key, promise);
    // A failure is not remembered: the next card to ask tries again.
    promise.then((value) => { if (!value && map.get(key) === promise) map.delete(key); })
      .catch(() => { if (map.get(key) === promise) map.delete(key); });
    return promise;
  }

  /** `{url, mime}` of an asset's thumbnail, or its original when none can be derived. */
  thumbnail(assetId) {
    if (!assetId) return Promise.resolve(null);
    if (this.thumbs.has(assetId)) return this.thumbs.get(assetId);
    const generation = this.generation;
    const promise = (async () => {
      try {
        const blob = await fetchBlobUrl(`/v1/assets/${encodeURIComponent(assetId)}/thumbnail`);
        return this._track({ ...blob, revocable: true }, generation);
      } catch (_error) {
        return this.media(assetId);
      }
    })();
    return this._remember(this.thumbs, assetId, promise);
  }

  /** `{url, mime}` of the original media. */
  media(assetId) {
    if (!assetId) return Promise.resolve(null);
    if (this.full.has(assetId)) return this.full.get(assetId);
    const generation = this.generation;
    const promise = (async () => {
      const asset = await api.asset(assetId).catch(() => null);
      if (!asset || generation !== this.generation) return null;
      const path = storagePath(asset);
      if (path) {
        const blob = await fetchBlobUrl(path).catch(() => null);
        if (!blob) return null;
        return this._track({ url: blob.url, mime: asset.mime_type || blob.mime, revocable: true }, generation);
      }
      if (!asset.public_url) return null;
      const url = new URL(asset.public_url, location.href);
      // A relative API address is served under the same prefix as every call.
      const href = asset.public_url.startsWith("/v1/") ? `${API_BASE}${asset.public_url}` : url.href;
      return { url: href, mime: asset.mime_type || "", revocable: false };
    })();
    return this._remember(this.full, assetId, promise);
  }

  clear() {
    this.generation += 1;
    for (const url of this.urls) URL.revokeObjectURL(url);
    this.urls.clear();
    this.thumbs.clear();
    this.full.clear();
  }
}
