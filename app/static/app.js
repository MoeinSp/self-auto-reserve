// Panel behaviour: tabs, live refresh without full reloads, small form helpers.
(function () {
  'use strict';

  var store = {
    get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
  };
  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  // ------------------------------------------------------------------ theme
  var THEMES = ['auto', 'light', 'dark'];
  var THEME_FA = { auto: 'خودکار', light: 'روشن', dark: 'تیره' };
  function applyTheme(t) {
    if (t === 'auto') delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = t;
    $$('[data-theme-label]').forEach(function (el) { el.textContent = THEME_FA[t]; });
  }
  function currentTheme() { return store.get('theme') || 'auto'; }

  // ------------------------------------------------------------------- tabs
  var tabsKey = 'tab:' + (document.body.dataset.tabsKey || location.pathname);
  function panels() { return $$('[data-panel]'); }
  function activate(name, push) {
    var list = panels();
    if (!list.length) return;
    if (!list.some(function (p) { return p.id === name; })) name = list[0].id;
    list.forEach(function (p) { p.classList.toggle('on', p.id === name); });
    $$('[data-tab]').forEach(function (a) {
      var on = a.dataset.tab === name;
      a.classList.toggle('on', on);
      if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
    store.set(tabsKey, name);
    var url = location.pathname + location.search + '#' + name;
    if (location.hash !== '#' + name) history[push ? 'pushState' : 'replaceState'](null, '', url);
  }
  function initTabs() {
    if (!panels().length) return;
    var fromHash = location.hash.slice(1);
    activate(fromHash || store.get(tabsKey) || panels()[0].id, false);
    document.addEventListener('click', function (e) {
      var a = e.target.closest('a[href^="#"]');
      if (!a) return;
      var name = a.getAttribute('href').slice(1);
      if (!document.getElementById(name) || !document.getElementById(name).hasAttribute('data-panel')) return;
      e.preventDefault();
      activate(name, true);
      window.scrollTo(0, 0);
    });
    window.addEventListener('popstate', function () { activate(location.hash.slice(1), false); });
  }

  // ------------------------------------------------------------ live refresh
  // Re-fetch the page and swap only the [data-live] regions that changed, so
  // scroll position, open sections and half-typed forms are kept.
  var timer = null;
  var doneCount = null;
  function typing() {
    var el = document.activeElement;
    var sheet = $('#cellSheet');
    return (el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) || !!(sheet && sheet.open);
  }
  function schedule(ms) {
    clearTimeout(timer);
    if (ms > 0) timer = setTimeout(refresh, ms);
  }
  function refresh() {
    if (document.hidden) return schedule(5000);
    fetch(location.pathname + location.search, { credentials: 'same-origin', headers: { 'X-Live': '1' } })
      .then(function (r) {
        if (r.redirected && /\/login/.test(r.url)) { location.href = '/login'; return null; }
        return r.ok ? r.text() : null;
      })
      .then(function (html) {
        if (!html) return schedule(15000);
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var reload = false;
        $$('[data-live]').forEach(function (el) {
          var next = doc.querySelector('[data-live="' + el.dataset.live + '"]');
          if (!next) return;
          // An operation finished (ops are queued, so count them): reload for its results.
          if (el.dataset.live === 'op' && (parseInt(next.dataset.done, 10) || 0) > (parseInt(el.dataset.done, 10) || 0)) reload = true;
          if (el.contains(document.activeElement)) return;
          if (el.dataset.key !== undefined && el.dataset.key === next.dataset.key) return;
          if (el.innerHTML === next.innerHTML) return;
          el.replaceWith(document.importNode(next, true));
        });
        afterSwap();
        // An operation finished: menu, credit or names may have changed elsewhere on the page.
        if (reload && !typing()) return location.reload();
        schedule(parseInt(doc.body.dataset.livePoll, 10) || 0);
      })
      .catch(function () { schedule(15000); });
  }
  function afterSwap() {
    paintPending();
    applyJobFilter();
    hideDismissedOp();
    tickElapsed();
    var jobs = $('.jobs[data-done]');
    if (jobs) {
      var n = parseInt(jobs.dataset.done, 10) || 0;
      if (doneCount !== null && n > doneCount) {
        if (navigator.vibrate) navigator.vibrate([120, 60, 120]);
        document.title = '✅ خرید انجام شد · سلف خودکار';
      }
      doneCount = n;
    }
  }

  // ------------------------------------------------------------ job filter
  function applyJobFilter() {
    var box = $('[data-job-filter]');
    if (!box) return;
    var checked = $('input:checked', box);
    var f = checked ? checked.value : 'all';
    $$('#jobs .job').forEach(function (j) {
      var s = j.dataset.status;
      j.hidden = f === 'active' ? s !== 'active' : f === 'rest' ? s === 'active' : false;
    });
  }

  // ------------------------------------------------------- operation card
  function dismissedOps() { return (store.get('ops-dismissed') || '').split(',').filter(Boolean); }
  function hideDismissedOp() {
    var gone = dismissedOps();
    $$('[data-dismiss-op]').forEach(function (btn) {
      if (gone.indexOf(btn.dataset.dismissOp) >= 0) btn.closest('.op').hidden = true;
    });
  }
  var elapsedTimer = null;
  function tickElapsed() {
    clearInterval(elapsedTimer);
    var els = $$('[data-elapsed]');
    if (!els.length) return;
    elapsedTimer = setInterval(function () {
      els.forEach(function (el) { el.textContent = (parseInt(el.textContent, 10) || 0) + 1; });
    }, 1000);
  }

  // ---------------------------------------------------------------- events
  document.addEventListener('click', function (e) {
    var t = e.target;
    var pill = t.closest('.date-pill');
    if (pill) {
      var input = $('#dateInput');
      if (input) input.value = pill.dataset.date;
      var badge = $('#dateBadge');
      if (badge) badge.textContent = pill.dataset.date;
      $$('.date-pill').forEach(function (p) {
        p.classList.toggle('on', p === pill);
        p.setAttribute('aria-checked', p === pill ? 'true' : 'false');
      });
      showDayMenu(pill.dataset.date);
      return;
    }
    var add = t.closest('[data-add-food]');
    if (add) {
      // Append a suggested food to its priority list (once).
      var box = document.getElementById(add.dataset.target);
      if (box) {
        var lines = box.value.split('\n').map(function (l) { return l.trim(); }).filter(Boolean);
        if (lines.indexOf(add.dataset.addFood) < 0) lines.push(add.dataset.addFood);
        box.value = lines.join('\n');
        add.classList.add('used');
      }
      return;
    }
    var amount = t.closest('[data-amount]');
    if (amount) {
      var inp = $('#chargeAmount');
      if (inp) { inp.value = Number(amount.dataset.amount).toLocaleString('en-US'); inp.focus(); }
      return;
    }
    var close = t.closest('[data-toast-close]');
    if (close) { close.closest('[data-toast]').remove(); return; }
    var dismiss = t.closest('[data-dismiss-op]');
    if (dismiss) {
      store.set('ops-dismissed', dismissedOps().concat(dismiss.dataset.dismissOp).slice(-30).join(','));
      dismiss.closest('.op').hidden = true;
      return;
    }
    var cell = t.closest('[data-cell]');
    if (cell) { openSheet(cell); return; }
    if (t.id === 'cellSheet' || t.closest('[data-sheet-close]')) { closeSheet(); return; }  // ×, or a tap on the backdrop
    if (t.closest('[data-sheet-apply]')) { applySheet(); return; }
    if (t.closest('[data-sheet-undo]')) { delete pending[sheetKey]; savePending(); closeSheet(); return; }
    if (t.closest('[data-batch-clear]')) { pending = {}; savePending(); return; }
    if (t.closest('[data-theme-toggle]')) {
      var next = THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length];
      store.set('theme', next);
      applyTheme(next);
      return;
    }
    var confirmEl = t.closest('[data-confirm]');
    if (confirmEl && !window.confirm(confirmEl.dataset.confirm)) { e.preventDefault(); return; }
    // Close the user menu when clicking elsewhere.
    $$('details.menu[open]').forEach(function (d) { if (!d.contains(t)) d.open = false; });
  });

  document.addEventListener('change', function (e) {
    var t = e.target;
    if (t.matches('[data-autosubmit]')) t.form.requestSubmit ? t.form.requestSubmit() : t.form.submit();
    if (t.closest('[data-job-filter]')) applyJobFilter();
    if (t.id === 'dateInput') { var b = $('#dateBadge'); if (b) b.textContent = t.value; showDayMenu(t.value.trim()); }
    if (t.matches('[data-cap-toggle]')) syncCap(t);
  });

  document.addEventListener('input', function (e) {
    if (e.target.matches('[data-speed]')) speedWarn(e.target);
  });

  // Show the stored week menu of the chosen day; ticks on other days are cleared.
  function showDayMenu(date) {
    var found = false;
    $$('[data-menu-date]').forEach(function (el) {
      var on = el.dataset.menuDate === date;
      found = found || on;
      el.hidden = !on;
      if (!on) $$('input[name=picks], input[name=sells]', el).forEach(function (i) { i.checked = false; });
    });
    var empty = $('[data-menu-empty]');
    if (empty) empty.hidden = found;
  }

  function speedWarn(input) {
    var warn = $('[data-speed-warn]');
    if (warn) warn.hidden = !(parseFloat(input.value) < 5);
  }
  function syncCap(box) {
    var input = document.getElementById(box.dataset.capToggle);
    if (input) input.disabled = box.checked;
  }

  // Prevent double submits (slow site operations) and show progress.
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (form.method === 'dialog') return;  // closes a sheet; nothing is sent
    if (form.dataset.sending) { e.preventDefault(); return; }
    form.dataset.sending = '1';
    var btn = e.submitter || $('button:not([type=button])', form);
    if (btn) setTimeout(function () { btn.disabled = true; btn.classList.add('busy'); }, 0);
  });
  // Coming back via the back button restores a page whose buttons were disabled.
  window.addEventListener('pageshow', function (e) {
    if (e.persisted) $$('form[data-sending]').forEach(function (f) {
      delete f.dataset.sending;
      $$('button.busy', f).forEach(function (b) { b.disabled = false; b.classList.remove('busy'); });
    });
  });

  // ------------------------------------------------- reservations table editor
  // Tapping a cell opens a sheet: reserve / change / cancel (days that aren't locked), put a
  // reserved meal in the basket, take it out, or buy from the basket. Choices wait in
  // `pending` (kept in sessionStorage across reloads) and are sent together by #batchForm.
  var pending = {};
  var sheetKey = null;
  var ss = {
    get: function (k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { sessionStorage.setItem(k, v); } catch (e) {} },
  };
  function batchForm() { return $('#batchForm'); }
  function pendingKey() { var f = batchForm(); return f ? 'pending:' + f.dataset.account : null; }
  function loadPending() {
    var k = pendingKey();
    try { pending = (k && JSON.parse(ss.get(k) || '{}')) || {}; } catch (e) { pending = {}; }
  }
  function savePending() {
    var k = pendingKey();
    if (k) ss.set(k, JSON.stringify(pending));
    paintPending();
  }
  function cellFor(key) {
    var p = key.split('|');
    return $('[data-cell][data-date="' + p[0] + '"][data-meal="' + p[1] + '"]');
  }
  function describe(ch) {
    if (ch.pick === '-') return 'لغو رزرو';
    if (ch.pick) return '← ' + ch.pick;
    if (ch.sell) return 'به سبد';
    if (ch.unsell) return 'از سبد بردار';
    if (ch.buy && ch.buy.length) return 'خرید از سبد';
    return '';
  }
  function paintPending() {
    var form = batchForm();
    if (!form) return;
    $$('[data-cell].pending').forEach(function (c) {
      c.classList.remove('pending');
      var tag = $('.pend-tag', c);
      if (tag) tag.remove();
    });
    var list = [];
    Object.keys(pending).forEach(function (key) {
      var c = cellFor(key);
      var text = describe(pending[key]);
      if (!c || !text) { delete pending[key]; return; }  // the cell changed meanwhile
      c.classList.add('pending');
      var tag = document.createElement('small');
      tag.className = 'pend-tag';
      tag.textContent = text;
      c.appendChild(tag);
      list.push(c.dataset.meal + ' ' + c.dataset.day.split(' ')[0] + ': ' + text);
    });
    var n = list.length;
    form.hidden = !n;
    document.body.classList.toggle('has-batch', !!n);
    $('[data-batch-count]', form).textContent = n;
    $('[data-batch-list]', form).textContent = list.join(' · ');
  }
  function option(type, name, value, text, checked, extra) {
    var label = document.createElement('label');
    label.className = 'pick' + (extra ? ' ' + extra : '');
    var input = document.createElement('input');
    input.type = type; input.name = name; input.value = value; input.checked = !!checked;
    var span = document.createElement('span');
    span.textContent = text;
    label.appendChild(input); label.appendChild(span);
    return label;
  }
  function openSheet(cell) {
    var sheet = $('#cellSheet');
    if (!sheet || !sheet.showModal) return;
    sheetKey = cell.dataset.date + '|' + cell.dataset.meal;
    var st = cell.dataset.state, food = cell.dataset.food, locked = cell.dataset.locked === '1';
    var foods = [];
    try { foods = JSON.parse(cell.dataset.foods || '[]'); } catch (e) {}
    var ch = pending[sheetKey] || {};
    $('#sheetTitle').textContent = cell.dataset.meal + ' · ' + cell.dataset.day;
    $('[data-sheet-status]', sheet).textContent = {
      reserved: 'رزرو داری: ' + food, listed: 'داخل سبد، در انتظار فروش: ' + food, free: 'رزرو نداری',
    }[st] || '';

    // Reserve / change / cancel: only days that aren't locked, and not while it waits in the basket.
    var canPick = !locked && st !== 'listed' && foods.length > 0;
    var box = $('[data-sheet-foods]', sheet);
    box.innerHTML = '';
    if (canPick) {
      foods.forEach(function (f) {
        box.appendChild(option('radio', 'sheet-pick', f, f, ch.pick ? ch.pick === f : f === food));
      });
      box.appendChild(option('radio', 'sheet-pick', '-', st === 'reserved' ? 'لغو رزرو' : 'رزرو نکن',
        ch.pick ? ch.pick === '-' : st !== 'reserved', 'none-pick'));
    }
    $('[data-sheet-reserve]', sheet).hidden = !canPick;
    $('[data-sheet-locked]', sheet).hidden = !locked;

    var bbox = $('[data-sheet-basket-opts]', sheet);
    bbox.innerHTML = '';
    if (st === 'reserved') {
      bbox.appendChild(option('checkbox', 'sheet-sell', '1', 'گذاشتن در سبد برای فروش (رزروت فروخته می‌شود)', ch.sell));
    } else if (st === 'listed') {
      bbox.appendChild(option('checkbox', 'sheet-unsell', '1', 'برداشتن از سبد (رزرو برای خودت می‌ماند)', ch.unsell));
    } else if (st === 'free') {
      var hint = document.createElement('p');
      hint.className = 'muted small';
      hint.textContent = 'خرید از سبد: هر کدام زودتر در سبد بیاید خریده می‌شود.';
      bbox.appendChild(hint);
      foods.forEach(function (f) {
        bbox.appendChild(option('checkbox', 'sheet-buy', f, f, (ch.buy || []).indexOf(f) >= 0));
      });
    }
    $('[data-sheet-basket]', sheet).hidden = !bbox.children.length;
    $('[data-sheet-undo]', sheet).hidden = !pending[sheetKey];
    sheet.showModal();
  }
  function closeSheet() {
    var sheet = $('#cellSheet');
    if (sheet && sheet.open) sheet.close();
    sheetKey = null;
  }
  function applySheet() {
    var sheet = $('#cellSheet');
    var cell = sheetKey && cellFor(sheetKey);
    if (!sheet || !cell) return closeSheet();
    var st = cell.dataset.state, food = cell.dataset.food;
    var ch = {};
    var pick = $('input[name="sheet-pick"]:checked', sheet);
    if (pick && pick.value !== (st === 'reserved' ? food : '-')) ch.pick = pick.value;
    var sell = $('input[name="sheet-sell"]:checked', sheet);
    var unsell = $('input[name="sheet-unsell"]:checked', sheet);
    var buy = $$('input[name="sheet-buy"]:checked', sheet).map(function (i) { return i.value; });
    if (ch.pick) {
      // A new reservation choice wins; basket actions for the same meal don't mix with it.
      if (ch.pick !== '-' && st === 'free' && buy.length) ch.buy = [];
    } else {
      if (sell) ch.sell = true;
      if (unsell) ch.unsell = true;
      if (buy.length) ch.buy = buy;
    }
    if (describe(ch)) pending[sheetKey] = ch; else delete pending[sheetKey];
    savePending();
    closeSheet();
  }
  // Turn `pending` into the form fields /batch reads, right before sending.
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (form.id !== 'batchForm') return;
    $$('input[data-gen]', form).forEach(function (i) { i.remove(); });
    function add(name, value) {
      var i = document.createElement('input');
      i.type = 'hidden'; i.name = name; i.value = value; i.dataset.gen = '1';
      form.appendChild(i);
    }
    Object.keys(pending).forEach(function (key) {
      var p = key.split('|'), ch = pending[key];
      if (ch.pick) add('pick:' + p[0] + ':' + p[1], ch.pick);
      if (ch.sell) add('sell', key);
      if (ch.unsell) add('unsell', key);
      (ch.buy || []).forEach(function (f) { add('buy:' + p[0] + ':' + p[1], f); });
    });
    pending = {};
    var k = pendingKey();
    if (k) ss.set(k, '{}');
  }, true);

  // ------------------------------------------------------------------ init
  function init() {
    applyTheme(currentTheme());
    initTabs();
    var toast = $('[data-toast]');
    if (toast) setTimeout(function () {
      toast.classList.add('hide');
      setTimeout(function () { toast.remove(); }, 300);
    }, 6000);
    var speed = $('[data-speed]');
    if (speed) speedWarn(speed);
    $$('[data-cap-toggle]').forEach(syncCap);
    loadPending();
    afterSwap();
    var poll = parseInt(document.body.dataset.livePoll, 10);
    if (poll > 0) schedule(poll);
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden && poll > 0) schedule(500);
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
