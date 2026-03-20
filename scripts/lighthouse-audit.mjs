#!/usr/bin/env node
/**
 * Lighthouse performance audit for AvailAI.
 * Runs headless Chrome against the live app and reports scores.
 * Called by: npm run test:lighthouse
 * Depends on: lighthouse, running app server
 */

import lighthouse from 'lighthouse';
import { launch } from 'chrome-launcher';

const BASE_URL = process.env.LIGHTHOUSE_URL || 'http://127.0.0.1:8000';

const PAGES = [
  { name: 'Login Page', path: '/' },
  { name: 'App Shell', path: '/v2' },
  { name: 'Requisitions', path: '/v2/requisitions' },
  { name: 'Vendors', path: '/v2/vendors' },
  { name: 'Companies', path: '/v2/companies' },
  { name: 'Materials', path: '/v2/materials' },
  { name: 'Search', path: '/v2/search' },
  { name: 'Settings', path: '/v2/settings' },
];

const THRESHOLDS = {
  performance: 60,
  accessibility: 80,
  'best-practices': 80,
  seo: 70,
};

async function auditPage(chrome, name, url) {
  console.log(`\n--- Auditing: ${name} (${url}) ---`);

  const result = await lighthouse(url, {
    port: chrome.port,
    output: 'json',
    onlyCategories: ['performance', 'accessibility', 'best-practices', 'seo'],
    formFactor: 'desktop',
    screenEmulation: { disabled: true },
    throttling: { cpuSlowdownMultiplier: 1 },
  });

  const categories = result.lhr.categories;
  const scores = {};
  let allPass = true;

  for (const [key, threshold] of Object.entries(THRESHOLDS)) {
    const score = Math.round((categories[key]?.score || 0) * 100);
    scores[key] = score;
    const pass = score >= threshold;
    if (!pass) allPass = false;
    const icon = pass ? '✓' : '✗';
    console.log(`  ${icon} ${key}: ${score}/100 (threshold: ${threshold})`);
  }

  return { name, url, scores, pass: allPass };
}

async function main() {
  let chrome;
  try {
    chrome = await launch({
      chromeFlags: ['--headless', '--no-sandbox', '--disable-gpu'],
    });

    const results = [];
    for (const page of PAGES) {
      const result = await auditPage(chrome, page.name, `${BASE_URL}${page.path}`);
      results.push(result);
    }

    console.log('\n=== Lighthouse Summary ===');
    let anyFail = false;
    for (const r of results) {
      const icon = r.pass ? '✓' : '✗';
      console.log(`${icon} ${r.name}: perf=${r.scores.performance} a11y=${r.scores.accessibility} bp=${r.scores['best-practices']} seo=${r.scores.seo}`);
      if (!r.pass) anyFail = true;
    }

    if (anyFail) {
      console.log('\nSome pages failed thresholds.');
      process.exit(1);
    } else {
      console.log('\nAll pages passed.');
    }
  } finally {
    if (chrome) await chrome.kill();
  }
}

main().catch((err) => {
  console.error('Lighthouse audit failed:', err.message);
  process.exit(1);
});
