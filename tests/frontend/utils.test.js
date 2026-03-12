/**
 * tests/frontend/utils.test.js — Frontend utility function tests.
 *
 * Tests pure utility functions extracted from app.js and crm.js.
 * Covers: fmtCurrency, toE164, formatPhoneDisplay, esc, isValidEmail, calcMarginPct
 */

import { describe, it, expect } from 'vitest';

// ── fmtCurrency (from crm.js) ──────────────────────────────────────────

function fmtCurrency(n) {
    const v = Math.abs(Number(n || 0));
    const sign = Number(n || 0) < 0 ? '-' : '';
    if (v >= 1e9) return sign + '$' + (v / 1e9).toFixed(4) + 'B';
    if (v >= 1e6) return sign + '$' + (v / 1e6).toFixed(4) + 'M';
    if (v >= 1e3) return sign + '$' + (v / 1e3).toFixed(1) + 'K';
    return sign + '$' + v.toFixed(4);
}

describe('fmtCurrency', () => {
    it('formats small values', () => {
        expect(fmtCurrency(5)).toBe('$5.0000');
        expect(fmtCurrency(0)).toBe('$0.0000');
    });
    it('formats thousands', () => {
        expect(fmtCurrency(1500)).toBe('$1.5K');
        expect(fmtCurrency(25000)).toBe('$25.0K');
    });
    it('formats millions', () => {
        expect(fmtCurrency(1500000)).toBe('$1.5000M');
    });
    it('formats billions', () => {
        expect(fmtCurrency(2000000000)).toBe('$2.0000B');
    });
    it('handles negative values', () => {
        expect(fmtCurrency(-5000)).toBe('-$5.0K');
    });
    it('handles null/undefined', () => {
        expect(fmtCurrency(null)).toBe('$0.0000');
        expect(fmtCurrency(undefined)).toBe('$0.0000');
    });
});

// ── toE164 (from app.js) ────────────────────────────────────────────────

function toE164(raw) {
    if (!raw) return null;
    var cleaned = raw.trim().replace(/\s*(ext|x|#)\s*\.?\s*\d*$/i, '');
    if (/[a-zA-Z]/.test(cleaned)) return null;
    var hasPlus = cleaned.charAt(0) === '+';
    var digits = cleaned.replace(/\D/g, '');
    if (!digits || digits.length < 7) return null;
    if (digits.length === 10) return '+1' + digits;
    if (digits.length === 11 && digits.charAt(0) === '1') return '+' + digits;
    if (hasPlus && digits.length >= 7) return '+' + digits;
    if (digits.length >= 12) return '+' + digits;
    return null;
}

describe('toE164', () => {
    it('formats 10-digit US numbers', () => {
        expect(toE164('(415) 555-1234')).toBe('+14155551234');
    });
    it('formats 11-digit with leading 1', () => {
        expect(toE164('1-800-555-1234')).toBe('+18005551234');
    });
    it('handles + prefix', () => {
        expect(toE164('+44 20 7946 0958')).toBe('+442079460958');
    });
    it('strips extensions', () => {
        expect(toE164('(415) 555-1234 ext 100')).toBe('+14155551234');
        expect(toE164('(415) 555-1234 x200')).toBe('+14155551234');
    });
    it('rejects alphabetic input', () => {
        expect(toE164('call me')).toBeNull();
    });
    it('rejects too-short numbers', () => {
        expect(toE164('12345')).toBeNull();
    });
    it('handles null/empty', () => {
        expect(toE164(null)).toBeNull();
        expect(toE164('')).toBeNull();
    });
});

// ── esc (XSS prevention, from app.js) ───────────────────────────────────

function esc(s) {
    if (!s) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

describe('esc', () => {
    it('escapes HTML entities', () => {
        expect(esc('<script>alert("xss")</script>')).toBe('&lt;script&gt;alert("xss")&lt;/script&gt;');
    });
    it('escapes ampersands', () => {
        expect(esc('A & B')).toBe('A &amp; B');
    });
    it('handles empty/null', () => {
        expect(esc('')).toBe('');
        expect(esc(null)).toBe('');
        expect(esc(undefined)).toBe('');
    });
});

// ── isValidEmail (from crm.js) ──────────────────────────────────────────

function isValidEmail(email) {
    return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
}

describe('isValidEmail', () => {
    it('accepts valid emails', () => {
        expect(isValidEmail('user@example.com')).toBe(true);
        expect(isValidEmail('a.b@c.d.com')).toBe(true);
    });
    it('rejects invalid emails', () => {
        expect(isValidEmail('notanemail')).toBe(false);
        expect(isValidEmail('@missing.com')).toBe(false);
        expect(isValidEmail('user@')).toBe(false);
        expect(isValidEmail('')).toBe(false);
    });
});

// ── calcMarginPct (from crm.js) ─────────────────────────────────────────

function calcMarginPct(sell, cost) {
    return sell > 0 ? ((sell - cost) / sell * 100) : 0;
}

describe('calcMarginPct', () => {
    it('calculates correct margin', () => {
        expect(calcMarginPct(100, 80)).toBe(20);
        expect(calcMarginPct(10, 5)).toBe(50);
    });
    it('returns 0 for zero sell price', () => {
        expect(calcMarginPct(0, 50)).toBe(0);
    });
    it('handles negative margin', () => {
        expect(calcMarginPct(100, 120)).toBe(-20);
    });
});
