#!/usr/bin/env python3
"""
Standalone bot-detection profile server.

Serves a single page at GET / that runs a comprehensive JS bot-detection
suite directly in the browser and renders results live. Server-side request
headers are embedded into the page at serve time. No storage, no other routes.

Usage:
    python scripts/bot_detect_server.py [--port 8899] [--host 0.0.0.0]

Point a detonation at http://<bridge-ip>:<port>/ to profile the agent.
"""
from __future__ import annotations

import argparse
import json

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
import uvicorn

app = FastAPI(title="Bot Detection")

# Python replaces the literal string "__SERVER_HEADERS__" (with surrounding quotes)
# with the JSON-serialised header dict before serving, so the page gets a real
# JS object without any template-literal / format-string conflicts.
_DETECTION_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Bot Detection Profile</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: monospace; background: #0d1117; color: #c9d1d9;
           padding: 1.5rem; font-size: 13px; }
    h1 { font-size: 1.1rem; color: #58a6ff; margin-bottom: 1rem; }
    h2 { font-size: 0.85rem; color: #8b949e; margin: 1.4rem 0 0.5rem;
         padding-bottom: 0.3rem; border-bottom: 1px solid #21262d; }
    #verdict { padding: 0.75rem 1rem; border-radius: 6px; font-size: 0.95rem;
               font-weight: bold; margin-bottom: 1rem;
               background: #21262d; color: #8b949e; }
    table { width: 100%; border-collapse: collapse; margin-bottom: 0.5rem; }
    th { background: #161b22; color: #8b949e; padding: 4px 8px; text-align: left; }
    td { padding: 3px 8px; border-bottom: 1px solid #21262d;
         vertical-align: top; word-break: break-all; }
    tr.pass  td.status { color: #3fb950; }
    tr.fail-red    { background: rgba(248,81,73,0.08); }
    tr.fail-red    td.status { color: #f85149; font-weight: bold; }
    tr.fail-yellow { background: rgba(210,153,34,0.08); }
    tr.fail-yellow td.status { color: #d2a520; }
    tr.info  td.status { color: #484f58; }
  </style>
</head>
<body>
<h1>Bot Detection Profile</h1>
<div id="verdict">Initializing…</div>

<h2>Server-Side Request Headers</h2>
<table>
  <thead><tr><th>Header</th><th>Value</th></tr></thead>
  <tbody id="hdr-body"></tbody>
</table>

<h2>Detection Checks</h2>
<table>
  <thead><tr><th>Category</th><th>Check</th><th>Value</th><th class="status">Result</th><th>Notes</th></tr></thead>
  <tbody id="check-body"></tbody>
</table>

<script>
"use strict";
// Injected by the server at serve time.
const SERVER_HEADERS = "__SERVER_HEADERS__";

// ── utilities ─────────────────────────────────────────────────────────────────

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

const tbody   = document.getElementById("check-body");
const verdict = document.getElementById("verdict");
const rows    = [];

function addRow(r) {
  rows.push(r);
  const cls = r.sev === "INFO"
    ? "info"
    : r.ok ? "pass" : r.sev === "RED" ? "fail-red" : "fail-yellow";
  const status = r.sev === "INFO" ? "INFO" : r.ok ? "PASS" : ("FAIL-" + r.sev);
  tbody.insertAdjacentHTML("beforeend",
    "<tr class=\"" + cls + "\">" +
    "<td>" + esc(r.cat) + "</td>" +
    "<td>" + esc(r.name) + "</td>" +
    "<td>" + esc(r.val) + "</td>" +
    "<td class=\"status\">" + status + "</td>" +
    "<td>" + esc(r.notes) + "</td>" +
    "</tr>");
  updateVerdict();
}

function updateVerdict() {
  const scored  = rows.filter(function(r) { return r.sev !== "INFO"; });
  const reds    = scored.filter(function(r) { return !r.ok && r.sev === "RED"; }).length;
  const yellows = scored.filter(function(r) { return !r.ok && r.sev === "YELLOW"; }).length;
  const total   = scored.length;
  const passed  = scored.filter(function(r) { return r.ok; }).length;
  if (total === 0) return;
  if (reds > 0) {
    verdict.style.background = "#3d1f1f"; verdict.style.color = "#f85149";
    verdict.textContent = "BOT — " + reds + " definitive tell(s), " + yellows +
      " suspicious | " + passed + "/" + total + " passed";
  } else if (yellows > 0) {
    verdict.style.background = "#2d2a1a"; verdict.style.color = "#d2a520";
    verdict.textContent = "SUSPICIOUS — " + yellows + " indicator(s) | " + passed + "/" + total + " passed";
  } else {
    verdict.style.background = "#1a2d1f"; verdict.style.color = "#3fb950";
    verdict.textContent = "CLEAN — " + passed + "/" + total + " checks passed";
  }
}

// sev: "RED" | "YELLOW" | "INFO"
function chk(cat, name, getVal, okFn, sev, notesFn) {
  var val, ok, notes;
  try {
    val   = getVal();
    ok    = okFn(val);
    notes = notesFn ? notesFn(val, ok) : "";
  } catch(e) {
    val = "ERROR"; ok = false; notes = e.message;
    sev = sev || "YELLOW";
  }
  addRow({ cat: cat, name: name, val: String(val == null ? "" : val),
           ok: !!ok, sev: sev || "YELLOW", notes: notes || "" });
}

function inf(cat, name, getVal) {
  var val;
  try { val = getVal(); } catch(e) { val = "ERROR: " + e.message; }
  addRow({ cat: cat, name: name, val: String(val == null ? "" : val),
           ok: true, sev: "INFO", notes: "" });
}

// async check — returns a Promise pushed into asyncJobs
function achk(cat, name, fn, sev) {
  return fn().then(function(r) {
    addRow({ cat: cat, name: name, val: String(r.val == null ? "" : r.val),
             ok: !!r.ok, sev: sev || "YELLOW", notes: r.notes || "" });
  }).catch(function(e) {
    addRow({ cat: cat, name: name, val: "ERROR", ok: false,
             sev: sev || "YELLOW", notes: e.message });
  });
}

// ── render server headers ─────────────────────────────────────────────────────

(function() {
  var hb = document.getElementById("hdr-body");
  for (var k in SERVER_HEADERS) {
    if (!Object.prototype.hasOwnProperty.call(SERVER_HEADERS, k)) continue;
    hb.insertAdjacentHTML("beforeend",
      "<tr><td>" + esc(k) + "</td><td>" + esc(SERVER_HEADERS[k]) + "</td></tr>");
  }
})();

verdict.textContent = "Running checks…";

// ═══════════════════════════════════════════════════════════════════════════════
// SYNCHRONOUS CHECKS
// ═══════════════════════════════════════════════════════════════════════════════

// ── 1. navigator.webdriver ───────────────────────────────────────────────────
chk("navigator", "webdriver",
  function() { return navigator.webdriver; },
  function(v) { return v === undefined || v === false; },
  "RED",
  function(v, ok) { return ok ? "undefined — not exposed" : "EXPOSED: " + v; }
);

// ── 2. plugins ───────────────────────────────────────────────────────────────
chk("navigator", "plugins.length > 0",
  function() { return navigator.plugins.length; },
  function(v) { return v > 0; },
  "YELLOW",
  function(v, ok) { return ok ? v + " plugin(s)" : "0 — real browsers have plugins"; }
);

chk("navigator", "plugins[0] has name",
  function() { return navigator.plugins[0] ? navigator.plugins[0].name : "(none)"; },
  function(v) { return v !== "(none)" && v.length > 0; },
  "YELLOW",
  function(v) { return v; }
);

inf("navigator", "all plugin names",
  function() {
    var names = [];
    for (var i = 0; i < navigator.plugins.length; i++) {
      names.push(navigator.plugins[i].name);
    }
    return names.join(", ") || "(none)";
  }
);

// ── 3. mimeTypes ─────────────────────────────────────────────────────────────
chk("navigator", "mimeTypes.length > 0",
  function() { return navigator.mimeTypes.length; },
  function(v) { return v > 0; },
  "YELLOW",
  function(v, ok) { return ok ? v + " type(s)" : "0"; }
);

// ── 4. languages ─────────────────────────────────────────────────────────────
chk("navigator", "languages non-empty",
  function() { return navigator.languages ? navigator.languages.length : 0; },
  function(v) { return v > 0; },
  "RED",
  function(v, ok) { return ok ? v + " language(s)" : "empty array — automation tell"; }
);

inf("navigator", "languages value",
  function() { return JSON.stringify(navigator.languages); }
);

chk("navigator", "languages matches Accept-Language",
  function() {
    var js  = ((navigator.languages && navigator.languages[0]) || "").toLowerCase().split("-")[0];
    var hdr = (SERVER_HEADERS["accept-language"] || "").toLowerCase().split(/[,;]/)[0].trim().split("-")[0];
    return { js: js, hdr: hdr, match: js.length > 0 && hdr.length > 0 && js === hdr };
  },
  function(v) { return v.match; },
  "YELLOW",
  function(v, ok) {
    return ok ? "JS \"" + v.js + "\" = header \"" + v.hdr + "\""
              : "MISMATCH — JS: \"" + v.js + "\" vs header: \"" + v.hdr + "\"";
  }
);

// ── 5. hardwareConcurrency ───────────────────────────────────────────────────
chk("navigator", "hardwareConcurrency in [2,64]",
  function() { return navigator.hardwareConcurrency; },
  function(v) { return Number.isInteger(v) && v >= 2 && v <= 64; },
  "YELLOW",
  function(v) { return v + " cores"; }
);

// ── 6. deviceMemory ──────────────────────────────────────────────────────────
inf("navigator", "deviceMemory",
  function() {
    return typeof navigator.deviceMemory !== "undefined"
      ? navigator.deviceMemory + " GB" : "API absent";
  }
);

// ── 7. platform ──────────────────────────────────────────────────────────────
chk("navigator", "platform non-Linux",
  function() { return navigator.platform; },
  function(v) { return v.length > 0 && !/linux/i.test(v); },
  "YELLOW",
  function(v) { return "\"" + v + "\""; }
);

inf("navigator", "platform value",
  function() { return "\"" + navigator.platform + "\""; }
);

// ── 8. maxTouchPoints ────────────────────────────────────────────────────────
inf("navigator", "maxTouchPoints",
  function() { return navigator.maxTouchPoints; }
);

// ── 9. pdfViewerEnabled ──────────────────────────────────────────────────────
inf("navigator", "pdfViewerEnabled",
  function() {
    return typeof navigator.pdfViewerEnabled !== "undefined"
      ? String(navigator.pdfViewerEnabled) : "API absent";
  }
);

// ── 10. cookieEnabled / onLine ───────────────────────────────────────────────
chk("navigator", "cookieEnabled",
  function() { return navigator.cookieEnabled; },
  function(v) { return v === true; },
  "YELLOW",
  function(v) { return String(v); }
);

chk("navigator", "onLine",
  function() { return navigator.onLine; },
  function(v) { return v === true; },
  "YELLOW",
  function(v) { return String(v); }
);

// ── 11. userAgent coherence ──────────────────────────────────────────────────
chk("navigator", "userAgent contains Chrome",
  function() { return navigator.userAgent; },
  function(v) { return /Chrome\/\d+/.test(v); },
  "YELLOW",
  function(v) { return v.slice(0, 110); }
);

chk("navigator", "JS userAgent matches request User-Agent header",
  function() {
    var js  = navigator.userAgent;
    var hdr = SERVER_HEADERS["user-agent"] || "";
    return { match: js === hdr, js: js.slice(0, 60), hdr: hdr.slice(0, 60) };
  },
  function(v) { return v.match; },
  "YELLOW",
  function(v, ok) {
    return ok ? "match" : "MISMATCH  JS: " + v.js + "  HDR: " + v.hdr;
  }
);

inf("navigator", "userAgentData.platform",
  function() {
    return navigator.userAgentData ? navigator.userAgentData.platform : "userAgentData absent";
  }
);

// ── 12. document state ───────────────────────────────────────────────────────
chk("document", "visibilityState = visible",
  function() { return document.visibilityState; },
  function(v) { return v === "visible"; },
  "YELLOW",
  function(v) { return v; }
);

inf("document", "hasFocus",
  function() { return String(document.hasFocus()); }
);

// ── 13. window.chrome ────────────────────────────────────────────────────────
chk("chrome", "window.chrome present",
  function() { return typeof window.chrome; },
  function(v) { return v === "object" && window.chrome !== null; },
  "RED",
  function(v, ok) { return ok ? "present" : "ABSENT (type=" + v + ")"; }
);

chk("chrome", "chrome.app present",
  function() { return typeof (window.chrome && window.chrome.app); },
  function(v) { return v === "object"; },
  "RED",
  function(v, ok) { return ok ? "present" : "absent (type=" + v + ")"; }
);

chk("chrome", "chrome.runtime present",
  function() { return typeof (window.chrome && window.chrome.runtime); },
  function(v) { return v === "object"; },
  "RED",
  function(v, ok) { return ok ? "present" : "absent (type=" + v + ")"; }
);

chk("chrome", "chrome.csi is function",
  function() { return typeof (window.chrome && window.chrome.csi); },
  function(v) { return v === "function"; },
  "YELLOW",
  function(v) { return v; }
);

chk("chrome", "chrome.loadTimes is function",
  function() { return typeof (window.chrome && window.chrome.loadTimes); },
  function(v) { return v === "function"; },
  "YELLOW",
  function(v) { return v; }
);

// Real Chrome's csi() returns {startE, onloadT, pageT, tran}
chk("chrome", "csi() returns object",
  function() {
    try { return typeof window.chrome.csi(); }
    catch(e) { return "threw: " + e.message; }
  },
  function(v) { return v === "object"; },
  "YELLOW",
  function(v, ok) {
    return ok ? "ok" : "returned " + v + " — real Chrome returns {startE, onloadT, pageT, tran}";
  }
);

// Real Chrome's loadTimes() returns a rich navigation-timing object
chk("chrome", "loadTimes() returns object",
  function() {
    try { return typeof window.chrome.loadTimes(); }
    catch(e) { return "threw: " + e.message; }
  },
  function(v) { return v === "object"; },
  "YELLOW",
  function(v, ok) {
    return ok ? "ok" : "returned " + v + " — real Chrome returns navigation timing object";
  }
);

// ── 14. WebGL ────────────────────────────────────────────────────────────────
(function() {
  var canvas = document.createElement("canvas");
  var gl = canvas.getContext("webgl") || canvas.getContext("experimental-webgl");

  if (!gl) {
    addRow({ cat: "webgl", name: "WebGL available", val: "false",
             ok: false, sev: "YELLOW", notes: "WebGL context creation failed" });
    return;
  }

  var ext      = gl.getExtension("WEBGL_debug_renderer_info");
  var vendor   = ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL)   : null;
  var renderer = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null;
  var vmPat    = /swiftshader|llvmpipe|vmware|virtualbox|microsoft basic render/i;

  chk("webgl", "vendor not a VM renderer",
    function() { return vendor; },
    function(v) { return v !== null && !vmPat.test(v); },
    "RED",
    function(v) {
      return v === null ? "(WEBGL_debug_renderer_info unavailable)" : "\"" + v + "\"";
    }
  );

  chk("webgl", "renderer not a VM renderer",
    function() { return renderer; },
    function(v) { return v !== null && !vmPat.test(v); },
    "RED",
    function(v) {
      return v === null ? "(WEBGL_debug_renderer_info unavailable)" : "\"" + v + "\"";
    }
  );

  inf("webgl", "vendor string",   function() { return vendor || "(none)"; });
  inf("webgl", "renderer string", function() { return renderer || "(none)"; });

  chk("webgl", "WebGL2 available",
    function() { return !!document.createElement("canvas").getContext("webgl2"); },
    function(v) { return v === true; },
    "YELLOW",
    function(v) { return v ? "yes" : "no"; }
  );

  inf("webgl", "supported extension count",
    function() { return String(gl.getSupportedExtensions().length); }
  );

  var vp = gl.getShaderPrecisionFormat(gl.VERTEX_SHADER, gl.HIGH_FLOAT);
  inf("webgl", "vertex highp precision",
    function() { return vp ? vp.precision + " bits" : "unknown"; }
  );
})();

// ── 15. Function prototype integrity ─────────────────────────────────────────
// Uses Function.prototype.toString.call(fn) which reads the internal [[SourceText]]
// slot in V8 — bypasses any fn.toString override. Patched wrappers will leak
// their source body instead of returning "[native code]".
(function() {
  function isNative(fn) {
    if (!fn) return false;
    try {
      return /\[native code\]/.test(Function.prototype.toString.call(fn));
    } catch(e) { return false; }
  }

  var targets = [
    ["navigator.permissions.query",
      function() { return navigator.permissions && navigator.permissions.query; }],
    ["HTMLCanvasElement.prototype.toDataURL",
      function() { return HTMLCanvasElement.prototype.toDataURL; }],
    ["CanvasRenderingContext2D.prototype.getImageData",
      function() { return CanvasRenderingContext2D.prototype.getImageData; }],
    ["WebGLRenderingContext.prototype.getParameter",
      function() {
        return typeof WebGLRenderingContext !== "undefined"
          ? WebGLRenderingContext.prototype.getParameter : null;
      }],
    ["WebGL2RenderingContext.prototype.getParameter",
      function() {
        return typeof WebGL2RenderingContext !== "undefined"
          ? WebGL2RenderingContext.prototype.getParameter : null;
      }],
    ["AudioBuffer.prototype.getChannelData",
      function() {
        return typeof AudioBuffer !== "undefined"
          ? AudioBuffer.prototype.getChannelData : null;
      }],
    ["AnalyserNode.prototype.getFloatFrequencyData",
      function() {
        return typeof AnalyserNode !== "undefined"
          ? AnalyserNode.prototype.getFloatFrequencyData : null;
      }],
    ["HTMLCanvasElement.prototype.getContext",
      function() { return HTMLCanvasElement.prototype.getContext; }],
    ["Document.prototype.createElement",
      function() { return Document.prototype.createElement; }],
    ["window.fetch",
      function() { return window.fetch; }],
    ["XMLHttpRequest.prototype.open",
      function() { return XMLHttpRequest.prototype.open; }],
    ["Function.prototype.toString itself",
      function() { return Function.prototype.toString; }],
    ["Object.defineProperty",
      function() { return Object.defineProperty; }],
  ];

  for (var i = 0; i < targets.length; i++) {
    (function(label, getFn) {
      chk("integrity", label,
        function() {
          var fn = getFn();
          if (fn === null || fn === undefined) return "(API unavailable)";
          try { return Function.prototype.toString.call(fn); }
          catch(e) { return "toString threw: " + e.message; }
        },
        function(v) {
          var fn = getFn();
          return fn === null || fn === undefined || isNative(fn);
        },
        "RED",
        function(v, ok) {
          if (v === "(API unavailable)") return "API unavailable";
          return ok ? "native code ✓" : "PATCHED — source visible: " + v.slice(0, 120);
        }
      );
    })(targets[i][0], targets[i][1]);
  }

  // navigator.webdriver descriptor — real Chrome has it as a getter returning undefined
  // after the flag, or it may not exist at all. Check the descriptor type.
  chk("integrity", "navigator.webdriver descriptor type",
    function() {
      var d = Object.getOwnPropertyDescriptor(navigator, "webdriver");
      if (!d) return "not own property (inherited or absent)";
      if (d.get) return "getter: " + Function.prototype.toString.call(d.get).slice(0, 80);
      return "value: " + d.value;
    },
    function(v) {
      // Real automated Chrome without stealth has webdriver=true as a plain value.
      // Either absent or a getter returning undefined are both "interesting".
      // We mark this as info-only since any presence is already caught above.
      return true;
    },
    "INFO",
    function(v) { return v; }
  );
})();

// ── 16. Automation globals / leaked markers ──────────────────────────────────
(function() {
  var markers = [
    // WebDriver / Selenium
    ["window", "__webdriver_evaluate"],
    ["window", "__selenium_evaluate"],
    ["window", "__fxdriver_evaluate"],
    ["window", "__driver_unwrapped"],
    ["window", "__webdriver_script_fn"],
    ["window", "__driver_evaluate"],
    ["window", "__selenium_unwrapped"],
    ["window", "__fxdriver_unwrapped"],
    ["window", "webdriver"],
    // PhantomJS
    ["window", "_phantom"],
    ["window", "callPhantom"],
    ["window", "phantom"],
    // Puppeteer / Node globals that leak into page context
    ["window", "Buffer"],
    ["window", "emit"],
    ["window", "spawn"],
    // Nightmare.js
    ["window", "__nightmare"],
    // Automation controller (Chromium internal)
    ["window", "domAutomation"],
    ["window", "domAutomationController"],
    // Old CDP leak (Chrome < 90)
    ["document", "$cdc_asdjflasutopfhvcZLmcfl_"],
    // Selenium IDE
    ["window", "_Selenium_IDE_Recorder"],
    ["window", "_selenium"],
    // Playwright-specific
    ["window", "__playwright_target"],
    ["window", "__pwInitScripts"],
    ["window", "__playwrightFunctionMap"],
    // Generic
    ["window", "awesomium"],
    ["window", "geb"],
    ["window", "_helium_test"],
    ["window", "__webdriverFunc"],
  ];

  for (var i = 0; i < markers.length; i++) {
    (function(obj, key) {
      chk("leaks", obj + "." + key,
        function() { return obj === "window" ? window[key] : document[key]; },
        function(v) { return v === undefined; },
        "RED",
        function(v, ok) {
          return ok ? "undefined ✓" : "PRESENT: " + String(v).slice(0, 80);
        }
      );
    })(markers[i][0], markers[i][1]);
  }

  // navigator prototype chain should not be tampered with
  chk("leaks", "navigator.__proto__ === Navigator.prototype",
    function() { return Object.getPrototypeOf(navigator) === Navigator.prototype; },
    function(v) { return v === true; },
    "RED",
    function(v, ok) { return ok ? "prototype chain intact" : "TAMPERED"; }
  );
})();

// ── 17. Window / screen coherence ───────────────────────────────────────────
chk("window", "outerWidth > 0",
  function() { return outerWidth; },
  function(v) { return v > 0; },
  "YELLOW",
  function(v) { return v + "px (0 = headless/no-window tell)"; }
);

chk("window", "outerHeight > 0",
  function() { return outerHeight; },
  function(v) { return v > 0; },
  "YELLOW",
  function(v) { return v + "px"; }
);

chk("window", "outerHeight > innerHeight (browser chrome present)",
  function() { return outerHeight - innerHeight; },
  function(delta) { return delta > 0; },
  "YELLOW",
  function(delta) {
    return "delta=" + delta + "px — 0 means no titlebar/toolbar (headless tell)";
  }
);

chk("window", "screen.colorDepth = 24",
  function() { return screen.colorDepth; },
  function(v) { return v === 24; },
  "YELLOW",
  function(v) { return v + " bits"; }
);

inf("window", "outerWidth × outerHeight",
  function() { return outerWidth + " \xd7 " + outerHeight; }
);

inf("window", "innerWidth × innerHeight",
  function() { return innerWidth + " \xd7 " + innerHeight; }
);

inf("window", "screen.width × screen.height",
  function() { return screen.width + " \xd7 " + screen.height; }
);

inf("window", "devicePixelRatio",
  function() { return window.devicePixelRatio; }
);

// ── 18. CSS media queries ────────────────────────────────────────────────────
inf("media", "prefers-color-scheme",
  function() {
    var mm = window.matchMedia;
    return mm("(prefers-color-scheme: dark)").matches ? "dark"
         : mm("(prefers-color-scheme: light)").matches ? "light"
         : "none/unknown";
  }
);

inf("media", "prefers-reduced-motion",
  function() {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches
      ? "reduce" : "no-preference";
  }
);

inf("media", "forced-colors",
  function() {
    return window.matchMedia("(forced-colors: active)").matches ? "active" : "none";
  }
);

inf("media", "pointer type",
  function() {
    var mm = window.matchMedia;
    return mm("(pointer: fine)").matches ? "fine"
         : mm("(pointer: coarse)").matches ? "coarse" : "none";
  }
);

// ── 19. Performance timer resolution ─────────────────────────────────────────
chk("perf", "timer resolution (distinct values in 5ms)",
  function() {
    var seen = {};
    var t0 = performance.now();
    while (performance.now() - t0 < 5) {
      seen[performance.now().toFixed(4)] = 1;
    }
    return Object.keys(seen).length;
  },
  function(v) { return v >= 3; },
  "YELLOW",
  function(v) {
    return v + " distinct values — very low count suggests reduced timer precision";
  }
);

// ── 20. Intl / timezone ──────────────────────────────────────────────────────
inf("intl", "timezone",
  function() { return Intl.DateTimeFormat().resolvedOptions().timeZone; }
);

inf("intl", "locale",
  function() { return Intl.DateTimeFormat().resolvedOptions().locale; }
);

inf("intl", "UTC offset (minutes)",
  function() { return new Date().getTimezoneOffset(); }
);

// ── 21. Error stack trace format ─────────────────────────────────────────────
inf("runtime", "stack trace format",
  function() {
    try { null.x; } catch(e) {
      return (e.stack || "(no stack)").split("\n").slice(0, 2).join(" | ").slice(0, 120);
    }
    return "(no error thrown)";
  }
);

// ── 22. Storage ──────────────────────────────────────────────────────────────
chk("storage", "localStorage writable",
  function() {
    localStorage.setItem("__bd", "1");
    localStorage.removeItem("__bd");
    return true;
  },
  function(v) { return v === true; },
  "YELLOW",
  function(v) { return v ? "ok" : "failed"; }
);

chk("storage", "sessionStorage writable",
  function() {
    sessionStorage.setItem("__bd", "1");
    sessionStorage.removeItem("__bd");
    return true;
  },
  function(v) { return v === true; },
  "YELLOW",
  function(v) { return v ? "ok" : "failed"; }
);

chk("storage", "indexedDB present",
  function() { return typeof indexedDB; },
  function(v) { return v !== "undefined"; },
  "YELLOW",
  function(v) { return v; }
);

// ── 23. Codec support ────────────────────────────────────────────────────────
inf("codec", "video/webm;codecs=vp9 (MediaRecorder)",
  function() {
    if (typeof MediaRecorder === "undefined") return "MediaRecorder absent";
    return MediaRecorder.isTypeSupported("video/webm;codecs=vp9")
      ? "supported" : "not supported";
  }
);

inf("codec", "video/mp4 (canPlayType)",
  function() {
    return document.createElement("video").canPlayType("video/mp4") || "empty string";
  }
);

// ═══════════════════════════════════════════════════════════════════════════════
// ASYNC CHECKS
// ═══════════════════════════════════════════════════════════════════════════════

var asyncJobs = [];

// ── 24. Permissions ──────────────────────────────────────────────────────────
asyncJobs.push(achk("perm", "notifications state != denied", function() {
  return navigator.permissions.query({ name: "notifications" }).then(function(r) {
    return {
      val: r.state, ok: r.state !== "denied",
      notes: r.state === "denied"
        ? "DENIED — automation default; real unseen users get \"default\""
        : "state: " + r.state
    };
  });
}, "RED"));

asyncJobs.push(achk("perm", "geolocation state", function() {
  return navigator.permissions.query({ name: "geolocation" }).then(function(r) {
    return { val: r.state, ok: true, notes: "(informational)" };
  });
}, "INFO"));

asyncJobs.push(achk("perm", "camera state", function() {
  return navigator.permissions.query({ name: "camera" }).then(function(r) {
    return { val: r.state, ok: true, notes: "(informational)" };
  });
}, "INFO"));

// ── 25. Canvas — pixel stability across two getImageData reads ───────────────
asyncJobs.push(achk("canvas", "getImageData stable (no per-call noise)", function() {
  return new Promise(function(resolve) {
    function djb2(data) {
      var h = 5381;
      for (var i = 0; i < data.length; i++) { h = ((h << 5) + h) ^ data[i]; }
      return h >>> 0;
    }
    var c = document.createElement("canvas");
    c.width = 280; c.height = 60;
    var ctx = c.getContext("2d");
    ctx.textBaseline = "top";
    ctx.font = "bold 14px Arial";
    ctx.fillStyle = "#f0932b";
    ctx.fillRect(100, 2, 100, 30);
    ctx.fillStyle = "#006ba6";
    ctx.fillText("BotDetect", 4, 14);
    ctx.fillStyle = "rgba(80,200,80,0.7)";
    ctx.fillText("FingerprintTest", 6, 38);

    var d1 = ctx.getImageData(0, 0, 280, 60).data;
    var d2 = ctx.getImageData(0, 0, 280, 60).data;
    var h1 = djb2(d1), h2 = djb2(d2);
    var stable = h1 === h2;
    resolve({
      val: stable ? "stable" : "UNSTABLE (h1=" + h1 + ", h2=" + h2 + ")",
      ok: stable,
      notes: stable
        ? "pixel data consistent across reads — no per-call random noise"
        : "pixel data differs between identical reads — randomised noise injection detected"
    });
  });
}, "YELLOW"));

// Canvas fingerprint hash (informational baseline)
asyncJobs.push(achk("canvas", "toDataURL fingerprint (informational)", function() {
  return new Promise(function(resolve) {
    var c = document.createElement("canvas");
    c.width = 220; c.height = 50;
    var ctx = c.getContext("2d");
    ctx.font = "12px Arial";
    ctx.fillStyle = "#cc3300";
    ctx.fillRect(0, 0, 220, 50);
    ctx.fillStyle = "#ffffff";
    ctx.fillText("CanvasFP", 10, 30);
    var url = c.toDataURL("image/png");
    resolve({
      val: url.slice(0, 72) + "…",
      ok: true,
      notes: "hash changes across sessions if noise seed varies"
    });
  });
}, "INFO"));

// ── 26. Audio — two renders of same signal should be byte-identical ──────────
asyncJobs.push(achk("audio", "two OfflineAudioContext renders identical", function() {
  function render() {
    var ctx = new OfflineAudioContext(1, 512, 44100);
    var osc = ctx.createOscillator();
    osc.type = "triangle";
    osc.frequency.value = 10000;
    osc.connect(ctx.destination);
    osc.start(0);
    return ctx.startRendering().then(function(buf) {
      return buf.getChannelData(0);
    });
  }
  return Promise.all([render(), render()]).then(function(results) {
    var d1 = results[0], d2 = results[1];
    var diffs = 0, maxDiff = 0;
    for (var i = 0; i < d1.length; i++) {
      var diff = Math.abs(d1[i] - d2[i]);
      if (diff > 1e-10) diffs++;
      if (diff > maxDiff) maxDiff = diff;
    }
    var stable = diffs === 0;
    return {
      val: stable ? "identical" : diffs + "/512 samples differ (maxΔ=" + maxDiff.toExponential(2) + ")",
      ok: stable,
      notes: stable
        ? "renders deterministic — no inter-render noise detected"
        : "renders differ — noise injected between getChannelData calls (or non-deterministic engine)"
    };
  });
}, "YELLOW"));

// Audio fingerprint sum (informational)
asyncJobs.push(achk("audio", "oscillator fingerprint sum (informational)", function() {
  var ctx = new OfflineAudioContext(1, 4096, 44100);
  var osc = ctx.createOscillator();
  osc.type = "triangle";
  osc.frequency.value = 10000;
  osc.connect(ctx.destination);
  osc.start(0);
  return ctx.startRendering().then(function(buf) {
    var data = buf.getChannelData(0);
    var sum = 0;
    for (var i = 0; i < data.length; i++) sum += Math.abs(data[i]);
    return { val: sum.toFixed(10), ok: true, notes: "compare across environments as baseline" };
  });
}, "INFO"));

// ── 27. Battery API ──────────────────────────────────────────────────────────
asyncJobs.push(achk("battery", "getBattery", function() {
  if (!navigator.getBattery) {
    return Promise.resolve({ val: "API absent", ok: true, notes: "(absent on some platforms/Chrome configs)" });
  }
  return navigator.getBattery().then(function(bat) {
    return {
      val: "level=" + (bat.level * 100).toFixed(0) + "%, charging=" + bat.charging,
      ok: true, notes: "(informational)"
    };
  });
}, "INFO"));

// ── 28. Web Worker userAgent ─────────────────────────────────────────────────
asyncJobs.push(achk("worker", "Worker navigator.userAgent matches window",
  function() {
    return new Promise(function(resolve) {
      var blob = new Blob(["postMessage(navigator.userAgent);"],
                          { type: "application/javascript" });
      var blobURL = URL.createObjectURL(blob);
      var timeout;
      try {
        var w = new Worker(blobURL);
        timeout = setTimeout(function() {
          w.terminate(); URL.revokeObjectURL(blobURL);
          resolve({ val: "timeout", ok: false, notes: "Worker did not respond within 3s" });
        }, 3000);
        w.onmessage = function(e) {
          clearTimeout(timeout);
          w.terminate(); URL.revokeObjectURL(blobURL);
          var wUA = e.data, match = wUA === navigator.userAgent;
          resolve({
            val: match ? "match" : "MISMATCH",
            ok: match,
            notes: match ? wUA.slice(0, 90)
                         : "Worker: " + wUA.slice(0, 55) + "\nWindow: " + navigator.userAgent.slice(0, 55)
          });
        };
        w.onerror = function(e) {
          clearTimeout(timeout);
          URL.revokeObjectURL(blobURL);
          resolve({ val: "error", ok: false, notes: e.message });
        };
      } catch(e) {
        URL.revokeObjectURL(blobURL);
        resolve({ val: "error", ok: false, notes: e.message });
      }
    });
  }, "YELLOW"
));

// ── Wait for all async checks, then finalize verdict ─────────────────────────
Promise.allSettled(asyncJobs).then(function() {
  updateVerdict();
  // Append "done" marker to the verdict text
  var cur = verdict.textContent;
  verdict.textContent = cur + " — complete";
});
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def serve(request: Request) -> HTMLResponse:
    headers = {
        "user-agent":          request.headers.get("user-agent"),
        "accept":              request.headers.get("accept"),
        "accept-language":     request.headers.get("accept-language"),
        "accept-encoding":     request.headers.get("accept-encoding"),
        "sec-ch-ua":           request.headers.get("sec-ch-ua"),
        "sec-ch-ua-mobile":    request.headers.get("sec-ch-ua-mobile"),
        "sec-ch-ua-platform":  request.headers.get("sec-ch-ua-platform"),
        "sec-fetch-site":      request.headers.get("sec-fetch-site"),
        "sec-fetch-mode":      request.headers.get("sec-fetch-mode"),
        "sec-fetch-dest":      request.headers.get("sec-fetch-dest"),
        "connection":          request.headers.get("connection"),
        "client-ip":           request.client.host if request.client else None,
    }
    page = _DETECTION_PAGE.replace('"__SERVER_HEADERS__"', json.dumps(headers))
    return HTMLResponse(page)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bot detection profile server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8899, help="Port (default: 8899)")
    args = parser.parse_args()
    print(f"Bot detection server → http://{args.host}:{args.port}/")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
