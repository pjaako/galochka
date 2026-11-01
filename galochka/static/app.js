(function () {
  var stringsCache = null;

  function t(key) {
    if (!stringsCache) return key;
    return stringsCache[key] || key;
  }

  async function loadStrings() {
    if (stringsCache) return stringsCache;
    var resp = await fetch("/static/strings.de.json");
    stringsCache = await resp.json();
    document.querySelectorAll("[data-i18n]").forEach(function (el) {
      el.textContent = t(el.getAttribute("data-i18n"));
    });
    document.querySelectorAll("[data-i18n-placeholder]").forEach(function (el) {
      el.setAttribute("placeholder", t(el.getAttribute("data-i18n-placeholder")));
    });
    return stringsCache;
  }

  // JSON/multipart fetch helper. Throws { status, detail } on a non-2xx answer.
  // A 401 on a worker path (anything but /api/expert and /api/login) means the
  // session is missing or expired: the browser is sent to /anmelden?next=<page>
  // and the error is still thrown so the caller stops. Pass noRedirect: true to
  // handle a 401 yourself (the print page does, to retry as expert).
  async function api(path, opts) {
    opts = opts || {};
    // A GET request cannot carry a body; fetch throws synchronously if one is set.
    // Default to POST whenever formData/body is given so callers don't have to
    // repeat "method: 'POST'" at every call site.
    var method = opts.method || (opts.formData || opts.body !== undefined ? "POST" : "GET");
    var init = { method: method };
    if (opts.formData) {
      init.body = opts.formData;
    } else if (opts.body !== undefined) {
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify(opts.body);
    }
    if (opts.headers) {
      init.headers = Object.assign({}, init.headers, opts.headers);
    }
    var resp = await fetch(path, init);
    if (resp.status === 204) {
      return null;
    }
    var data = null;
    try {
      data = await resp.json();
    } catch (e) {
      data = null;
    }
    if (!resp.ok) {
      if (
        resp.status === 401 &&
        !opts.noRedirect &&
        path.indexOf("/api/expert") !== 0 &&
        path.indexOf("/api/login") !== 0 &&
        window.location.pathname !== "/anmelden"
      ) {
        window.location.href =
          "/anmelden?next=" + encodeURIComponent(window.location.pathname + window.location.search);
      }
      var detail = data && typeof data.detail === "string" ? data.detail : t("error.generic");
      throw { status: resp.status, detail: detail };
    }
    return data;
  }

  function renderPhotoTile(el, photoUrl, alt) {
    while (el.firstChild) el.removeChild(el.firstChild);
    el.classList.remove("photo-tile--empty");
    if (photoUrl) {
      var img = document.createElement("img");
      img.src = photoUrl;
      img.alt = alt || "";
      // Never let a part photo start a native drag or (on touch) a
      // long-press context menu; it sits over draggable/swipeable tiles.
      img.draggable = false;
      el.appendChild(img);
    } else {
      el.classList.add("photo-tile--empty");
      var mark = document.createElement("span");
      mark.className = "photo-tile__mark";
      mark.textContent = "?";
      el.appendChild(mark);
    }
  }

  var EXPERT_PASSWORD_KEY = "galochka.expertPassword";

  function getExpertPassword() {
    try {
      return sessionStorage.getItem(EXPERT_PASSWORD_KEY);
    } catch (e) {
      return null;
    }
  }

  function setExpertPassword(pw) {
    try {
      sessionStorage.setItem(EXPERT_PASSWORD_KEY, pw);
    } catch (e) {
      // ignore storage failures (e.g. private browsing)
    }
  }

  function clearExpertPassword() {
    try {
      sessionStorage.removeItem(EXPERT_PASSWORD_KEY);
    } catch (e) {
      // ignore storage failures
    }
  }

  async function expertApi(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({}, opts.headers, { "X-Expert-Password": getExpertPassword() || "" });
    // An expert never has a worker session; never bounce to the badge login.
    opts.noRedirect = true;
    try {
      return await api(path, opts);
    } catch (err) {
      if (err && err.status === 401) {
        clearExpertPassword();
        toast(t("expert.wrong_password"));
      }
      throw err;
    }
  }

  var toastTimer = null;
  function toast(msg) {
    var el = document.getElementById("toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "toast";
      el.className = "toast";
      el.setAttribute("role", "status");
      el.setAttribute("aria-live", "polite");
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.add("toast--visible");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      el.classList.remove("toast--visible");
    }, 2500);
  }

  // Downscale a photo before upload (phone Wi-Fi is slow). Resolves to a JPEG
  // Blob whose long side is at most maxSide (never upscaled), EXIF rotation
  // applied. On any failure resolves to the original file: never blocks.
  function downscaleImage(file, maxSide, quality) {
    var bitmap = null;
    function closeBitmap() {
      try { if (bitmap && bitmap.close) bitmap.close(); } catch (e) { /* ignore */ }
      bitmap = null;
    }
    if (typeof createImageBitmap !== "function") return Promise.resolve(file);
    return createImageBitmap(file, { imageOrientation: "from-image" }).then(function (bmp) {
      bitmap = bmp;
      var scale = Math.min(1, maxSide / Math.max(bmp.width, bmp.height));
      var w = Math.max(1, Math.round(bmp.width * scale));
      var h = Math.max(1, Math.round(bmp.height * scale));
      var canvas = document.createElement("canvas");
      canvas.width = w;
      canvas.height = h;
      var ctx = canvas.getContext("2d");
      if (!ctx) throw new Error("no canvas");
      ctx.drawImage(bmp, 0, 0, w, h);
      closeBitmap();
      return new Promise(function (resolve, reject) {
        canvas.toBlob(function (blob) {
          if (blob) resolve(blob); else reject(new Error("toBlob failed"));
        }, "image/jpeg", quality);
      });
    }).catch(function () {
      closeBitmap();
      return file;
    });
  }

  window.C = {
    downscaleImage: downscaleImage, t: t, loadStrings: loadStrings, api: api, toast: toast,
    expertApi: expertApi, getExpertPassword: getExpertPassword, setExpertPassword: setExpertPassword,
    clearExpertPassword: clearExpertPassword, renderPhotoTile: renderPhotoTile,
  };
})();
