/**
 * avatar_cropper.js — the profile-photo avatarCropper Alpine.data component:
 * canvas pan/zoom/pinch crop, 512x512 export, and the multipart upload to
 * /api/user/avatar with HX-Trigger bridging.
 *
 * What calls it: imported for side effects by app/static/htmx_app.js (Vite entry).
 * Depends on: alpinejs, ./globals.js (csrfToken).
 */

import Alpine from 'alpinejs';
import { csrfToken } from './globals.js';

/**
 * avatarCropper — vanilla Alpine + <canvas> face-centering cropper for the
 * profile-photo uploader (settings/profile.html "Profile photo" card).
 *
 * Flow: the user picks a file → openFile() loads it into an off-DOM Image and
 * opens a circular crop viewport. The image is painted to a <canvas> the same
 * pixel size as the round viewport; the user PANS by dragging (mouse/touch) and
 * ZOOMS with a slider, the mouse wheel, or a two-finger pinch. Pan/zoom are
 * clamped so the image always fully covers the circle (no gaps). On Save we
 * re-render the visible circular region into a 512×512 export canvas and
 * toBlob() a JPEG (PNG when the source has alpha), guaranteed ≤ 2 MB because the
 * export is downscaled to 512². The blob is POSTed as multipart to the existing
 * /api/user/avatar route (canvas output is a real JPEG/PNG, so it clears the
 * server's magic-byte gate); the route's HX-Trigger {avatarUpdated} refreshes
 * the card's preview via the existing onResult() handler.
 *
 * Geometry: `scale` maps source pixels → viewport pixels; `minScale` is the
 * cover scale (largest of width/height ratios) so the image can never be smaller
 * than the circle. `tx`/`ty` are the top-left offset of the scaled image inside
 * the square viewport, clamped to [viewport - scaledSize, 0] on each axis.
 *
 * Called by: settings/profile.html (profile-photo card, x-data="avatarCropper(...)").
 * Depends on: Alpine.js; the browser Canvas 2D + FileReader APIs; the existing
 *             /api/user/avatar POST. No third-party cropper library — the math is
 *             ~1 screen and a dependency would only add bundle weight.
 */
Alpine.data('avatarCropper', (postUrl, maxBytes) => ({
  postUrl: postUrl || '/api/user/avatar',
  maxBytes: maxBytes || 2 * 1024 * 1024,
  open: false,
  busy: false,
  error: '',
  // Source image + geometry (all in CSS px of the square viewport).
  img: null,
  hasAlpha: false,
  viewport: 288, // canvas + circle edge length in CSS px (md+); reset on open to actual size
  scale: 1,
  minScale: 1,
  maxScale: 1,
  tx: 0,
  ty: 0,
  // Slider position 0..100 maps linearly across [minScale, maxScale].
  zoomPct: 0,
  // Drag state.
  _dragging: false,
  _lastX: 0,
  _lastY: 0,
  // Pinch state.
  _pinchDist: 0,

  init() {
    // Repaint whenever Alpine notices a geometry change without us calling draw()
    // directly (e.g. the zoom slider's x-model write). Cheap; canvas only redraws
    // when open with an image.
    this.$watch('zoomPct', () => this.applyZoomFromSlider());
  },

  // ── File selection ────────────────────────────────────────────────────
  openFile(evt) {
    this.error = '';
    const file = evt.target.files && evt.target.files[0];
    // Let the user re-pick the same file later (change won't fire on identical value).
    evt.target.value = '';
    if (!file) return;
    if (!/^image\/(png|jpeg|webp|gif)$/.test(file.type)) {
      this.error = 'Choose a PNG, JPEG, WEBP, or GIF image.';
      return;
    }
    const reader = new FileReader();
    reader.onload = (e) => this.loadDataUrl(e.target.result, file.type);
    reader.onerror = () => { this.error = "That file couldn't be read. Try another."; };
    reader.readAsDataURL(file);
  },

  loadDataUrl(dataUrl, mime) {
    const image = new Image();
    image.onload = () => {
      if (!image.naturalWidth || !image.naturalHeight) {
        this.error = "That image couldn't be opened. Try another.";
        return;
      }
      this.img = image;
      // PNG/WEBP/GIF can carry transparency → export PNG to preserve it; JPEG never does.
      this.hasAlpha = mime !== 'image/jpeg';
      this.open = true;
      // Measure the actual rendered viewport once the modal is in the DOM.
      this.$nextTick(() => {
        const c = this.$refs.canvas;
        if (c) this.viewport = c.clientWidth || this.viewport;
        this.resetGeometry();
        this.draw();
      });
    };
    image.onerror = () => { this.error = "That image couldn't be opened. Try another."; };
    image.src = dataUrl;
  },

  // ── Geometry ──────────────────────────────────────────────────────────
  resetGeometry() {
    const v = this.viewport;
    const iw = this.img.naturalWidth;
    const ih = this.img.naturalHeight;
    // Cover scale: smallest scale that still fills the square in both axes.
    this.minScale = Math.max(v / iw, v / ih);
    this.maxScale = this.minScale * 4; // allow up to 4× tighter crop
    this.scale = this.minScale;
    this.zoomPct = 0;
    // Center the image in the viewport.
    this.tx = (v - iw * this.scale) / 2;
    this.ty = (v - ih * this.scale) / 2;
    this.clamp();
  },

  clamp() {
    const v = this.viewport;
    const sw = this.img.naturalWidth * this.scale;
    const sh = this.img.naturalHeight * this.scale;
    // Keep the scaled image fully covering the viewport: offset in [v - size, 0].
    this.tx = Math.min(0, Math.max(v - sw, this.tx));
    this.ty = Math.min(0, Math.max(v - sh, this.ty));
  },

  applyZoomFromSlider() {
    if (!this.img || !this.open) return;
    const target = this.minScale + (this.maxScale - this.minScale) * (this.zoomPct / 100);
    this.zoomTo(target, this.viewport / 2, this.viewport / 2);
  },

  // Zoom toward a focal point (cx, cy) in viewport px so the pixel under the
  // cursor/pinch stays put.
  zoomTo(nextScale, cx, cy) {
    const s = Math.min(this.maxScale, Math.max(this.minScale, nextScale));
    if (s === this.scale) { this.draw(); return; }
    const ratio = s / this.scale;
    this.tx = cx - (cx - this.tx) * ratio;
    this.ty = cy - (cy - this.ty) * ratio;
    this.scale = s;
    this.clamp();
    this.draw();
  },

  // ── Pointer / touch / wheel handlers ──────────────────────────────────
  pointerDown(e) {
    if (!this.img) return;
    this._dragging = true;
    const p = this._pt(e);
    this._lastX = p.x;
    this._lastY = p.y;
  },

  pointerMove(e) {
    if (!this._dragging || !this.img) return;
    e.preventDefault();
    const p = this._pt(e);
    this.tx += p.x - this._lastX;
    this.ty += p.y - this._lastY;
    this._lastX = p.x;
    this._lastY = p.y;
    this.clamp();
    this.draw();
  },

  pointerUp() {
    this._dragging = false;
  },

  wheel(e) {
    if (!this.img) return;
    e.preventDefault();
    const rect = this.$refs.canvas.getBoundingClientRect();
    const cx = e.clientX - rect.left;
    const cy = e.clientY - rect.top;
    const factor = e.deltaY < 0 ? 1.08 : 1 / 1.08;
    this.zoomTo(this.scale * factor, cx, cy);
    this.syncSlider();
  },

  touchStart(e) {
    if (!this.img) return;
    if (e.touches.length === 2) {
      this._dragging = false;
      this._pinchDist = this._dist(e.touches);
    } else if (e.touches.length === 1) {
      this.pointerDown(e.touches[0]);
    }
  },

  touchMove(e) {
    if (!this.img) return;
    if (e.touches.length === 2) {
      e.preventDefault();
      const rect = this.$refs.canvas.getBoundingClientRect();
      const cx = (e.touches[0].clientX + e.touches[1].clientX) / 2 - rect.left;
      const cy = (e.touches[0].clientY + e.touches[1].clientY) / 2 - rect.top;
      const d = this._dist(e.touches);
      if (this._pinchDist > 0) this.zoomTo(this.scale * (d / this._pinchDist), cx, cy);
      this._pinchDist = d;
      this.syncSlider();
    } else if (e.touches.length === 1 && this._dragging) {
      this.pointerMove(e.touches[0]);
    }
  },

  touchEnd(e) {
    this._pinchDist = 0;
    if (!e.touches || e.touches.length === 0) this.pointerUp();
  },

  // Push the current scale back onto the slider (after wheel/pinch) without
  // re-triggering applyZoomFromSlider (guarded by the equality check in zoomTo).
  syncSlider() {
    const span = this.maxScale - this.minScale;
    this.zoomPct = span > 0 ? Math.round(((this.scale - this.minScale) / span) * 100) : 0;
  },

  _pt(e) {
    const rect = this.$refs.canvas.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  },

  _dist(touches) {
    const dx = touches[0].clientX - touches[1].clientX;
    const dy = touches[0].clientY - touches[1].clientY;
    return Math.hypot(dx, dy);
  },

  // ── Render ────────────────────────────────────────────────────────────
  draw() {
    const canvas = this.$refs.canvas;
    if (!canvas || !this.img) return;
    const v = this.viewport;
    const dpr = window.devicePixelRatio || 1;
    // Backing store at device resolution for a crisp preview; CSS size stays v.
    if (canvas.width !== Math.round(v * dpr)) {
      canvas.width = Math.round(v * dpr);
      canvas.height = Math.round(v * dpr);
    }
    const ctx = canvas.getContext('2d');
    ctx.save();
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, v, v);
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(this.img, this.tx, this.ty, this.img.naturalWidth * this.scale, this.img.naturalHeight * this.scale);
    ctx.restore();
  },

  // ── Save ──────────────────────────────────────────────────────────────
  save() {
    if (!this.img || this.busy) return;
    this.busy = true;
    this.error = '';
    const OUT = 512;
    const out = document.createElement('canvas');
    out.width = OUT;
    out.height = OUT;
    const ctx = out.getContext('2d');
    ctx.imageSmoothingQuality = 'high';
    // The export maps the SAME source region the circle shows: viewport px → 512 px.
    const k = OUT / this.viewport;
    ctx.drawImage(
      this.img,
      this.tx * k,
      this.ty * k,
      this.img.naturalWidth * this.scale * k,
      this.img.naturalHeight * this.scale * k,
    );
    const type = this.hasAlpha ? 'image/png' : 'image/jpeg';
    const ext = this.hasAlpha ? 'png' : 'jpg';
    out.toBlob(
      (blob) => {
        if (!blob) { this.busy = false; this.error = 'Could not process the image. Try another.'; return; }
        if (blob.size > this.maxBytes) {
          // 512² PNG of a photographic image can rarely exceed 2 MB — fall back to JPEG.
          out.toBlob((jpeg) => this.upload(jpeg, 'image/jpeg', 'jpg'), 'image/jpeg', 0.9);
          return;
        }
        this.upload(blob, type, ext);
      },
      type,
      0.9,
    );
  },

  upload(blob, type, ext) {
    if (!blob) { this.busy = false; this.error = 'Could not process the image. Try another.'; return; }
    if (blob.size > this.maxBytes) {
      this.busy = false;
      this.error = 'The cropped image is still over 2 MB. Try a smaller photo.';
      return;
    }
    const form = new FormData();
    form.append('file', new File([blob], 'avatar.' + ext, { type }));
    // P5.2 NOTE: intentionally stays on raw fetch() (not postJSON/htmx.ajax) — the
    // body is a binary Blob wrapped in FormData (multipart file upload), not JSON;
    // htmx's values/json-enc pipeline (formDataFromObject + JSON.stringify) is built
    // for JSON-shaped payloads and isn't a clean fit for carrying a File/Blob through.
    // Raw fetch (not htmx), so the CSRF double-submit header must be added by hand —
    // starlette_csrf 403s any session POST without it, before the route ever runs.
    fetch(this.postUrl, {
      method: 'POST',
      body: form,
      headers: { 'HX-Request': 'true', 'x-csrftoken': csrfToken() },
      credentials: 'same-origin',
    })
      .then((resp) => {
        if (!resp.ok) {
          const fallback = 'Upload failed (HTTP ' + resp.status + '). Try again.';
          return resp.json().then(
            (b) => { throw new Error((b && b.error) || fallback); },
            () => { throw new Error(fallback); },
          );
        }
        // The route returns HX-Trigger {avatarUpdated:{filename}, showToast:{...}}.
        // We're not in an HTMX swap, so bridge those events ourselves: fire the global
        // showToast and a kebab-case `avatar-updated` the profile card listens for
        // (@avatar-updated.window) to refresh its preview. Kebab so Alpine's lowercased
        // attribute matches; HTMX's own camelCase `avatarUpdated` only reaches DOM
        // addEventListener, not Alpine's @-binding.
        let filename = null;
        const trigger = resp.headers.get('HX-Trigger');
        if (trigger) {
          try {
            const events = JSON.parse(trigger);
            filename = (events.avatarUpdated || {}).filename || null;
            const toast = events.showToast;
            // The global toast bridge listens on document.body (htmx_app.js) — dispatch there.
            if (toast) document.body.dispatchEvent(new CustomEvent('showToast', { detail: toast }));
          } catch { /* non-JSON trigger — ignore */ }
        }
        window.dispatchEvent(new CustomEvent('avatar-updated', { detail: { filename } }));
        this.close();
      })
      .catch((err) => { this.error = err.message || 'Upload failed. Try again.'; })
      .finally(() => { this.busy = false; });
  },

  close() {
    this.open = false;
    this.busy = false;
    this.img = null;
    this.error = '';
  },
}));
