/**
 * modal_geometry.js — pure geometry helpers for the resizable/movable modal.
 *
 * What it does: stateless math for dragging a modal panel — resize from any
 *   edge/corner, move within the viewport, and clamp a stored geometry back
 *   onto the current viewport on restore. No DOM, no storage, no Alpine, so it
 *   is unit-testable in isolation (tests/frontend/modal-geometry.test.ts).
 * What calls it: app/static/htmx_app.js — Alpine.data('resizableModal').
 * Depends on: nothing.
 */

// Smallest the user can shrink a modal to (px). Below this, shrinking stops.
export const MIN_W = 360;
export const MIN_H = 240;

/** Clamp `v` into the inclusive range [lo, hi]. */
export function clamp(v, lo, hi) {
  return Math.min(Math.max(v, lo), hi);
}

/**
 * Resize from a drag on `edge` (a subset of the chars n/s/e/w).
 * `start` is the panel box {w,h,l,t} captured at pointer-down; `dx`/`dy` are the
 * pointer deltas; `vw`/`vh` the viewport. The opposite edge stays anchored, so
 * dragging a west/north edge moves `l`/`t` as the size changes. Returns a new
 * box clamped to [minW,minH] and to the viewport bounds.
 */
export function resizeGeometry(start, edge, dx, dy, vw, vh, minW = MIN_W, minH = MIN_H) {
  let { w, h, l, t } = start;
  if (edge.includes('e')) w = clamp(start.w + dx, minW, vw - start.l);
  if (edge.includes('s')) h = clamp(start.h + dy, minH, vh - start.t);
  if (edge.includes('w')) {
    w = clamp(start.w - dx, minW, start.l + start.w);
    l = start.l + (start.w - w);
  }
  if (edge.includes('n')) {
    h = clamp(start.h - dy, minH, start.t + start.h);
    t = start.t + (start.h - h);
  }
  return { w, h, l, t };
}

/**
 * Move the panel by (dx,dy), keeping it fully on-screen. Size is unchanged.
 * `start` is the box at pointer-down; `vw`/`vh` the viewport.
 */
export function moveGeometry(start, dx, dy, vw, vh) {
  return {
    w: start.w,
    h: start.h,
    l: clamp(start.l + dx, 0, vw - start.w),
    t: clamp(start.t + dy, 0, vh - start.h),
  };
}

/**
 * Clamp a previously-stored geometry onto the current viewport (with a `margin`
 * gap so it never sits flush to an edge). Used on restore so a size saved on a
 * large monitor never opens off-screen on a smaller one.
 */
export function clampToViewport(geom, vw, vh, margin = 16) {
  const w = Math.min(geom.w, vw - margin);
  const h = Math.min(geom.h, vh - margin);
  return { w, h, l: clamp(geom.l, 0, vw - w), t: clamp(geom.t, 0, vh - h) };
}
