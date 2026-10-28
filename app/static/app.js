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
    return el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName);
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
          if (el.dataset.live === 'op' && el.dataset.state === 'running' && next.dataset.state !== 'running') reload = true;
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
  function hideDismissedOp() {
    var btn = $('[data-dismiss-op]');
    if (btn && store.get('op-dismissed') === btn.dataset.dismissOp) btn.closest('.op').hidden = true;
  }
  var elapsedTimer = null;
  function tickElapsed() {
    clearInterval(elapsedTimer);
    var el = $('[data-elapsed]');
    if (!el) return;
    var n = parseInt(el.dataset.elapsed, 10) || 0;
    elapsedTimer = setInterval(function () { el.textContent = ++n; }, 1000);
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
    var amount = t.closest('[data-amount]');
    if (amount) {
      var inp = $('#chargeAmount');
      if (inp) { inp.value = Number(amount.dataset.amount).toLocaleString('en-US'); inp.focus(); }
      return;
    }
    var close = t.closest('[data-toast-close]');
    if (close) { close.closest('[data-toast]').remove(); return; }
    var dismiss = t.closest('[data-dismiss-op]');
    if (dismiss) { store.set('op-dismissed', dismiss.dataset.dismissOp); dismiss.closest('.op').hidden = true; return; }
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
      if (!on) $$('input[name=picks]', el).forEach(function (i) { i.checked = false; });
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
