// Session Browser — SPA
// Hash-based routing, vanilla JS, no frameworks

(function () {
  'use strict';

  // ---------------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------------
  let sessions = [];
  let totalCount = 0;
  let availableFilters = { projects: [], repos: [], branches: [] };
  let scannedAt = null;

  const defaults = {
    q: '',
    project: '',
    repo: '',
    branch: '',
    date_from: '',
    date_to: '',
    sort: 'start_time',
    order: 'desc',
    limit: 50,
    offset: 0,
  };

  let currentFilters = { ...defaults };

  // Debounce timer for search input
  let searchTimer = null;

  // ---------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------

  function qs(sel, root) {
    return (root || document).querySelector(sel);
  }

  function ce(tag, attrs, children) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (k === 'className') el.className = v;
        else if (k === 'textContent') el.textContent = v;
        else if (k === 'innerHTML') el.innerHTML = v;
        else if (k.startsWith('on')) el.addEventListener(k.slice(2).toLowerCase(), v);
        else el.setAttribute(k, v);
      }
    }
    if (children) {
      for (const c of Array.isArray(children) ? children : [children]) {
        if (typeof c === 'string') el.appendChild(document.createTextNode(c));
        else if (c) el.appendChild(c);
      }
    }
    return el;
  }

  /** Deterministic color from string hash — returns hsl string */
  function colorFromString(str) {
    let hash = 0;
    for (let i = 0; i < str.length; i++) {
      hash = str.charCodeAt(i) + ((hash << 5) - hash);
    }
    const hue = ((hash % 360) + 360) % 360;
    return `hsl(${hue}, 55%, 42%)`;
  }

  function formatDuration(seconds) {
    if (seconds == null || seconds <= 0) return '--';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m`;
    return `<1m`;
  }

  function formatCost(usd) {
    if (usd == null) return '--';
    return `$${Number(usd).toFixed(2)}`;
  }

  function formatDate(isoStr) {
    if (!isoStr) return '--';
    const d = new Date(isoStr);
    const now = new Date();
    const sameYear = d.getFullYear() === now.getFullYear();
    const isToday =
      d.getFullYear() === now.getFullYear() &&
      d.getMonth() === now.getMonth() &&
      d.getDate() === now.getDate();
    const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    const timeStr = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    if (isToday) return `Today, ${timeStr}`;

    const yesterday = new Date(now);
    yesterday.setDate(yesterday.getDate() - 1);
    const isYesterday =
      d.getFullYear() === yesterday.getFullYear() &&
      d.getMonth() === yesterday.getMonth() &&
      d.getDate() === yesterday.getDate();
    if (isYesterday) return `Yesterday, ${timeStr}`;

    // Within last 7 days — show "Mon 7, 10:21"
    const daysDiff = Math.floor((now - d) / 86400000);
    if (daysDiff < 7 && sameYear) {
      return `${months[d.getMonth()]} ${d.getDate()}, ${timeStr}`;
    }
    // Older — show "Mar 25"
    if (sameYear) return `${months[d.getMonth()]} ${d.getDate()}`;
    return `${months[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}`;
  }

  function safeParse(jsonStr) {
    if (!jsonStr) return null;
    try {
      return JSON.parse(jsonStr);
    } catch {
      return null;
    }
  }

  function truncate(str, len) {
    if (!str) return '';
    if (str.length <= len) return str;
    return str.slice(0, len) + '...';
  }

  // ---------------------------------------------------------------------------
  // URL hash <-> filter state
  // ---------------------------------------------------------------------------

  function filtersToHash(f) {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(f)) {
      if (v !== '' && v !== defaults[k]) {
        params.set(k, String(v));
      }
    }
    const str = params.toString();
    return str ? `#/?${str}` : '#/';
  }

  function hashToFilters() {
    const hash = location.hash || '#/';
    const f = { ...defaults };

    // Detail route
    const detailMatch = hash.match(/^#\/session\/(.+)$/);
    if (detailMatch) return { __route: 'detail', id: detailMatch[1] };

    // List route
    const qIdx = hash.indexOf('?');
    if (qIdx !== -1) {
      const params = new URLSearchParams(hash.slice(qIdx + 1));
      for (const [k, v] of params.entries()) {
        if (k in f) {
          f[k] = k === 'limit' || k === 'offset' ? Number(v) : v;
        }
      }
    }
    f.__route = 'list';
    return f;
  }

  function pushFilters(f) {
    const newHash = filtersToHash(f);
    if (location.hash !== newHash) {
      history.pushState(null, '', newHash);
    }
  }

  // ---------------------------------------------------------------------------
  // API
  // ---------------------------------------------------------------------------

  async function fetchSessions(filters) {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(filters)) {
      if (k.startsWith('__')) continue;
      if (v !== '' && v != null) params.set(k, String(v));
    }
    const resp = await fetch(`/api/sessions?${params.toString()}`);
    if (!resp.ok) throw new Error(`API error: ${resp.status}`);
    return resp.json();
  }

  async function reindex() {
    const btn = qs('#reindex-btn');
    btn.disabled = true;
    btn.textContent = 'Indexing...';
    try {
      const resp = await fetch('/api/reindex', { method: 'POST' });
      if (!resp.ok) throw new Error(`Reindex failed: ${resp.status}`);
      await loadList();
    } catch (err) {
      console.error('Reindex error:', err);
    } finally {
      btn.disabled = false;
      btn.textContent = '\u21BB Reindex';
    }
  }

  // ---------------------------------------------------------------------------
  // Date preset helpers
  // ---------------------------------------------------------------------------

  function datePreset(label) {
    const now = new Date();
    switch (label) {
      case 'today': {
        const d = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        return d.toISOString();
      }
      case '7d': {
        const d = new Date(now);
        d.setDate(d.getDate() - 7);
        return d.toISOString();
      }
      case '30d': {
        const d = new Date(now);
        d.setDate(d.getDate() - 30);
        return d.toISOString();
      }
      case 'all':
      default:
        return '';
    }
  }

  // ---------------------------------------------------------------------------
  // Rendering — List View
  // ---------------------------------------------------------------------------

  function renderFilterBar(container) {
    const bar = ce('div', { className: 'filter-bar' });

    // Search
    const searchInput = ce('input', {
      type: 'text',
      className: 'filter-search',
      placeholder: 'Search prompts...',
      value: currentFilters.q,
      onInput: (e) => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => {
          currentFilters.q = e.target.value;
          currentFilters.offset = 0;
          pushFilters(currentFilters);
          loadList();
        }, 300);
      },
    });
    bar.appendChild(searchInput);

    // Project dropdown
    const projectSelect = ce('select', {
      className: 'filter-select',
      onChange: (e) => {
        currentFilters.project = e.target.value;
        currentFilters.offset = 0;
        pushFilters(currentFilters);
        loadList();
      },
    });
    projectSelect.appendChild(ce('option', { value: '', textContent: 'All projects' }));
    for (const p of availableFilters.projects || []) {
      const opt = ce('option', { value: p, textContent: p });
      if (p === currentFilters.project) opt.selected = true;
      projectSelect.appendChild(opt);
    }
    bar.appendChild(projectSelect);

    // Repo dropdown
    const repoSelect = ce('select', {
      className: 'filter-select',
      onChange: (e) => {
        currentFilters.repo = e.target.value;
        currentFilters.offset = 0;
        pushFilters(currentFilters);
        loadList();
      },
    });
    repoSelect.appendChild(ce('option', { value: '', textContent: 'All repos' }));
    for (const r of availableFilters.repos || []) {
      const opt = ce('option', { value: r, textContent: r });
      if (r === currentFilters.repo) opt.selected = true;
      repoSelect.appendChild(opt);
    }
    bar.appendChild(repoSelect);

    // Date presets
    const dateGroup = ce('div', { className: 'date-presets' });
    const presets = [
      { label: 'Today', value: 'today' },
      { label: '7d', value: '7d' },
      { label: '30d', value: '30d' },
      { label: 'All', value: 'all' },
    ];
    for (const p of presets) {
      const isActive =
        (p.value === 'all' && !currentFilters.date_from) ||
        (p.value !== 'all' && currentFilters.date_from === datePreset(p.value));
      const btn = ce('button', {
        className: `date-btn${isActive ? ' active' : ''}`,
        textContent: p.label,
        onClick: () => {
          currentFilters.date_from = datePreset(p.value);
          currentFilters.date_to = '';
          currentFilters.offset = 0;
          pushFilters(currentFilters);
          loadList();
        },
      });
      dateGroup.appendChild(btn);
    }
    bar.appendChild(dateGroup);

    // Total count
    const countEl = ce('span', {
      className: 'session-count',
      textContent: `${totalCount} sessions`,
    });
    bar.appendChild(countEl);

    container.appendChild(bar);
  }

  function sortIndicator(col) {
    if (currentFilters.sort !== col) return '';
    return currentFilters.order === 'asc' ? ' \u25B2' : ' \u25BC';
  }

  function handleSort(col) {
    if (currentFilters.sort === col) {
      currentFilters.order = currentFilters.order === 'asc' ? 'desc' : 'asc';
    } else {
      currentFilters.sort = col;
      currentFilters.order = 'desc';
    }
    currentFilters.offset = 0;
    pushFilters(currentFilters);
    loadList();
  }

  function renderTable(container) {
    const table = ce('table', { className: 'session-table' });

    // Header
    const thead = ce('thead');
    const headerRow = ce('tr');

    const columns = [
      { key: 'start_time', label: 'Date', sortable: true },
      { key: 'repos', label: 'Repos', sortable: false },
      { key: 'first_prompt', label: 'First Prompt', sortable: false },
      { key: 'git_branch', label: 'Branch', sortable: true },
      { key: 'duration_seconds', label: 'Duration', sortable: true },
      { key: 'estimated_cost_usd', label: 'Cost', sortable: true },
      { key: 'turn_count', label: 'Turns', sortable: true },
    ];

    for (const col of columns) {
      const th = ce('th', {
        className: `col-${col.key}${currentFilters.sort === col.key ? ' sorted' : ''}${col.sortable ? ' sortable' : ''}`,
        textContent: col.label + (col.sortable ? sortIndicator(col.key) : ''),
      });
      if (col.sortable) {
        th.addEventListener('click', () => handleSort(col.key));
      }
      headerRow.appendChild(th);
    }
    thead.appendChild(headerRow);
    table.appendChild(thead);

    // Body
    const tbody = ce('tbody');
    if (sessions.length === 0) {
      const tr = ce('tr');
      tr.appendChild(ce('td', { colSpan: '7', className: 'empty-state', textContent: 'No sessions found' }));
      tbody.appendChild(tr);
    } else {
      for (const s of sessions) {
        const tr = ce('tr', { className: 'session-row' });
        tr.addEventListener('click', () => {
          location.hash = `#/session/${s.session_id}`;
        });

        // Date
        tr.appendChild(ce('td', { className: 'col-date', textContent: formatDate(s.start_time) }));

        // Repos
        const reposCell = ce('td', { className: 'col-repos' });
        const repos = safeParse(s.repos_touched) || [];
        for (const r of repos) {
          const pill = ce('span', {
            className: 'repo-pill',
            textContent: r,
          });
          pill.style.backgroundColor = colorFromString(r);
          reposCell.appendChild(pill);
        }
        tr.appendChild(reposCell);

        // First prompt
        tr.appendChild(
          ce('td', {
            className: 'col-prompt',
            textContent: truncate(s.first_prompt, 120),
            title: s.first_prompt || '',
          })
        );

        // Branch
        tr.appendChild(ce('td', { className: 'col-branch mono', textContent: s.git_branch || '--' }));

        // Duration
        tr.appendChild(ce('td', { className: 'col-duration', textContent: formatDuration(s.duration_seconds) }));

        // Cost
        tr.appendChild(ce('td', { className: 'col-cost', textContent: formatCost(s.estimated_cost_usd) }));

        // Turns
        tr.appendChild(ce('td', { className: 'col-turns', textContent: s.turn_count ?? '--' }));

        tbody.appendChild(tr);
      }
    }
    table.appendChild(tbody);
    container.appendChild(table);
  }

  function renderPagination(container) {
    const offset = currentFilters.offset;
    const limit = currentFilters.limit;
    const from = totalCount === 0 ? 0 : offset + 1;
    const to = Math.min(offset + limit, totalCount);

    const pag = ce('div', { className: 'pagination' });

    const info = ce('span', {
      className: 'pagination-info',
      textContent: `Showing ${from}-${to} of ${totalCount}`,
    });
    pag.appendChild(info);

    const buttons = ce('div', { className: 'pagination-buttons' });

    const prevBtn = ce('button', {
      className: 'pagination-btn',
      textContent: '\u2190 Prev',
      disabled: offset === 0,
      onClick: () => {
        currentFilters.offset = Math.max(0, offset - limit);
        pushFilters(currentFilters);
        loadList();
      },
    });
    buttons.appendChild(prevBtn);

    const nextBtn = ce('button', {
      className: 'pagination-btn',
      textContent: 'Next \u2192',
      disabled: offset + limit >= totalCount,
      onClick: () => {
        currentFilters.offset = offset + limit;
        pushFilters(currentFilters);
        loadList();
      },
    });
    buttons.appendChild(nextBtn);

    pag.appendChild(buttons);
    container.appendChild(pag);
  }

  function renderList() {
    const content = qs('#content');
    content.innerHTML = '';

    const wrapper = ce('div', { className: 'list-view' });
    renderFilterBar(wrapper);
    renderTable(wrapper);
    renderPagination(wrapper);
    content.appendChild(wrapper);
  }

  async function loadList() {
    try {
      const data = await fetchSessions(currentFilters);
      sessions = data.sessions || [];
      totalCount = data.total || 0;
      availableFilters = data.filters || { projects: [], repos: [], branches: [] };
      scannedAt = data.scanned_at;
      renderList();
    } catch (err) {
      console.error('Failed to load sessions:', err);
      const content = qs('#content');
      content.innerHTML = '';
      content.appendChild(
        ce('div', { className: 'error-state', textContent: `Failed to load sessions: ${err.message}` })
      );
    }
  }

  // ---------------------------------------------------------------------------
  // Rendering — Detail View (stub)
  // ---------------------------------------------------------------------------

  function renderDetail(sessionId) {
    const content = qs('#content');
    content.innerHTML = '';

    const wrapper = ce('div', { className: 'detail-view' });
    wrapper.appendChild(
      ce('a', {
        href: '#/',
        className: 'back-link',
        textContent: '\u2190 Back to sessions',
      })
    );
    wrapper.appendChild(ce('p', { className: 'loading-msg', textContent: `Loading session ${sessionId}...` }));
    content.appendChild(wrapper);
  }

  // ---------------------------------------------------------------------------
  // Router
  // ---------------------------------------------------------------------------

  function route() {
    const parsed = hashToFilters();
    if (parsed.__route === 'detail') {
      renderDetail(parsed.id);
      return;
    }
    // List view — update current filters from hash
    const { __route, ...filters } = parsed;
    currentFilters = { ...defaults, ...filters };
    loadList();
  }

  // ---------------------------------------------------------------------------
  // Theme toggle
  // ---------------------------------------------------------------------------

  function initTheme() {
    const saved = localStorage.getItem('sb-theme');
    if (saved) document.documentElement.setAttribute('data-theme', saved);

    qs('#theme-toggle').addEventListener('click', () => {
      const current = document.documentElement.getAttribute('data-theme') || 'dark';
      const next = current === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      localStorage.setItem('sb-theme', next);
    });
  }

  // ---------------------------------------------------------------------------
  // Init
  // ---------------------------------------------------------------------------

  function init() {
    initTheme();

    qs('#home-link').addEventListener('click', (e) => {
      e.preventDefault();
      location.hash = '#/';
    });

    qs('#reindex-btn').addEventListener('click', reindex);

    window.addEventListener('hashchange', route);
    route();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
