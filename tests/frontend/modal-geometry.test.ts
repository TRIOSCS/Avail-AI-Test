/**
 * modal-geometry.test.ts — Vitest unit tests for the resizable-modal geometry math.
 *
 * Covers resize (every edge + corner, min clamps, viewport clamps), move
 * (on-screen clamping), and restore clamping. Pure functions, no DOM needed.
 *
 * Called by: npx vitest run
 * Depends on: vitest, app/static/modal_geometry.js
 */
import { describe, it, expect } from 'vitest';
import {
  clamp,
  resizeGeometry,
  moveGeometry,
  clampToViewport,
  MIN_W,
  MIN_H,
} from '../../app/static/modal_geometry.js';

// A roomy viewport and a centered starting box used by most cases.
const VW = 1600;
const VH = 1000;
const start = { w: 600, h: 400, l: 500, t: 300 }; // right edge 1100, bottom 700

describe('clamp', () => {
  it('bounds below, within, and above', () => {
    expect(clamp(-5, 0, 10)).toBe(0);
    expect(clamp(5, 0, 10)).toBe(5);
    expect(clamp(15, 0, 10)).toBe(10);
  });
});

describe('resizeGeometry — east/south grow the far edges, anchor stays put', () => {
  it('east edge grows width only', () => {
    const g = resizeGeometry(start, 'e', 120, 0, VW, VH);
    expect(g).toEqual({ w: 720, h: 400, l: 500, t: 300 });
  });
  it('south edge grows height only', () => {
    const g = resizeGeometry(start, 's', 0, 80, VW, VH);
    expect(g).toEqual({ w: 600, h: 480, l: 500, t: 300 });
  });
  it('se corner grows both', () => {
    const g = resizeGeometry(start, 'se', 100, 100, VW, VH);
    expect(g).toEqual({ w: 700, h: 500, l: 500, t: 300 });
  });
});

describe('resizeGeometry — west/north move the near edge while resizing', () => {
  it('west edge dragged left widens and moves left', () => {
    const g = resizeGeometry(start, 'w', -100, 0, VW, VH);
    expect(g).toEqual({ w: 700, h: 400, l: 400, t: 300 });
  });
  it('north edge dragged up grows and moves up', () => {
    const g = resizeGeometry(start, 'n', 0, -60, VW, VH);
    expect(g).toEqual({ w: 600, h: 460, l: 500, t: 240 });
  });
  it('nw corner grows both and repositions top-left', () => {
    const g = resizeGeometry(start, 'nw', -50, -50, VW, VH);
    expect(g).toEqual({ w: 650, h: 450, l: 450, t: 250 });
  });
});

describe('resizeGeometry — minimum size clamps', () => {
  it('cannot shrink below MIN_W from the east edge', () => {
    const g = resizeGeometry(start, 'e', -10000, 0, VW, VH);
    expect(g.w).toBe(MIN_W);
  });
  it('cannot shrink below MIN_H from the south edge', () => {
    const g = resizeGeometry(start, 's', 0, -10000, VW, VH);
    expect(g.h).toBe(MIN_H);
  });
  it('west-edge shrink clamps to MIN_W and pins left at the right edge', () => {
    const g = resizeGeometry(start, 'w', 10000, 0, VW, VH);
    expect(g.w).toBe(MIN_W);
    // right edge (l + w) is preserved at start.l + start.w = 1100
    expect(g.l + g.w).toBe(start.l + start.w);
  });
});

describe('resizeGeometry — viewport clamps (cannot grow off-screen)', () => {
  it('east edge cannot exceed the right viewport edge', () => {
    const g = resizeGeometry(start, 'e', 10000, 0, VW, VH);
    expect(g.l + g.w).toBe(VW); // 500 + 1100
    expect(g.w).toBe(VW - start.l);
  });
  it('south edge cannot exceed the bottom viewport edge', () => {
    const g = resizeGeometry(start, 's', 0, 10000, VW, VH);
    expect(g.t + g.h).toBe(VH);
  });
  it('west edge cannot widen past the left viewport edge (l never < 0)', () => {
    const g = resizeGeometry(start, 'w', -10000, 0, VW, VH);
    expect(g.l).toBe(0);
    expect(g.w).toBe(start.l + start.w); // grew to fill from x=0 to the right edge
  });
});

describe('moveGeometry', () => {
  it('moves by the delta and keeps size', () => {
    const g = moveGeometry(start, 40, -25, VW, VH);
    expect(g).toEqual({ w: 600, h: 400, l: 540, t: 275 });
  });
  it('clamps to the top-left corner', () => {
    const g = moveGeometry(start, -10000, -10000, VW, VH);
    expect(g).toEqual({ w: 600, h: 400, l: 0, t: 0 });
  });
  it('clamps to the bottom-right corner', () => {
    const g = moveGeometry(start, 10000, 10000, VW, VH);
    expect(g).toEqual({ w: 600, h: 400, l: VW - 600, t: VH - 400 });
  });
});

describe('minimum-size floor — raised min-height (#461 review fix d)', () => {
  it('keeps MIN_H/MIN_W above the sliver thresholds so a modal stays usable', () => {
    // The #461 adversarial review raised MIN_H off 240 (where a shrunk panel clipped its
    // header + the New-Requisition customer-picker dropdown) to a usable floor. Guard the
    // value so a future edit can't silently collapse a modal back to an unusable sliver.
    expect(MIN_H).toBeGreaterThanOrEqual(360);
    expect(MIN_W).toBeGreaterThanOrEqual(320);
  });
});

describe('clampToViewport — restore onto a smaller screen', () => {
  it('leaves a fitting geometry untouched', () => {
    expect(clampToViewport(start, VW, VH)).toEqual(start);
  });
  it('shrinks an over-wide geometry and pulls it on-screen', () => {
    const saved = { w: 3000, h: 2000, l: 2800, t: 1900 }; // saved on a huge monitor
    const g = clampToViewport(saved, 1280, 800);
    expect(g.w).toBe(1280 - 16); // capped to viewport minus margin
    expect(g.h).toBe(800 - 16);
    // origin was off the bottom-right, so it pins flush to the available edge
    expect(g.l).toBe(1280 - g.w);
    expect(g.t).toBe(800 - g.h);
    expect(g.l + g.w).toBeLessThanOrEqual(1280);
    expect(g.t + g.h).toBeLessThanOrEqual(800);
  });
});
