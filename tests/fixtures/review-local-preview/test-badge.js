// test-badge.js — Node + jsdom harness for the review-local preview viewer.
//
// Regression test for issue #777: when the per-iteration `iteration_done`
// SSE frame arrives, the header status badge must reflect the AGGREGATE
// gate verdict, not the wrapper-script's exit_code.
//
// Usage: `node test-badge.js` (exits 0 on pass, 1 on any assertion failure).

'use strict';

const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const HTML_PATH = path.resolve(__dirname, '../../../tools/review-local-preview.html');

function fail(msg) {
  console.error('FAIL:', msg);
  process.exit(1);
}

function assertEqual(actual, expected, label) {
  if (actual !== expected) {
    fail(label + ': expected ' + JSON.stringify(expected) + ', got ' + JSON.stringify(actual));
  }
  console.log('  ok  ' + label + ' == ' + JSON.stringify(actual));
}

// The page's IIFE runs at parse time and only attaches the SSE handler
// when window.__PR_NUMBER__ is set. We inject both the EventSource
// stub and the PR number via jsdom's `beforeParse` hook, which fires
// BEFORE any <script> in the document is evaluated.
const html = fs.readFileSync(HTML_PATH, 'utf8');
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => fail('jsdom error: ' + e.message));
const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  virtualConsole: vc,
  beforeParse(window) {
    // Make the page's `new EventSource(url)` construct call our mock.
    window.EventSource = function MockEventSource(url) {
      this.url = url;
      this.readyState = 0;
      this.onmessage = null;
      this.onerror = null;
      window.__lastEventSource = this;
    };
    window.EventSource.OPEN = 1;
    window.EventSource.CLOSED = 2;
    // Pre-set the PR number so the IIFE's `if (window.__PR_NUMBER__)`
    // branch fires and calls startTail() — which is what wires up
    // the EventSource.onmessage handler we want to drive.
    window.__PR_NUMBER__ = 777;
  },
});

const window = dom.window;
const document = window.document;

if (!window.__lastEventSource) {
  fail('page IIFE did not capture the EventSource mock');
}
const es = window.__lastEventSource;
if (typeof es.onmessage !== 'function') {
  fail('EventSource mock has no onmessage handler — page did not wire it');
}

function runScenario(label, gateClasses, exitCode, expectedBadgeText, expectedBadgeCls) {
  console.log('\nScenario: ' + label);
  for (const g of ['review', 'security', 'maintenance']) {
    const row = document.querySelector('[data-gate="' + g + '"]');
    if (!row) fail('gate row missing for ' + g);
    row.className = 'gate ' + gateClasses[g];
  }
  es.onmessage({ data: JSON.stringify({ event: 'iteration_done', exit_code: exitCode }) });
  const status = document.getElementById('status');
  assertEqual(status.textContent, expectedBadgeText, 'badge text');
  assertEqual(status.className, 'status ' + expectedBadgeCls, 'badge class');
}

// Issue #777 repro: all 3 gates approved, wrapper rc=1 -> Approve/done-ok.
runScenario(
  'all 3 gates approved, rc=1 (issue #777 repro)',
  { review: 'approved', security: 'approved', maintenance: 'approved' },
  1,
  'Approve (all gates, rc=1)',
  'done-ok'
);

runScenario(
  'all 3 gates approved, rc=0 (clean run)',
  { review: 'approved', security: 'approved', maintenance: 'approved' },
  0,
  'Approve (all gates, rc=0)',
  'done-ok'
);

runScenario(
  'one gate still running, rc=1 (incomplete -> watching)',
  { review: 'approved', security: 'approved', maintenance: 'running' },
  1,
  'watching (iter done, rc=1, incomplete)',
  'running'
);

runScenario(
  'one gate blocked, rc=1',
  { review: 'approved', security: 'blocked', maintenance: 'approved' },
  1,
  'Blocked (rc=1)',
  'done-err'
);

runScenario(
  'one gate changes-requested, rc=1',
  { review: 'approved', security: 'changes', maintenance: 'approved' },
  1,
  'Changes Requested (rc=1)',
  'done-err'
);

runScenario(
  'blocked AND changes present -> blocked wins',
  { review: 'blocked', security: 'changes', maintenance: 'approved' },
  1,
  'Blocked (rc=1)',
  'done-err'
);

runScenario(
  'one gate failed (parse-failed) -> incomplete',
  { review: 'approved', security: 'failed', maintenance: 'approved' },
  1,
  'watching (iter done, rc=1, incomplete)',
  'running'
);

runScenario(
  'all 3 gates missing, rc=1 (initial state)',
  { review: '', security: '', maintenance: '' },
  1,
  'watching (iter done, rc=1, incomplete)',
  'running'
);

console.log('\nALL SCENARIOS PASSED');
process.exit(0);
