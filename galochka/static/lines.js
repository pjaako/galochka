// Line-by-line confirmation component shared by Packen (delivery packing, rma outgoing) and the
// RMA page (intake). Lines.mount(container, job, options) draws the progress head, the numbered
// strip, one line at a time with its photo / quality / serial inputs, the summary and the finish
// button into `container`.
//
// job      GET /api/jobs/{id}: the lines of the job's current stage are used.
// options  editable  false = read-only overview.
//          title, meta  head texts (the page knows the order / RMA number).
//          keys      {name: string-key} overrides of the button and heading texts (see DEFAULT_KEYS).
//          onFinish(result)  called after a successful POST /finish; without it the component shows
//                    its own "done" view (options.keys.done_check / done_released).
(function () {
  var DEFAULT_KEYS = {
    action_ok: "pack.action_ok",
    action_missing: "pack.action_missing",
    action_notok: "pack.action_notok",
    missing_title: "pack.missing_title",
    notok_title: "pack.notok_title",
    send_missing: "pack.send_missing",
    send_notok: "pack.send_notok",
    extra: "pack.extra",
    extra_title: "pack.extra_title",
    extra_help: "pack.extra_help",
    extra_add: "pack.extra_add",
    summary_title: "pack.summary_title",
    finish: "pack.finish",
    done_check: "pack.done_second_check",
    done_released: "pack.done_released"
  };

  function reportError(err) {
    C.toast((err && err.detail) || C.t("error.generic"));
  }

  function clearNode(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function el(tagName, className, text) {
    var node = document.createElement(tagName);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function button(label, className, onClick) {
    var btn = el("button", "btn " + (className || ""), label);
    btn.type = "button";
    if (onClick) btn.addEventListener("click", onClick);
    return btn;
  }

  // Item search against GET /api/items?q= (worker session). Choosing an item fills an empty label.
  function itemPicker(onPick) {
    var wrap = el("div", "item-picker");
    var input = el("input", "input-id");
    input.type = "text";
    input.setAttribute("autocomplete", "off");
    input.setAttribute("placeholder", C.t("pack.extra_item_placeholder"));
    var list = el("div", "item-picker__list");
    list.hidden = true;
    wrap.appendChild(input);
    wrap.appendChild(list);
    var seq = 0;
    var timer = null;

    function hide() {
      seq += 1;
      list.hidden = true;
      clearNode(list);
    }

    function show(items) {
      clearNode(list);
      if (!items.length) list.appendChild(el("div", "item-picker__none", C.t("pack.extra_item_none")));
      items.forEach(function (item) {
        var btn = el("button", "item-picker__option", item.item_id + " – " + item.name);
        btn.type = "button";
        btn.addEventListener("mousedown", function (event) { event.preventDefault(); choose(item); });
        btn.addEventListener("click", function () { choose(item); });
        list.appendChild(btn);
      });
      list.hidden = false;
    }

    function choose(item) {
      input.value = item.item_id + " – " + item.name;
      hide();
      onPick(item);
    }

    input.addEventListener("input", function () {
      onPick(null);
      if (timer) clearTimeout(timer);
      timer = setTimeout(async function () {
        var q = input.value.trim();
        seq += 1;
        var mine = seq;
        if (!q) {
          hide();
          return;
        }
        try {
          var items = await C.api("/api/items?q=" + encodeURIComponent(q) + "&limit=10");
          if (mine === seq) show(items);
        } catch (err) { /* the search is optional */ }
      }, 250);
    });
    input.addEventListener("blur", function () { setTimeout(hide, 200); });
    return wrap;
  }

  function mount(container, job, options) {
    options = options || {};
    var jobId = job.id;
    var lines = (job.lines || []).slice(); // lines of the current stage, in position order
    var idx = 0; // index into lines
    var view = "line"; // "line" | "summary" | "extra" | "done"
    var editable = !!options.editable; // false: read-only summary (job not in a packing status)
    var editing = {}; // line id -> true after "Ändern"
    var flagged = {}; // line id -> true: reported open by the server on finish
    var busy = false;
    var draft = null; // the unsent answer for the line on screen
    var extraDraft = null; // the "Zusätzliches Teil" form
    var doneStatus = null;

    // The component owns its DOM: head, strip, navigation, body, tools and the sticky action bar.
    container.classList.add("lines");
    while (container.firstChild) container.removeChild(container.firstChild);

    function make(tagName, className, id) {
      var node = document.createElement(tagName);
      if (className) node.className = className;
      if (id) node.id = id;
      return node;
    }

    var headEl = make("header", "pack-head", "pack-head");
    var orderEl = make("h1", "pack-head__order", "pack-order");
    var metaEl = make("p", "pack-head__meta", "pack-meta");
    var barEl = make("div", "progress-bar", "pack-bar");
    barEl.setAttribute("role", "progressbar");
    barEl.setAttribute("aria-valuemin", "0");
    barEl.setAttribute("aria-valuemax", "100");
    var barFill = make("div", "progress-bar__fill", "pack-bar-fill");
    var progressEl = make("p", "pack-progress", "pack-progress");
    barEl.appendChild(barFill);
    headEl.appendChild(orderEl);
    headEl.appendChild(metaEl);
    headEl.appendChild(barEl);
    headEl.appendChild(progressEl);
    headEl.hidden = true;
    var stripEl = make("div", "line-strip", "pack-strip");
    stripEl.hidden = true;
    var navEl = make("div", "pack-nav", "pack-nav");
    navEl.hidden = true;
    var bodyEl = make("div", "", "pack-body");
    var toolsEl = make("div", "pack-tools", "pack-tools");
    toolsEl.hidden = true;
    var actionsEl = make("div", "pack-actions", "pack-actions");
    actionsEl.hidden = true;
    [headEl, stripEl, navEl, bodyEl, toolsEl, actionsEl].forEach(function (node) { container.appendChild(node); });

    // Button and heading texts: the page can swap any of them by string key (options.keys).
    function txt(name) {
      return C.t((options.keys && options.keys[name]) || DEFAULT_KEYS[name]);
    }

    // Containers of the line form, rebuilt independently so typing never loses focus.
    var qualityWrap = null;
    var serialWrap = null;
    var photoWrap = null;
    var panelWrap = null;

    // --- state helpers ------------------------------------------------------------------

    // open | ok | missing | not_ok | withheld  (a confirmation of an earlier, rejected attempt counts as open)
    function lineState(line) {
      if (line.withheld) return "withheld";
      var latest = line.latest;
      // Intake lines are done once confirmed; attempts only concern the packed/outgoing box.
      if (!latest || (line.stage !== "intake" && latest.attempt !== job.attempt)) return "open";
      return latest.result;
    }

    function stateLabel(state) {
      return C.t("pack.state_" + state);
    }

    function countable() {
      return lines.filter(function (line) { return !line.withheld; });
    }

    function doneCount() {
      return countable().filter(function (line) { return lineState(line) !== "open"; }).length;
    }

    function openIndexes() {
      var out = [];
      lines.forEach(function (line, i) {
        if (lineState(line) === "open") out.push(i);
      });
      return out;
    }

    // The next open line after `from`, wrapping around; -1 when everything is done.
    function nextOpenIndex(from) {
      for (var step = 1; step <= lines.length; step += 1) {
        var i = (from + step) % lines.length;
        if (lineState(lines[i]) === "open") return i;
      }
      return -1;
    }

    function currentLine() {
      return lines[idx] || null;
    }

    // --- header, strip, navigation ------------------------------------------------------

    function renderHead() {
      headEl.hidden = false;
      orderEl.textContent = options.title || "";
      metaEl.textContent = options.meta || "";
      metaEl.hidden = !options.meta;
      var total = countable().length;
      var done = doneCount();
      progressEl.textContent = C.fmt(C.t("pack.progress"), { done: done, total: total });
      var percent = total ? Math.round((done / total) * 100) : 0;
      barFill.style.width = percent + "%";
      barEl.setAttribute("aria-valuenow", String(percent));
    }

    function renderStrip() {
      clearNode(stripEl);
      if (!lines.length || view === "done") {
        stripEl.hidden = true;
        return;
      }
      stripEl.hidden = false;
      var current = null;
      lines.forEach(function (line, i) {
        var state = lineState(line);
        var dot = el("button", "line-dot line-dot--" + state + (flagged[line.id] ? " line-dot--flag" : ""), String(i + 1));
        dot.type = "button";
        dot.setAttribute("aria-label", line.label + " – " + stateLabel(state));
        if (view === "line" && i === idx) {
          dot.setAttribute("aria-current", "true");
          current = dot;
        }
        dot.addEventListener("click", function () { showLine(i); });
        stripEl.appendChild(dot);
      });
      var sum = el("button", "line-dot line-dot--summary", "Σ");
      sum.type = "button";
      sum.setAttribute("aria-label", C.t("pack.overview"));
      if (view === "summary") sum.setAttribute("aria-current", "true");
      sum.addEventListener("click", showSummary);
      stripEl.appendChild(sum);
      if (current) stripEl.scrollLeft = Math.max(0, current.offsetLeft - stripEl.clientWidth / 2);
    }

    function renderNav() {
      clearNode(navEl);
      clearNode(toolsEl);
      var showNav = view === "line" && lines.length > 0;
      navEl.hidden = !showNav;
      toolsEl.hidden = !(editable && (view === "line" || view === "summary"));
      if (showNav) {
        navEl.appendChild(button("‹ " + C.t("pack.back"), "btn--secondary", function () { step(-1); }));
        navEl.appendChild(el("span", "pack-nav__pos", C.fmt(C.t("pack.line_pos"), { n: idx + 1, total: lines.length })));
        navEl.appendChild(button(C.t("pack.next") + " ›", "btn--secondary", function () { step(1); }));
      }
      if (!toolsEl.hidden) {
        if (view === "line") toolsEl.appendChild(button(C.t("pack.overview"), "btn--secondary", showSummary));
        toolsEl.appendChild(button(txt("extra"), "btn--secondary", showExtra));
      }
    }

    // Weiter / Zurück: skip withheld lines; past the last line comes the summary.
    function step(dir) {
      if (busy) return;
      var i = idx + dir;
      while (i >= 0 && i < lines.length && lines[i].withheld) i += dir;
      if (i < 0) return;
      if (i >= lines.length) {
        showSummary();
        return;
      }
      showLine(i);
    }

    function revokePhoto() {
      if (draft && draft.photoUrl) {
        try { URL.revokeObjectURL(draft.photoUrl); } catch (e) { /* ignore */ }
      }
    }

    function newDraft(line) {
      revokePhoto();
      draft = {
        lineId: line.id,
        quality: {},
        serials: [],
        photo: null,
        photoUrl: null,
        mode: "main", // main | missing | notok
        presentQty: 0,
        reason: "",
      };
      // "Ändern": start from the earlier answers (the photo has to be taken again)
      if (editing[line.id] && line.latest) {
        Object.keys(line.latest.quality || {}).forEach(function (key) { draft.quality[key] = line.latest.quality[key]; });
        draft.serials = (line.latest.serials || []).slice();
      }
    }

    function showLine(i) {
      if (busy) return;
      idx = i;
      view = "line";
      var line = currentLine();
      if (line) newDraft(line);
      window.scrollTo(0, 0);
      renderAll();
    }

    function showSummary() {
      if (busy) return;
      view = "summary";
      window.scrollTo(0, 0);
      renderAll();
    }

    function showExtra() {
      if (busy) return;
      view = "extra";
      extraDraft = { picked: null };
      window.scrollTo(0, 0);
      renderAll();
    }

    // --- line view ------------------------------------------------------------------------

    function buildReference(line) {
      var wrap = el("div", "");
      var photos = (line.reference && line.reference.photos) || [];
      var tile = el("div", "photo-tile pack-ref");
      var hint = el("p", "pack-ref__hint", C.t("pack.no_reference"));
      hint.hidden = true;

      function show(url) {
        C.renderPhotoTile(tile, url, line.label);
        var img = tile.querySelector("img");
        if (!url) {
          hint.hidden = false;
          return;
        }
        hint.hidden = true;
        img.onerror = function () {
          C.renderPhotoTile(tile, null, "");
          hint.hidden = false;
        };
      }

      show(photos.length ? photos[0].url : null);
      wrap.appendChild(tile);
      wrap.appendChild(hint);
      if (photos.length > 1) {
        var row = el("div", "pack-thumbs");
        photos.forEach(function (photo, i) {
          var thumb = el("button", "pack-thumb");
          thumb.type = "button";
          thumb.setAttribute("aria-current", String(i === 0));
          var img = document.createElement("img");
          img.src = photo.url;
          img.alt = "";
          img.draggable = false;
          thumb.appendChild(img);
          thumb.addEventListener("click", function () {
            Array.prototype.forEach.call(row.children, function (other) { other.setAttribute("aria-current", "false"); });
            thumb.setAttribute("aria-current", "true");
            show(photo.url);
          });
          row.appendChild(thumb);
        });
        wrap.appendChild(row);
      }
      return wrap;
    }

    // Quantity of the answer being prepared: all of it, except "Fehlt" which sends the present part.
    function answerQty(line) {
      return draft.mode === "missing" ? draft.presentQty : line.qty;
    }

    function photoNeeded(line) {
      if (draft.mode === "notok") return true;
      if (draft.mode === "missing" && draft.presentQty === 0) return false;
      return !!(line.photo_required || line.source === "worker");
    }

    function buildQualityRow(point) {
      var row = el("div", "q-row");
      row.appendChild(el("div", "q-row__label", point));
      var pair = el("div", "q-pair");
      var okBtn = el("button", "q-btn q-btn--ok", C.t("pack.q_ok"));
      var badBtn = el("button", "q-btn q-btn--bad", C.t("pack.q_bad"));
      okBtn.type = "button";
      badBtn.type = "button";

      function sync() {
        okBtn.setAttribute("aria-pressed", String(draft.quality[point] === "ok"));
        badBtn.setAttribute("aria-pressed", String(draft.quality[point] === "not_ok"));
      }
      okBtn.addEventListener("click", function () { draft.quality[point] = "ok"; sync(); });
      badBtn.addEventListener("click", function () { draft.quality[point] = "not_ok"; sync(); });
      sync();
      pair.appendChild(okBtn);
      pair.appendChild(badBtn);
      row.appendChild(pair);
      return row;
    }

    function renderQuality(line) {
      clearNode(qualityWrap);
      var points = line.quality_points || [];
      qualityWrap.hidden = !points.length || answerQty(line) === 0;
      points.forEach(function (point) { qualityWrap.appendChild(buildQualityRow(point)); });
    }

    function renderSerials(line) {
      clearNode(serialWrap);
      var count = answerQty(line);
      serialWrap.hidden = !line.serial_required || count === 0;
      if (serialWrap.hidden) return;
      serialWrap.appendChild(el("p", "pack-block__title", C.t("pack.serial_title")));
      for (var i = 0; i < count; i += 1) {
        (function (n) {
          var input = el("input", "input-id serial-input");
          input.type = "text";
          input.maxLength = 64;
          input.value = draft.serials[n] || "";
          input.setAttribute("autocapitalize", "characters");
          input.setAttribute("autocomplete", "off");
          input.setAttribute("autocorrect", "off");
          input.setAttribute("spellcheck", "false");
          input.setAttribute("placeholder", C.fmt(C.t("pack.serial_placeholder"), { n: n + 1 }));
          input.setAttribute("data-serial", String(n));
          input.addEventListener("input", function () { draft.serials[n] = input.value; });
          serialWrap.appendChild(input);
        })(i);
      }
    }

    function renderPhoto(line) {
      clearNode(photoWrap);
      if (draft.photoUrl) {
        var img = el("img", "pack-photo");
        img.src = draft.photoUrl;
        img.alt = C.t("pack.photo_alt");
        photoWrap.appendChild(img);
      }
      var required = photoNeeded(line);
      var label = el("label", "photo-button" + (draft.photo ? " photo-button--done" : ""));
      label.appendChild(el("span", "", (draft.photo ? C.t("pack.photo_retake") : C.t("pack.photo_take")) + (required ? " ★" : "")));
      var input = el("input", "");
      input.type = "file";
      input.accept = "image/*";
      input.setAttribute("capture", "environment");
      input.hidden = true;
      input.addEventListener("change", function () {
        var file = input.files && input.files[0];
        if (!file) return;
        revokePhoto();
        draft.photo = file;
        draft.photoUrl = URL.createObjectURL(file);
        renderPhoto(line);
      });
      label.appendChild(input);
      photoWrap.appendChild(label);
      if (required) photoWrap.appendChild(el("p", "pack-photo-hint", "★ " + C.t("pack.photo_required")));
    }

    function renderPanel(line) {
      clearNode(panelWrap);
      panelWrap.hidden = draft.mode === "main";
      if (draft.mode === "main") return;
      var bad = draft.mode === "notok";
      panelWrap.className = "pack-panel" + (bad ? " pack-panel--bad" : "");
      panelWrap.appendChild(el("p", "pack-block__title", txt(bad ? "notok_title" : "missing_title")));
      if (!bad && line.qty > 1) {
        var stepper = el("div", "stepper");
        var minus = button("−", "", function () { setPresent(line, draft.presentQty - 1); });
        var plus = button("+", "", function () { setPresent(line, draft.presentQty + 1); });
        minus.setAttribute("aria-label", C.t("pack.qty_less"));
        plus.setAttribute("aria-label", C.t("pack.qty_more"));
        minus.disabled = draft.presentQty <= 0;
        plus.disabled = draft.presentQty >= line.qty - 1;
        stepper.appendChild(minus);
        stepper.appendChild(el("span", "stepper__value", C.fmt(C.t("pack.present_qty"), { n: draft.presentQty, total: line.qty })));
        stepper.appendChild(plus);
        panelWrap.appendChild(stepper);
      }
      var reason = el("textarea", "input-id");
      reason.rows = 2;
      reason.maxLength = 200;
      reason.value = draft.reason;
      reason.setAttribute("placeholder", C.t("pack.reason_placeholder"));
      reason.setAttribute("data-reason", "1");
      reason.addEventListener("input", function () { draft.reason = reason.value; });
      panelWrap.appendChild(reason);
    }

    function setPresent(line, qty) {
      draft.presentQty = Math.max(0, Math.min(line.qty - 1, qty));
      renderPanel(line);
      renderQuality(line);
      renderSerials(line);
      renderPhoto(line);
    }

    function setMode(mode) {
      var line = currentLine();
      draft.mode = mode;
      draft.presentQty = 0;
      renderPanel(line);
      renderQuality(line);
      renderSerials(line);
      renderPhoto(line);
      renderActions();
    }

    function stateBadge(state) {
      return el("span", "state-badge state-badge--" + state, stateLabel(state));
    }

    function qualitySummary(quality) {
      return Object.keys(quality || {}).map(function (key) {
        return key + ": " + (quality[key] === "ok" ? C.t("pack.q_ok") : C.t("pack.q_bad"));
      }).join(", ");
    }

    function buildConfirmed(line) {
      var latest = line.latest;
      var box = el("div", "pack-done");
      var row = el("div", "pack-done__row");
      row.appendChild(stateBadge(latest.result));
      row.appendChild(el("span", "", C.fmt(C.t("pack.by"), { name: latest.worker_name })));
      box.appendChild(row);
      if (latest.result !== "ok") box.appendChild(el("p", "pack-done__detail", C.fmt(C.t("pack.present_qty"), { n: latest.qty, total: line.qty })));
      if (latest.reason) box.appendChild(el("p", "pack-done__detail", C.fmt(C.t("pack.reason_line"), { reason: latest.reason })));
      if (latest.serials && latest.serials.length) {
        box.appendChild(el("p", "pack-done__detail", C.fmt(C.t("pack.serials_line"), { list: latest.serials.join(", ") })));
      }
      var quality = qualitySummary(latest.quality);
      if (quality) box.appendChild(el("p", "pack-done__detail", quality));
      if (latest.photo_url) {
        var img = el("img", "pack-photo");
        img.src = latest.photo_url;
        img.alt = C.t("pack.photo_alt");
        img.style.marginTop = "8px";
        box.appendChild(img);
      }
      if (editable) {
        box.appendChild(button(C.t("pack.change"), "btn--secondary", function () {
          editing[line.id] = true;
          newDraft(line);
          renderAll();
        }));
      }
      return box;
    }

    function renderLineView() {
      var line = currentLine();
      clearNode(bodyEl);
      if (!line) return;
      var card = el("div", "pack-line" + (line.withheld ? " pack-line--withheld" : ""));
      var tags = el("div", "pack-line__tags");
      if (line.source === "worker") tags.appendChild(el("span", "tag tag--warn", C.t("pack.source_worker")));
      if (line.source === "office") tags.appendChild(el("span", "tag", C.t("pack.source_office")));
      if (tags.firstChild) card.appendChild(tags);
      card.appendChild(buildReference(line));
      card.appendChild(el("h2", "pack-line__label", line.label));
      if (line.item_id) card.appendChild(el("p", "pack-line__id", line.item_id));
      if (line.instruction) card.appendChild(el("p", "note", line.instruction));
      card.appendChild(el("p", "pack-line__qty", C.fmt(C.t("pack.qty"), { qty: line.qty })));
      if (line.stage === "outgoing" && line.in_qty !== undefined && line.in_qty !== null) {
        card.appendChild(el("p", "pack-line__in", C.fmt(C.t("pack.rma_in_qty"), { n: line.in_qty })));
      }
      if (line.stage === "outgoing" && line.reason) card.appendChild(el("p", "note pack-line__reason", line.reason));

      if (line.withheld) {
        card.appendChild(el("p", "pack-line__withheld", C.fmt(C.t("pack.withheld"), { reason: line.withheld_reason || "" })));
        bodyEl.appendChild(card);
        return;
      }
      if (lineState(line) !== "open" && !editing[line.id]) {
        card.appendChild(buildConfirmed(line));
        bodyEl.appendChild(card);
        return;
      }

      qualityWrap = el("div", "pack-block");
      serialWrap = el("div", "pack-block");
      photoWrap = el("div", "pack-block");
      panelWrap = el("div", "pack-panel");
      card.appendChild(qualityWrap);
      card.appendChild(serialWrap);
      card.appendChild(photoWrap);
      card.appendChild(panelWrap);
      if (line.source === "worker" && !line.latest && !line.generated && editable) {
        var remove = button(C.t("pack.remove"), "btn--danger", function () { removeLine(line); });
        remove.style.marginTop = "16px";
        card.appendChild(remove);
      }
      bodyEl.appendChild(card);
      renderQuality(line);
      renderSerials(line);
      renderPhoto(line);
      renderPanel(line);
    }

    // --- sending an answer ------------------------------------------------------------------

    // Mirrors repo.validate_confirmation for the cases a worker can run into; returns an error text or null.
    function checkDraft(line, result) {
      var qty = result === "missing" ? draft.presentQty : line.qty;
      var points = line.quality_points || [];
      var reason = draft.reason.trim();
      if (result !== "ok" && reason.length < 3) return C.t("error.reason_required");
      if (qty > 0) {
        for (var i = 0; i < points.length; i += 1) {
          if (!draft.quality[points[i]]) return C.t("error.quality_incomplete");
        }
        if (result === "ok") {
          for (var j = 0; j < points.length; j += 1) {
            if (draft.quality[points[j]] === "not_ok") return C.t("error.quality_not_ok");
          }
        }
      }
      if (line.serial_required && qty > 0) {
        for (var n = 0; n < qty; n += 1) {
          if (!(draft.serials[n] || "").trim()) return C.t("error.serial_count");
        }
      }
      var needsPhoto = line.photo_required || line.source === "worker" || result === "not_ok";
      if (needsPhoto && !(result === "missing" && qty === 0) && !draft.photo) return C.t("error.photo_required");
      return null;
    }

    async function sendAnswer(result) {
      if (busy) return;
      var line = currentLine();
      var problem = checkDraft(line, result);
      if (problem) {
        C.toast(problem);
        return;
      }
      var qty = result === "missing" ? draft.presentQty : line.qty;
      var points = line.quality_points || [];
      var quality = {};
      if (qty > 0) {
        points.forEach(function (point) { quality[point] = draft.quality[point]; });
      }
      var serials = [];
      if (line.serial_required) {
        for (var n = 0; n < qty; n += 1) serials.push((draft.serials[n] || "").trim());
      }
      setBusy(true);
      try {
        var form = new FormData();
        form.append("result", result);
        form.append("qty", String(qty));
        form.append("reason", result === "ok" ? "" : draft.reason.trim());
        form.append("serials", JSON.stringify(serials));
        form.append("quality", JSON.stringify(quality));
        if (draft.photo && !(result === "missing" && qty === 0)) {
          var blob = await C.downscaleImage(draft.photo, 1600, 0.9);
          form.append("image", blob, "photo.jpg");
        }
        var answer = await C.api("/api/lines/" + line.id + "/confirm", { formData: form });
        lines[idx] = mergeLine(lines[idx], answer.line);
        delete editing[line.id];
        delete flagged[line.id];
        busy = false;
        var next = nextOpenIndex(idx);
        if (next === -1) {
          view = "summary";
          renderAll();
          window.scrollTo(0, 0);
        } else {
          showLine(next);
        }
      } catch (err) {
        reportError(err);
        setBusy(false);
      }
    }

    // The confirm answer has no reference photos and no RMA in_qty: keep what we already have.
    function mergeLine(old, fresh) {
      if (!fresh.reference) fresh.reference = old.reference;
      if (fresh.in_qty === undefined && old.in_qty !== undefined) fresh.in_qty = old.in_qty;
      return fresh;
    }

    function setBusy(value) {
      busy = value;
      renderActions();
    }

    async function removeLine(line) {
      if (busy) return;
      if (!window.confirm(C.fmt(C.t("pack.remove_confirm"), { label: line.label }))) return;
      setBusy(true);
      try {
        await C.api("/api/lines/" + line.id, { method: "DELETE" });
        lines = lines.filter(function (other) { return other.id !== line.id; });
        busy = false;
        if (idx >= lines.length) idx = Math.max(0, lines.length - 1);
        if (lines.length) {
          showLine(idx);
        } else {
          view = "summary";
          renderAll();
        }
      } catch (err) {
        reportError(err);
        setBusy(false);
      }
    }

    // --- "Zusätzliches Teil" ---------------------------------------------------------------

    function renderExtraView() {
      clearNode(bodyEl);
      var form = el("div", "pack-line");
      form.appendChild(el("h2", "pack-line__label", txt("extra_title")));
      form.appendChild(el("p", "field__help", txt("extra_help")));

      function field(labelKey, control) {
        var box = el("div", "field");
        box.appendChild(el("label", "field__label", C.t(labelKey)));
        box.appendChild(control);
        return box;
      }

      var labelInput = el("input", "input-id");
      labelInput.type = "text";
      labelInput.maxLength = 120;
      labelInput.id = "extra-label";
      var qtyInput = el("input", "input-id");
      qtyInput.type = "number";
      qtyInput.min = "1";
      qtyInput.max = "999";
      qtyInput.value = "1";
      qtyInput.id = "extra-qty";
      qtyInput.setAttribute("inputmode", "numeric");
      var reasonInput = el("textarea", "input-id");
      reasonInput.rows = 2;
      reasonInput.maxLength = 200;
      reasonInput.id = "extra-reason";

      var picker = itemPicker(function (item) {
        extraDraft.picked = item;
        if (item && !labelInput.value.trim()) labelInput.value = item.name;
      });
      form.appendChild(field("pack.extra_item", picker));
      form.appendChild(field("pack.extra_label", labelInput));
      form.appendChild(field("pack.extra_qty", qtyInput));
      form.appendChild(field("pack.extra_reason", reasonInput));
      extraDraft.read = function () {
        return {
          item_id: extraDraft.picked ? extraDraft.picked.item_id : null,
          label: labelInput.value.trim() || (extraDraft.picked ? extraDraft.picked.name : ""),
          qty: parseInt(qtyInput.value, 10),
          reason: reasonInput.value.trim(),
        };
      };
      bodyEl.appendChild(form);
    }

    async function sendExtra() {
      if (busy) return;
      var data = extraDraft.read();
      if (!data.label) {
        C.toast(C.t("pack.extra_label_required"));
        return;
      }
      if (!(data.qty >= 1 && data.qty <= 999)) {
        C.toast(C.t("error.bad_qty"));
        return;
      }
      if (data.reason.length < 3) {
        C.toast(C.t("error.reason_required"));
        return;
      }
      setBusy(true);
      try {
        var line = await C.api("/api/jobs/" + jobId + "/lines", { body: data });
        lines.push(line);
        lines.sort(function (a, b) { return a.position - b.position; });
        busy = false;
        showLine(lines.indexOf(lines.filter(function (other) { return other.id === line.id; })[0]));
      } catch (err) {
        reportError(err);
        setBusy(false);
      }
    }

    // --- summary --------------------------------------------------------------------------

    function summaryRow(line, i) {
      var state = lineState(line);
      var cls = "sum-row sum-row--" + state;
      if (line.source === "worker" && state !== "missing" && state !== "not_ok") cls += " sum-row--worker";
      if (flagged[line.id]) cls += " sum-row--flag";
      var row = el("button", cls);
      row.type = "button";
      row.setAttribute("data-line-id", String(line.id));
      var head = el("div", "sum-row__head");
      head.appendChild(el("span", "sum-row__label", (i + 1) + ". " + line.label + " × " + line.qty));
      head.appendChild(stateBadge(state));
      row.appendChild(head);
      var meta = [];
      if (line.item_id) meta.push(line.item_id);
      if (line.source === "worker") meta.push(C.t("pack.source_worker"));
      if (line.source === "office") meta.push(C.t("pack.source_office"));
      if (line.stage === "outgoing" && line.in_qty !== undefined && line.in_qty !== null) {
        meta.push(C.fmt(C.t("pack.rma_in_qty"), { n: line.in_qty }));
      }
      if (line.latest && state !== "open" && state !== "withheld") {
        meta.push(C.fmt(C.t("pack.by"), { name: line.latest.worker_name }));
        if (line.latest.result !== "ok") meta.push(C.fmt(C.t("pack.present_qty"), { n: line.latest.qty, total: line.qty }));
        if (line.latest.reason) meta.push(C.fmt(C.t("pack.reason_line"), { reason: line.latest.reason }));
      }
      if (state === "withheld") meta.push(C.fmt(C.t("pack.withheld"), { reason: line.withheld_reason || "" }));
      if ((line.source === "worker" || line.stage === "outgoing") && line.reason
          && !(line.latest && line.latest.reason === line.reason)) {
        meta.push(C.fmt(C.t("pack.reason_line"), { reason: line.reason }));
      }
      if (meta.length) row.appendChild(el("div", "sum-row__meta", meta.join(" · ")));
      row.addEventListener("click", function () { showLine(i); });
      return row;
    }

    function renderSummaryView() {
      clearNode(bodyEl);
      var box = el("div", "pack-line");
      box.appendChild(el("h2", "pack-line__label", txt("summary_title")));
      if (!editable) {
        box.appendChild(el("p", "note", C.fmt(C.t("pack.readonly"), { status: C.t("status." + job.status) })));
      }
      var list = el("div", "sum-list");
      lines.forEach(function (line, i) { list.appendChild(summaryRow(line, i)); });
      box.appendChild(list);
      if (job.kind === "rma") {
        var history = el("a", "btn btn--secondary", C.t("pack.rma_history"));
        history.href = "/rma/" + jobId;
        history.style.marginTop = "16px";
        box.appendChild(history);
      }
      if (!editable) {
        var back = el("a", "btn btn--primary", C.t("pack.to_overview"));
        back.href = "/auftraege";
        back.style.marginTop = "16px";
        box.appendChild(back);
      }
      bodyEl.appendChild(box);
    }

    async function finish() {
      if (busy) return;
      setBusy(true);
      try {
        var result = await C.api("/api/jobs/" + jobId + "/finish", { method: "POST" });
        doneStatus = result.status;
        job.status = result.status;
        busy = false;
        editable = false;
        if (typeof options.onFinish === "function") {
          options.onFinish(result);
          return;
        }
        view = "done";
        window.scrollTo(0, 0);
        renderAll();
      } catch (err) {
        setBusy(false);
        if (err && err.status === 409 && err.data && err.data.open_line_ids) {
          flagged = {};
          err.data.open_line_ids.forEach(function (id) { flagged[id] = true; });
          C.toast(C.t("error.lines_open"));
          var first = -1;
          lines.forEach(function (line, i) { if (first === -1 && flagged[line.id]) first = i; });
          if (first !== -1) showLine(first); else renderAll();
          return;
        }
        reportError(err);
      }
    }

    function renderDoneView() {
      clearNode(bodyEl);
      var needsCheck = doneStatus === "packed";
      var box = el("div", "pack-result" + (needsCheck ? " pack-result--check" : ""));
      box.appendChild(el("p", "", txt(needsCheck ? "done_check" : "done_released")));
      var overview = el("a", "btn btn--primary", C.t("pack.to_overview"));
      overview.href = "/auftraege";
      box.appendChild(overview);
      box.appendChild(button(C.t("pack.logout_done"), "btn--secondary", function () { C.logout(); }));
      bodyEl.appendChild(box);
    }

    // --- bottom action bar ----------------------------------------------------------------

    function actionButton(label, className, wide, onClick) {
      var btn = button(label, className + (busy ? " btn--working" : "") + (wide ? " pack-actions__wide" : ""), onClick);
      btn.disabled = busy;
      return btn;
    }

    function renderActions() {
      clearNode(actionsEl);
      var line = currentLine();
      var show = false;
      if (editable && view === "line" && line && !line.withheld && (lineState(line) === "open" || editing[line.id])) {
        show = true;
        if (draft.mode === "main") {
          actionsEl.appendChild(actionButton(txt("action_ok"), "btn--primary", true, function () { sendAnswer("ok"); }));
          actionsEl.appendChild(actionButton(txt("action_missing"), "btn--secondary", false, function () { setMode("missing"); }));
          actionsEl.appendChild(actionButton(txt("action_notok"), "btn--danger", false, function () { setMode("notok"); }));
        } else {
          var result = draft.mode === "missing" ? "missing" : "not_ok";
          actionsEl.appendChild(actionButton(C.t("pack.cancel"), "", false, function () { setMode("main"); }));
          actionsEl.appendChild(actionButton(
            txt(result === "missing" ? "send_missing" : "send_notok"),
            result === "missing" ? "btn--primary" : "btn--danger", false, function () { sendAnswer(result); }
          ));
        }
      } else if (editable && view === "extra") {
        show = true;
        actionsEl.appendChild(actionButton(C.t("pack.cancel"), "", false, function () { view = "summary"; renderAll(); }));
        actionsEl.appendChild(actionButton(txt("extra_add"), "btn--primary", false, sendExtra));
      } else if (editable && view === "summary") {
        show = true;
        actionsEl.appendChild(actionButton(txt("finish"), "btn--primary", true, finish));
      }
      actionsEl.hidden = !show;
    }

    function renderAll() {
      renderHead();
      renderStrip();
      renderNav();
      if (view === "line") renderLineView();
      else if (view === "summary") renderSummaryView();
      else if (view === "extra") renderExtraView();
      else renderDoneView();
      renderActions();
    }

    // --- start ------------------------------------------------------------------------------

    // Swipe left / right = Weiter / Zurück. Short or mostly vertical moves are ignored; a swipe never confirms.
    var touch = null;
    bodyEl.addEventListener("touchstart", function (event) {
      touch = null;
      if (view !== "line" || !event.touches.length) return;
      var target = event.target;
      if (target && target.closest && target.closest("input,textarea,select")) return;
      touch = { x: event.touches[0].clientX, y: event.touches[0].clientY };
    }, { passive: true });
    bodyEl.addEventListener("touchend", function (event) {
      if (!touch || !event.changedTouches.length) return;
      var dx = event.changedTouches[0].clientX - touch.x;
      var dy = event.changedTouches[0].clientY - touch.y;
      touch = null;
      if (Math.abs(dx) < 60 || Math.abs(dx) < Math.abs(dy) * 1.5) return;
      step(dx < 0 ? 1 : -1);
    }, { passive: true });

    var open = openIndexes();
    if (editable && open.length) {
      idx = open[0];
      view = "line";
      newDraft(lines[idx]);
    } else {
      view = "summary";
    }
    renderAll();
  }

  window.Lines = { mount: mount, itemPicker: itemPicker };
})();
