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

  // 7. WebGL vendor/renderer — SwiftShader/llvmpipe/VMware are strong automation tells.
  //    Use Windows ANGLE strings consistent with platform: 'Win32'.
  var UNMASKED_VENDOR = 0x9245;    // 37445
  var UNMASKED_RENDERER = 0x9246;  // 37446
  var _wglVendor = 'Google Inc. (Intel)';
  var _wglRenderer = 'ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)';

  (function patchWebGL(Ctor) {
    if (!Ctor) return;
    var orig = Ctor.prototype.getParameter;
    Ctor.prototype.getParameter = function (param) {
      if (param === UNMASKED_VENDOR) return _wglVendor;
      if (param === UNMASKED_RENDERER) return _wglRenderer;
      return orig.call(this, param);
    };
  })(window.WebGLRenderingContext);

  (function patchWebGL2(Ctor) {
    if (!Ctor) return;
    var orig = Ctor.prototype.getParameter;
    Ctor.prototype.getParameter = function (param) {
      if (param === UNMASKED_VENDOR) return _wglVendor;
      if (param === UNMASKED_RENDERER) return _wglRenderer;
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

  // 10. Canvas + AudioContext fingerprint noise.
  //
  //     Turnstile fetches /cmg/1 (a probe image), draws it to a canvas, and reads back pixels
  //     to fingerprint the GPU renderer. It also runs an AudioContext oscillator to fingerprint
  //     the audio stack. Both produce VM-distinctive values that land in Cloudflare's known-bad
  //     set.
  //
  //     All overrides are made to look native via Function.prototype.toString spoofing so that
  //     `fn.toString()` still returns "function name() { [native code] }". Without this,
  //     Turnstile's prototype-integrity checks detect the override directly.
  (function patchFingerprintAPIs() {
    var seed = (Math.random() * 0xFFFFFFFF) >>> 0;

    // Make a wrapped function appear native to toString() and property checks.
    function nativeify(wrapper, original) {
      Object.defineProperty(wrapper, 'name', { value: original.name, configurable: true });
      Object.defineProperty(wrapper, 'length', { value: original.length, configurable: true });
      var nativeStr = 'function ' + original.name + '() { [native code] }';
      wrapper.toString = function () { return nativeStr; };
      // Also spoof Function.prototype.toString.call(wrapper)
      Object.defineProperty(wrapper, Symbol.toStringTag, { value: undefined, configurable: true });
      return wrapper;
    }

    function pixelNoise(x, y, ch) {
      var h = (seed ^ (x * 374761393) ^ (y * 668265263) ^ (ch * 2654435761)) >>> 0;
      h = (Math.imul(h ^ (h >>> 13), 1540483477)) >>> 0;
      h = h ^ (h >>> 15);
      return (h & 3) - 1; // -1, 0, or +1
    }

    // --- Canvas 2D ---
    var _origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = nativeify(function getImageData(sx, sy, sw, sh) {
      var id = _origGetImageData.apply(this, arguments);
      var d = id.data;
      for (var i = 0; i < d.length; i += 4) {
        var px = ((i >>> 2) % sw) | 0;
        var py = ((i >>> 2) / sw) | 0;
        d[i]     = Math.max(0, Math.min(255, d[i]     + pixelNoise(sx + px, sy + py, 0)));
        d[i + 1] = Math.max(0, Math.min(255, d[i + 1] + pixelNoise(sx + px, sy + py, 1)));
        d[i + 2] = Math.max(0, Math.min(255, d[i + 2] + pixelNoise(sx + px, sy + py, 2)));
      }
      return id;
    }, _origGetImageData);

    var _origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = nativeify(function toDataURL(type, quality) {
      var ctx = this.getContext && this.getContext('2d');
      if (!ctx || this.width === 0 || this.height === 0) return _origToDataURL.apply(this, arguments);
      var scratch = document.createElement('canvas');
      scratch.width = this.width;
      scratch.height = this.height;
      scratch.getContext('2d').putImageData(ctx.getImageData(0, 0, this.width, this.height), 0, 0);
      return _origToDataURL.call(scratch, type, quality);
    }, _origToDataURL);

    // --- OffscreenCanvas (used by Turnstile workers) ---
    if (typeof OffscreenCanvasRenderingContext2D !== 'undefined') {
      var _origOffGID = OffscreenCanvasRenderingContext2D.prototype.getImageData;
      OffscreenCanvasRenderingContext2D.prototype.getImageData = nativeify(function getImageData(sx, sy, sw, sh) {
        var id = _origOffGID.apply(this, arguments);
        var d = id.data;
        for (var i = 0; i < d.length; i += 4) {
          var px = ((i >>> 2) % sw) | 0;
          var py = ((i >>> 2) / sw) | 0;
          d[i]     = Math.max(0, Math.min(255, d[i]     + pixelNoise(sx + px, sy + py, 0)));
          d[i + 1] = Math.max(0, Math.min(255, d[i + 1] + pixelNoise(sx + px, sy + py, 1)));
          d[i + 2] = Math.max(0, Math.min(255, d[i + 2] + pixelNoise(sx + px, sy + py, 2)));
        }
        return id;
      }, _origOffGID);
    }

    // --- AudioContext fingerprint ---
    // Real Chrome on different hardware produces slightly different floating-point values
    // when summing an oscillator's output buffer. Virtual audio stacks are distinctive.
    // We add a tiny per-session offset to getChannelData and getFloatFrequencyData so
    // the summed fingerprint value differs from the VM baseline.
    var _audioNoise = (seed & 0xFF) / 0xFFFFFF; // ~0 to ~0.0000001, imperceptible

    if (typeof AudioBuffer !== 'undefined') {
      var _origGetChannelData = AudioBuffer.prototype.getChannelData;
      AudioBuffer.prototype.getChannelData = nativeify(function getChannelData(channel) {
        var data = _origGetChannelData.call(this, channel);
        // Patch only small buffers used for fingerprinting (large buffers are real audio)
        if (data.length < 4096) {
          for (var i = 0; i < data.length; i++) {
            data[i] += _audioNoise * (i % 2 === 0 ? 1 : -1);
          }
        }
        return data;
      }, _origGetChannelData);
    }

    if (typeof AnalyserNode !== 'undefined') {
      var _origGetFloatFreq = AnalyserNode.prototype.getFloatFrequencyData;
      AnalyserNode.prototype.getFloatFrequencyData = nativeify(function getFloatFrequencyData(arr) {
        _origGetFloatFreq.call(this, arr);
        if (arr.length < 4096) {
          for (var i = 0; i < arr.length; i++) arr[i] += _audioNoise;
        }
      }, _origGetFloatFreq);
    }
  })();
})();
