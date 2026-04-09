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

  /** Clean command markup from prompts.
   *  Input like: <command-message>brief</command-message>\n<command-name>/brief</command-name>\n<command-args>I had two meetings</command-args>
   *  Output: /brief I had two meetings
   */
  function cleanPrompt(str) {
    if (!str) return '';
    // Detect command-message XML pattern
    const nameMatch = str.match(/<command-name>\s*(\/[^<]+?)\s*<\/command-name>/);
    if (nameMatch) {
      const name = nameMatch[1].trim();
      const argsMatch = str.match(/<command-args>\s*([\s\S]*?)\s*<\/command-args>/);
      const args = argsMatch ? argsMatch[1].trim() : '';
      return args ? `${name} ${args}` : name;
    }
    // Fallback: strip any remaining XML tags
    return str.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
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

    // Subagent route: #/session/{id}/subagent/{agentId}
    const subagentMatch = hash.match(/^#\/session\/([^/]+)\/subagent\/(.+)$/);
    if (subagentMatch) return { __route: 'subagent', id: subagentMatch[1], agentId: subagentMatch[2] };

    // Detail route
    const detailMatch = hash.match(/^#\/session\/([^/]+)$/);
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

    // Workspace dropdown (merged projects + repos)
    const workspaceSelect = ce('select', {
      className: 'filter-select',
      onChange: (e) => {
        currentFilters.repo = e.target.value;
        currentFilters.project = '';
        currentFilters.offset = 0;
        pushFilters(currentFilters);
        loadList();
      },
    });
    workspaceSelect.appendChild(ce('option', { value: '', textContent: 'All workspaces' }));
    for (const r of availableFilters.repos || []) {
      const opt = ce('option', { value: r, textContent: r });
      if (r === currentFilters.repo) opt.selected = true;
      workspaceSelect.appendChild(opt);
    }
    bar.appendChild(workspaceSelect);

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
            textContent: truncate(cleanPrompt(s.first_prompt), 120),
            title: cleanPrompt(s.first_prompt) || '',
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
  // Rendering — Detail View
  // ---------------------------------------------------------------------------

  // Configure marked to use highlight.js for code blocks
  marked.setOptions({
    highlight: function (code, lang) {
      if (lang && hljs.getLanguage(lang)) {
        return hljs.highlight(code, { language: lang }).value;
      }
      return hljs.highlightAuto(code).value;
    },
  });

  /** Ensure a value is an object/array — handles JSON-string fields from the API */
  function ensureObj(val) {
    if (val == null) return null;
    if (typeof val === 'string') return safeParse(val);
    return val;
  }

  /** Format a timestamp as "HH:MM:SS" */
  function formatTimestamp(isoStr) {
    if (!isoStr) return '';
    const d = new Date(isoStr);
    return (
      String(d.getHours()).padStart(2, '0') +
      ':' +
      String(d.getMinutes()).padStart(2, '0') +
      ':' +
      String(d.getSeconds()).padStart(2, '0')
    );
  }

  /** Format full date + time for the detail header */
  function formatFullDate(isoStr) {
    if (!isoStr) return '--';
    const d = new Date(isoStr);
    const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    const days = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
    const time =
      String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
    return `${days[d.getDay()]}, ${months[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()} at ${time}`;
  }

  /** Build the tool summary string, e.g. "42 turns · 15 Bash · 8 Read" */
  function buildToolSummary(session) {
    const parts = [];
    if (session.turn_count != null) parts.push(`${session.turn_count} turns`);
    const toolCounts = ensureObj(session.tool_counts);
    if (toolCounts) {
      for (const [name, count] of Object.entries(toolCounts)) {
        parts.push(`${count} ${name}`);
      }
    }
    return parts.join(' \u00B7 ');
  }

  /** Get a one-character icon/prefix for a tool call */
  function toolIcon(toolName) {
    switch (toolName) {
      case 'Bash':
        return '$';
      case 'Read':
      case 'Write':
      case 'Edit':
        return '\uD83D\uDCC4'; // page emoji
      case 'Agent':
      case 'Task':
        return '\uD83E\uDD16'; // robot emoji
      case 'Grep':
      case 'Glob':
        return '\uD83D\uDD0D'; // magnifying glass
      default:
        return '\u2699'; // gear
    }
  }

  /** Render a single tool-call block (collapsed by default) */
  function renderToolCall(block, sessionId) {
    const wrapper = ce('div', { className: 'tool-call' });
    const isError = block.result && block.result.is_error;

    // Summary line (always visible)
    const summary = ce('div', { className: 'tool-call-summary' });
    const icon = ce('span', { className: 'tool-icon', textContent: toolIcon(block.name) });
    summary.appendChild(icon);

    const summaryText = block.summary || `${block.name}`;
    summary.appendChild(ce('span', { className: 'tool-summary-text mono', textContent: summaryText }));

    if (isError) {
      summary.appendChild(ce('span', { className: 'tool-error-badge', textContent: 'ERROR' }));
    }

    // Chevron
    const chevron = ce('span', { className: 'tool-chevron', textContent: '\u25B6' });
    summary.appendChild(chevron);

    // Detail (hidden by default)
    const detail = ce('div', { className: 'tool-call-detail hidden' });

    // Input
    if (block.input) {
      detail.appendChild(ce('div', { className: 'tool-detail-label', textContent: 'Input' }));
      const inputContent =
        typeof block.input === 'string' ? block.input : JSON.stringify(block.input, null, 2);
      const inputPre = ce('pre', { className: 'tool-input mono' });
      inputPre.appendChild(ce('code', { textContent: inputContent }));
      detail.appendChild(inputPre);
    }

    // Result
    if (block.result) {
      detail.appendChild(ce('div', { className: 'tool-detail-label', textContent: 'Result' }));
      const resultText = block.result.text || '';
      const resultDiv = ce('div', {
        className: 'tool-result' + (isError ? ' error' : ''),
      });

      const sizeBytes = block.result.size_bytes || resultText.length;
      const lines = resultText.split('\n');
      const isSkill = block.name === 'Skill';

      if (!isSkill && (sizeBytes < 2000 || lines.length <= 50)) {
        const pre = ce('pre', { className: 'tool-result-content mono' });
        pre.appendChild(ce('code', { textContent: resultText }));
        resultDiv.appendChild(pre);
      } else {
        // Show first 50 lines with "Show more"
        const truncated = lines.slice(0, 50).join('\n');
        const pre = ce('pre', { className: 'tool-result-content mono' });
        pre.appendChild(ce('code', { textContent: truncated }));
        resultDiv.appendChild(pre);

        const showMore = ce('button', {
          className: 'show-more-btn',
          textContent: `Show all ${lines.length} lines`,
          onClick: () => {
            pre.textContent = '';
            pre.appendChild(ce('code', { textContent: resultText }));
            showMore.remove();
          },
        });
        resultDiv.appendChild(showMore);
      }
      detail.appendChild(resultDiv);
    }

    // Subagent link
    if (block.subagent_id && sessionId) {
      const link = ce('a', {
        className: 'subagent-link',
        href: `#/session/${sessionId}/subagent/${block.subagent_id}`,
        textContent: 'View subagent transcript \u2192',
      });
      detail.appendChild(link);
    }

    // Toggle
    summary.addEventListener('click', () => {
      const isOpen = !detail.classList.contains('hidden');
      detail.classList.toggle('hidden');
      chevron.textContent = isOpen ? '\u25B6' : '\u25BC';
      wrapper.classList.toggle('open', !isOpen);
    });

    wrapper.appendChild(summary);
    wrapper.appendChild(detail);
    return wrapper;
  }

  /** Render a thinking block (collapsed by default) */
  function renderThinkingBlock(block) {
    const wrapper = ce('div', { className: 'thinking-block' });
    const toggle = ce('div', {
      className: 'thinking-toggle',
      innerHTML: '<span class="thinking-icon">\uD83D\uDCAD</span> <em>Thinking...</em>',
    });

    const content = ce('div', {
      className: 'thinking-content hidden',
      innerHTML: marked.parse(block.text || ''),
    });

    toggle.addEventListener('click', () => {
      content.classList.toggle('hidden');
      wrapper.classList.toggle('open');
    });

    wrapper.appendChild(toggle);
    wrapper.appendChild(content);
    return wrapper;
  }

  /** Render a single assistant message (contains multiple blocks) */
  function renderAssistantMessage(msg, sessionId) {
    const wrapper = ce('div', { className: 'message assistant' });

    // Timestamp
    if (msg.timestamp) {
      wrapper.appendChild(
        ce('div', { className: 'msg-timestamp', textContent: formatTimestamp(msg.timestamp) })
      );
    }

    const blocks = msg.blocks || [];
    for (const block of blocks) {
      switch (block.type) {
        case 'text':
          wrapper.appendChild(
            ce('div', { className: 'msg-text', innerHTML: marked.parse(block.text || '') })
          );
          break;
        case 'thinking':
          wrapper.appendChild(renderThinkingBlock(block));
          break;
        case 'tool_use':
          wrapper.appendChild(renderToolCall(block, sessionId));
          break;
        default:
          // Unknown block type — render as text if possible
          if (block.text) {
            wrapper.appendChild(
              ce('div', { className: 'msg-text', innerHTML: marked.parse(block.text) })
            );
          }
          break;
      }
    }

    return wrapper;
  }

  /** Render a user message */
  function renderUserMessage(msg) {
    const wrapper = ce('div', { className: 'message user' });
    if (msg.timestamp) {
      wrapper.appendChild(
        ce('div', { className: 'msg-timestamp', textContent: formatTimestamp(msg.timestamp) })
      );
    }
    wrapper.appendChild(
      ce('div', { className: 'msg-text', innerHTML: marked.parse(msg.content || '') })
    );
    return wrapper;
  }

  /** Render the conversation list */
  function renderConversation(conversation, sessionId) {
    const container = ce('div', { className: 'conversation' });
    if (!conversation || conversation.length === 0) {
      container.appendChild(
        ce('div', { className: 'empty-conversation', textContent: 'No conversation data available.' })
      );
      return container;
    }

    for (const msg of conversation) {
      if (msg.type === 'user') {
        container.appendChild(renderUserMessage(msg));
      } else if (msg.type === 'assistant') {
        container.appendChild(renderAssistantMessage(msg, sessionId));
      }
    }

    return container;
  }

  /** Build the detail header metadata bar */
  function renderDetailHeader(session) {
    const header = ce('div', { className: 'detail-header' });

    // Date
    header.appendChild(
      ce('div', { className: 'detail-meta-item' }, [
        ce('span', { className: 'meta-label', textContent: 'Date' }),
        ce('span', { className: 'meta-value', textContent: formatFullDate(session.start_time) }),
      ])
    );

    // Duration
    header.appendChild(
      ce('div', { className: 'detail-meta-item' }, [
        ce('span', { className: 'meta-label', textContent: 'Duration' }),
        ce('span', { className: 'meta-value', textContent: formatDuration(session.duration_seconds) }),
      ])
    );

    // Project
    if (session.project_name) {
      header.appendChild(
        ce('div', { className: 'detail-meta-item' }, [
          ce('span', { className: 'meta-label', textContent: 'Project' }),
          ce('span', { className: 'meta-value', textContent: session.project_name }),
        ])
      );
    }

    // Branch
    if (session.git_branch) {
      header.appendChild(
        ce('div', { className: 'detail-meta-item' }, [
          ce('span', { className: 'meta-label', textContent: 'Branch' }),
          ce('span', { className: 'meta-value mono', textContent: session.git_branch }),
        ])
      );
    }

    // Cost
    header.appendChild(
      ce('div', { className: 'detail-meta-item' }, [
        ce('span', { className: 'meta-label', textContent: 'Cost' }),
        ce('span', { className: 'meta-value', textContent: formatCost(session.estimated_cost_usd) }),
      ])
    );

    // Tool summary
    const toolStr = buildToolSummary(session);
    if (toolStr) {
      header.appendChild(
        ce('div', { className: 'detail-meta-item detail-meta-tools' }, [
          ce('span', { className: 'meta-label', textContent: 'Activity' }),
          ce('span', { className: 'meta-value', textContent: toolStr }),
        ])
      );
    }

    return header;
  }

  /** Main detail view — fetches session data and renders */
  async function renderDetail(sessionId) {
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
    wrapper.appendChild(ce('p', { className: 'loading-msg', textContent: 'Loading session...' }));
    content.appendChild(wrapper);

    try {
      const resp = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`);
      if (!resp.ok) throw new Error(`API error: ${resp.status}`);
      const data = await resp.json();

      wrapper.innerHTML = '';

      // Back link
      wrapper.appendChild(
        ce('a', {
          href: '#/',
          className: 'back-link',
          textContent: '\u2190 Back to sessions',
        })
      );

      // Header metadata
      if (data.session) {
        wrapper.appendChild(renderDetailHeader(data.session));
      }

      // Conversation
      wrapper.appendChild(renderConversation(data.conversation, sessionId));

      // Jump to bottom FAB
      const fab = ce('button', {
        className: 'jump-bottom-fab',
        textContent: '\u2193 Bottom',
        onClick: () => window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' }),
      });
      wrapper.appendChild(fab);

      // Show/hide FAB based on scroll position
      const handleScroll = () => {
        const nearBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 200;
        fab.classList.toggle('hidden', nearBottom);
      };
      window.addEventListener('scroll', handleScroll);
      handleScroll();

      // Scroll to top on load
      window.scrollTo(0, 0);
    } catch (err) {
      console.error('Failed to load session detail:', err);
      wrapper.innerHTML = '';
      wrapper.appendChild(
        ce('a', { href: '#/', className: 'back-link', textContent: '\u2190 Back to sessions' })
      );
      wrapper.appendChild(
        ce('div', {
          className: 'error-state',
          textContent: `Failed to load session: ${err.message}`,
        })
      );
    }
  }

  /** Subagent detail view */
  async function renderSubagent(sessionId, agentId) {
    const content = qs('#content');
    content.innerHTML = '';

    const wrapper = ce('div', { className: 'detail-view' });

    // Breadcrumb
    const breadcrumb = ce('div', { className: 'breadcrumb' });
    breadcrumb.appendChild(
      ce('a', { href: `#/session/${sessionId}`, className: 'back-link', textContent: 'Session' })
    );
    breadcrumb.appendChild(ce('span', { className: 'breadcrumb-sep', textContent: ' \u2192 ' }));
    breadcrumb.appendChild(ce('span', { className: 'breadcrumb-current', textContent: 'Subagent' }));
    wrapper.appendChild(breadcrumb);

    wrapper.appendChild(ce('p', { className: 'loading-msg', textContent: 'Loading subagent...' }));
    content.appendChild(wrapper);

    try {
      const resp = await fetch(
        `/api/sessions/${encodeURIComponent(sessionId)}/subagents/${encodeURIComponent(agentId)}`
      );
      if (!resp.ok) throw new Error(`API error: ${resp.status}`);
      const data = await resp.json();

      // Clear loading state but keep breadcrumb
      wrapper.innerHTML = '';

      // Breadcrumb (rebuild)
      const bc2 = ce('div', { className: 'breadcrumb' });
      bc2.appendChild(
        ce('a', { href: `#/session/${sessionId}`, className: 'back-link', textContent: 'Session' })
      );
      bc2.appendChild(ce('span', { className: 'breadcrumb-sep', textContent: ' \u2192 ' }));

      const subLabel =
        data.meta && data.meta.description
          ? `Subagent: ${data.meta.description}`
          : `Subagent: ${agentId}`;
      bc2.appendChild(ce('span', { className: 'breadcrumb-current', textContent: subLabel }));
      wrapper.appendChild(bc2);

      // Subagent meta header
      if (data.meta) {
        const metaHeader = ce('div', { className: 'subagent-meta-header' });
        if (data.meta.agentType) {
          metaHeader.appendChild(
            ce('span', { className: 'subagent-type-badge', textContent: data.meta.agentType })
          );
        }
        if (data.meta.description) {
          metaHeader.appendChild(
            ce('span', { className: 'subagent-description', textContent: data.meta.description })
          );
        }
        wrapper.appendChild(metaHeader);
      }

      // Conversation
      wrapper.appendChild(renderConversation(data.conversation, sessionId));

      // Scroll to top
      window.scrollTo(0, 0);
    } catch (err) {
      console.error('Failed to load subagent:', err);
      wrapper.innerHTML = '';
      const bc3 = ce('div', { className: 'breadcrumb' });
      bc3.appendChild(
        ce('a', { href: `#/session/${sessionId}`, className: 'back-link', textContent: 'Session' })
      );
      wrapper.appendChild(bc3);
      wrapper.appendChild(
        ce('div', {
          className: 'error-state',
          textContent: `Failed to load subagent: ${err.message}`,
        })
      );
    }
  }

  // ---------------------------------------------------------------------------
  // Router
  // ---------------------------------------------------------------------------

  function route() {
    const parsed = hashToFilters();
    if (parsed.__route === 'subagent') {
      renderSubagent(parsed.id, parsed.agentId);
      return;
    }
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
