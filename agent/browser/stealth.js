// Playwright stealth init script — runs before any page script.
// Patches common automation fingerprints checked by cloakers.
// Configuration variables injected by playwright_chromium.py before this script loads:
//   window.__stealthLocale__  e.g. "en-US"

(function () {
  const locale = window.__stealthLocale__ || 'en-US';
  const localeLang = locale.split('-')[0];

  // 1. Remove navigator.webdriver
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

  // 2. Shim window.chrome (real Chrome always has this)
  if (!window.chrome) {
    window.chrome = {
      app: { isInstalled: false, InstallState: {}, RunningState: {} },
      csi: function () {},
      loadTimes: function () {},
      runtime: {},
    };
  }

  // 3. Realistic navigator.plugins (PDF Viewer + Chrome PDF Viewer)
  function makeFakePlugin(name, description, filename, mimeTypes) {
    const plugin = Object.create(null);
    plugin.name = name;
    plugin.description = description;
    plugin.filename = filename;
    plugin.length = mimeTypes.length;
    mimeTypes.forEach(function (mt, i) {
      plugin[i] = mt;
      plugin[mt.type] = mt;
    });
    plugin[Symbol.iterator] = Array.prototype[Symbol.iterator].bind(
      Array.from({ length: mimeTypes.length }, function (_, i) { return plugin[i]; })
    );
    return plugin;
  }

  function makeMimeType(type, suffixes, description) {
    return { type: type, suffixes: suffixes, description: description };
  }

  const pdfMt1 = makeMimeType('application/pdf', 'pdf', 'Portable Document Format');
  const pdfMt2 = makeMimeType('text/pdf', 'pdf', 'Portable Document Format');
  const pdfPlugin = makeFakePlugin('PDF Viewer', 'Portable Document Format', 'internal-pdf-viewer', [pdfMt1, pdfMt2]);
  const chromePdfPlugin = makeFakePlugin('Chrome PDF Viewer', 'Portable Document Format', 'internal-pdf-viewer', [pdfMt1, pdfMt2]);

  // Back-link mimeTypes to plugin
  [pdfMt1, pdfMt2].forEach(function (mt) {
    Object.defineProperty(mt, 'enabledPlugin', { get: function () { return pdfPlugin; } });
  });

  const fakePlugins = [pdfPlugin, chromePdfPlugin];
  fakePlugins.length = 2;
  fakePlugins['PDF Viewer'] = pdfPlugin;
  fakePlugins['Chrome PDF Viewer'] = chromePdfPlugin;
  fakePlugins[Symbol.iterator] = Array.prototype[Symbol.iterator].bind([pdfPlugin, chromePdfPlugin]);

  Object.defineProperty(navigator, 'plugins', { get: function () { return fakePlugins; } });

  // 4. navigator.mimeTypes
  const fakeMimeTypes = Object.create(null);
  fakeMimeTypes[0] = pdfMt1;
  fakeMimeTypes[1] = pdfMt2;
  fakeMimeTypes['application/pdf'] = pdfMt1;
  fakeMimeTypes['text/pdf'] = pdfMt2;
  fakeMimeTypes.length = 2;
  Object.defineProperty(navigator, 'mimeTypes', { get: function () { return fakeMimeTypes; } });

  // 5. navigator.permissions.query — automation mode returns "denied" for notifications;
  //    real browsers return "default" until the user is prompted.
  const originalQuery = navigator.permissions.query.bind(navigator.permissions);
  navigator.permissions.query = function (params) {
    if (params && params.name === 'notifications') {
      return Promise.resolve({ state: 'default', onchange: null });
    }
    return originalQuery(params);
  };

  // 6. navigator.languages — must match locale and Accept-Language header
  Object.defineProperty(navigator, 'languages', { get: function () { return [locale, localeLang]; } });

  // 7. WebGL vendor/renderer — SwiftShader is a strong automation tell
  var UNMASKED_VENDOR = 0x9245;    // 37445
  var UNMASKED_RENDERER = 0x9246;  // 37446

  (function patchWebGL(Ctor) {
    if (!Ctor) return;
    var orig = Ctor.prototype.getParameter;
    Ctor.prototype.getParameter = function (param) {
      if (param === UNMASKED_VENDOR) return 'Intel Inc.';
      if (param === UNMASKED_RENDERER) return 'Intel Iris OpenGL Engine';
      return orig.call(this, param);
    };
  })(window.WebGLRenderingContext);

  (function patchWebGL2(Ctor) {
    if (!Ctor) return;
    var orig = Ctor.prototype.getParameter;
    Ctor.prototype.getParameter = function (param) {
      if (param === UNMASKED_VENDOR) return 'Intel Inc.';
      if (param === UNMASKED_RENDERER) return 'Intel Iris OpenGL Engine';
      return orig.call(this, param);
    };
  })(window.WebGL2RenderingContext);

  // 8. Hardware fingerprint — match a typical analyst workstation
  Object.defineProperty(navigator, 'hardwareConcurrency', { get: function () { return 8; } });
  if ('deviceMemory' in navigator) {
    Object.defineProperty(navigator, 'deviceMemory', { get: function () { return 8; } });
  }

  // 9. Non-zero outer dimensions — automation contexts sometimes report 0
  if (typeof outerWidth !== 'undefined' && outerWidth === 0) {
    Object.defineProperty(window, 'outerWidth', { get: function () { return window.innerWidth; } });
  }
  if (typeof outerHeight !== 'undefined' && outerHeight === 0) {
    Object.defineProperty(window, 'outerHeight', { get: function () { return window.innerHeight + 85; } });
  }
})();
