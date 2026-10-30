/**
 * Live Log Portal - Client Application
 * Handles SSE streaming, log filtering, searching, sound alerts, and log exports.
 */

// Application State
const state = {
  logs: [],
  filterTag: 'ALL',
  searchQuery: '',
  autoScroll: true,
  soundAlerts: false,
  agentOnline: false,
  maxRenderLimit: 3000,
};

// DOM References
const terminalBody = document.getElementById('terminal-body');
const statusPill = document.getElementById('agent-status-pill');
const statusText = document.getElementById('status-text');
const metricTotal = document.getElementById('metric-total');
const metricErrors = document.getElementById('metric-errors');
const metricWarnings = document.getElementById('metric-warnings');
const metricHost = document.getElementById('metric-host');
const searchInput = document.getElementById('search-input');
const clearSearchBtn = document.getElementById('clear-search-btn');
const autoScrollBtn = document.getElementById('auto-scroll-btn');
const soundAlertBtn = document.getElementById('sound-alert-btn');
const clearBtn = document.getElementById('clear-btn');
const downloadBtn = document.getElementById('download-btn');
const copyBtn = document.getElementById('copy-btn');
const setupModal = document.getElementById('setup-modal');
const setupBtn = document.getElementById('setup-btn');
const closeModalBtn = document.getElementById('close-modal-btn');
const toastContainer = document.getElementById('toast-container');
const lineCountBadge = document.getElementById('line-count-badge');

// Web Audio API Synthesizer for Zero-Asset Beep Alerts
let audioCtx = null;
function playAlertBeep(freq = 440, type = 'sine', duration = 0.15) {
  if (!state.soundAlerts) return;
  try {
    if (!audioCtx) {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = type;
    osc.frequency.setValueAtTime(freq, audioCtx.currentTime);
    gain.gain.setValueAtTime(0.1, audioCtx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + duration);
    osc.connect(gain);
    gain.connect(audioCtx.destination);
    osc.start();
    osc.stop(audioCtx.currentTime + duration);
  } catch (e) {
    console.warn('Audio alert blocked:', e);
  }
}

// Show Toast
function showToast(msg, isError = false) {
  const toast = document.createElement('div');
  toast.className = `toast ${isError ? 'toast-error' : ''}`;
  toast.textContent = msg;
  toastContainer.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(10px)';
    setTimeout(() => toast.remove(), 300);
  }, 3200);
}

// Determine Log Category / Tag
function classifyLog(msg) {
  const upper = (msg || '').toUpperCase();
  if (upper.includes('ERROR') || upper.includes('EXCEPTION') || upper.includes('TRACEBACK') || upper.includes('FAILED')) {
    return 'ERROR';
  }
  if (upper.includes('WARN') || upper.includes('REJECTED') || upper.includes('TIMED OUT')) {
    return 'WARN';
  }
  if (upper.includes('TWILIO') || upper.includes('CALL_') || upper.includes('CALL-') || upper.includes('CALLSID') || upper.includes('CA')) {
    return 'TWILIO';
  }
  if (upper.includes('AUDIT') || upper.includes('TKT-') || upper.includes('INC0') || upper.includes('ASSIGNMENT')) {
    return 'AUDIT';
  }
  return 'INFO';
}

// Create DOM Node for a Log Line
function createLogElement(entry, index) {
  const row = document.createElement('div');
  const level = (entry.level || classifyLog(entry.message)).toUpperCase();
  
  let badgeClass = 'badge-info';
  let lineClass = 'level-info';
  if (level.includes('ERR')) {
    badgeClass = 'badge-err';
    lineClass = 'level-err';
  } else if (level.includes('WARN')) {
    badgeClass = 'badge-warn';
    lineClass = 'level-warn';
  } else if (level.includes('TWILIO')) {
    badgeClass = 'badge-twilio';
    lineClass = 'level-twilio';
  } else if (level.includes('AUDIT')) {
    badgeClass = 'badge-audit';
    lineClass = 'level-audit';
  }

  row.className = `log-line ${lineClass}`;
  row.dataset.level = level;
  row.dataset.id = entry.id || index;

  // Format timestamp (HH:MM:SS)
  let timeStr = entry.timestamp || '';
  if (timeStr.includes('T')) {
    timeStr = timeStr.split('T')[1].replace('Z', '').split('.')[0];
  }

  const numSpan = document.createElement('span');
  numSpan.className = 'log-num';
  numSpan.textContent = index + 1;

  const timeSpan = document.createElement('span');
  timeSpan.className = 'log-time';
  timeSpan.textContent = timeStr || '--:--:--';

  const badgeSpan = document.createElement('span');
  badgeSpan.className = `log-badge ${badgeClass}`;
  badgeSpan.textContent = level.length > 6 ? level.slice(0, 6) : level;

  const msgSpan = document.createElement('span');
  msgSpan.className = 'log-msg';

  // Highlight search terms if active
  const rawMsg = entry.message || '';
  if (state.searchQuery && rawMsg.toLowerCase().includes(state.searchQuery.toLowerCase())) {
    row.classList.add('highlight');
    const regex = new RegExp(`(${state.searchQuery.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi');
    msgSpan.innerHTML = escapeHtml(rawMsg).replace(regex, '<mark style="background:#f59e0b;color:#000;border-radius:2px;padding:0 2px">$1</mark>');
  } else {
    msgSpan.textContent = rawMsg;
  }

  row.appendChild(numSpan);
  row.appendChild(timeSpan);
  row.appendChild(badgeSpan);
  row.appendChild(msgSpan);

  return row;
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// Check if Entry Matches Filters
function entryMatchesFilter(entry) {
  const level = (entry.level || classifyLog(entry.message)).toUpperCase();
  if (state.filterTag !== 'ALL') {
    if (state.filterTag === 'ERROR' && !level.includes('ERR')) return false;
    if (state.filterTag === 'WARN' && !level.includes('WARN')) return false;
    if (state.filterTag === 'TWILIO' && !level.includes('TWILIO') && !(entry.message || '').toUpperCase().includes('TWILIO')) return false;
    if (state.filterTag === 'AUDIT' && !level.includes('AUDIT') && !(entry.message || '').toUpperCase().includes('AUDIT')) return false;
  }

  if (state.searchQuery) {
    const q = state.searchQuery.toLowerCase();
    const m = (entry.message || '').toLowerCase();
    const l = level.toLowerCase();
    const h = (entry.host || '').toLowerCase();
    if (!m.includes(q) && !l.includes(q) && !h.includes(q)) {
      return false;
    }
  }

  return true;
}

// Render All Filtered Logs
function renderLogs() {
  terminalBody.innerHTML = '';
  const filtered = state.logs.filter(entryMatchesFilter);

  if (filtered.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'terminal-empty';
    empty.innerHTML = `
      <div class="empty-icon">📡</div>
      <p><b>No matching logs.</b></p>
      <p style="font-size:12px;margin-top:6px;color:#64748b;">
        ${state.logs.length === 0 ? 'Waiting for remote machine start_server.bat logs...' : 'Try adjusting your search or category filters.'}
      </p>
    `;
    terminalBody.appendChild(empty);
    lineCountBadge.textContent = '0 lines';
    return;
  }

  const fragment = document.createDocumentFragment();
  const slice = filtered.slice(-state.maxRenderLimit);
  slice.forEach((entry, idx) => {
    fragment.appendChild(createLogElement(entry, idx));
  });
  terminalBody.appendChild(fragment);

  lineCountBadge.textContent = `${filtered.length} lines`;

  if (state.autoScroll) {
    scrollToBottom();
  }
}

// Append a Single Incoming Log
function appendLog(entry) {
  state.logs.push(entry);
  if (state.logs.length > 5000) {
    state.logs.shift();
  }

  const level = (entry.level || classifyLog(entry.message)).toUpperCase();
  if (level.includes('ERR')) {
    playAlertBeep(320, 'sawtooth', 0.25);
  }

  if (entryMatchesFilter(entry)) {
    // If empty screen currently showing, clear it first
    if (terminalBody.querySelector('.terminal-empty')) {
      terminalBody.innerHTML = '';
    }

    const el = createLogElement(entry, state.logs.length - 1);
    terminalBody.appendChild(el);
    lineCountBadge.textContent = `${terminalBody.children.length} lines`;

    if (state.autoScroll) {
      scrollToBottom();
    }
  }
}

function scrollToBottom() {
  terminalBody.scrollTop = terminalBody.scrollHeight;
}

// Detect User Scroll to Pause Auto-Scroll
terminalBody.addEventListener('scroll', () => {
  const atBottom = terminalBody.scrollHeight - terminalBody.scrollTop <= terminalBody.clientHeight + 40;
  if (!atBottom && state.autoScroll) {
    state.autoScroll = false;
    autoScrollBtn.classList.remove('active');
    autoScrollBtn.title = 'Auto-scroll is paused (click to resume)';
  } else if (atBottom && !state.autoScroll) {
    state.autoScroll = true;
    autoScrollBtn.classList.add('active');
    autoScrollBtn.title = 'Auto-scroll is active';
  }
});

// Periodic Status Polling & Metrics
async function updateStatus() {
  try {
    const res = await fetch('/api/status');
    if (res.ok) {
      const data = await res.json();
      const isOnline = data.status === 'online';
      state.agentOnline = isOnline;

      if (isOnline) {
        statusPill.className = 'agent-status-pill online';
        statusText.textContent = `REMOTE CONNECTED (${data.hostname || data.ip})`;
        metricHost.textContent = `${data.hostname} [${data.ip}]`;
      } else {
        statusPill.className = 'agent-status-pill';
        statusText.textContent = 'WAITING FOR REMOTE AGENT';
        metricHost.textContent = data.hostname !== 'Unknown' ? `${data.hostname} (Idle)` : 'No agent active';
      }

      metricTotal.textContent = (data.total_received || state.logs.length).toLocaleString();
      metricErrors.textContent = (data.error_count || 0).toLocaleString();
      metricWarnings.textContent = (data.warning_count || 0).toLocaleString();

      // Populate local IPs in setup guide if available
      if (data.local_ips && data.local_ips.length > 0) {
        const ipDisplay = document.getElementById('detected-local-ip');
        if (ipDisplay) ipDisplay.textContent = data.local_ips[0];
      }
    }
  } catch (err) {
    statusPill.className = 'agent-status-pill';
    statusText.textContent = 'PORTAL RECONNECTING...';
  }
}

// Connect to SSE Stream
function initSSE() {
  const eventSource = new EventSource('/api/stream');

  eventSource.addEventListener('batch', (e) => {
    try {
      const batch = JSON.parse(e.data);
      if (Array.isArray(batch)) {
        state.logs = batch;
        renderLogs();
      }
    } catch (err) {
      console.error('Error parsing batch:', err);
    }
  });

  eventSource.addEventListener('clear', () => {
    state.logs = [];
    renderLogs();
    showToast('Logs cleared by server');
  });

  eventSource.onmessage = (e) => {
    try {
      const entry = JSON.parse(e.data);
      appendLog(entry);
    } catch (err) {
      // Keepalive or parse error
    }
  };

  eventSource.onerror = () => {
    statusPill.className = 'agent-status-pill';
    statusText.textContent = 'STREAM CONNECTING...';
  };
}

// Filter Tag Buttons
document.querySelectorAll('.tag-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tag-btn').forEach((b) => b.classList.remove('active'));
    btn.classList.add('active');
    state.filterTag = btn.dataset.tag;
    renderLogs();
  });
});

// Search Input Handlers
let searchDebounceTimer = null;
searchInput.addEventListener('input', (e) => {
  state.searchQuery = e.target.value.trim();
  clearSearchBtn.style.display = state.searchQuery ? 'block' : 'none';
  clearTimeout(searchDebounceTimer);
  searchDebounceTimer = setTimeout(() => {
    renderLogs();
  }, 120);
});

clearSearchBtn.addEventListener('click', () => {
  searchInput.value = '';
  state.searchQuery = '';
  clearSearchBtn.style.display = 'none';
  renderLogs();
});

// Toolbar Buttons
autoScrollBtn.addEventListener('click', () => {
  state.autoScroll = !state.autoScroll;
  autoScrollBtn.classList.toggle('active', state.autoScroll);
  if (state.autoScroll) {
    scrollToBottom();
  }
});

soundAlertBtn.addEventListener('click', () => {
  state.soundAlerts = !state.soundAlerts;
  soundAlertBtn.classList.toggle('active', state.soundAlerts);
  showToast(state.soundAlerts ? '🔔 Error sound alerts enabled' : '🔕 Sound alerts muted');
  if (state.soundAlerts) {
    playAlertBeep(520, 'sine', 0.1);
  }
});

clearBtn.addEventListener('click', async () => {
  if (confirm('Clear current log window?')) {
    try {
      await fetch('/api/clear', { method: 'POST' });
    } catch (e) {
      state.logs = [];
      renderLogs();
    }
  }
});

downloadBtn.addEventListener('click', () => {
  const filtered = state.logs.filter(entryMatchesFilter);
  const textContent = filtered.map((l, i) => `[${l.timestamp || ''}] [${(l.level || 'INFO')}] ${l.message || ''}`).join('\n');
  const blob = new Blob([textContent], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `flm_remote_logs_${new Date().toISOString().replace(/[:.]/g, '-')}.txt`;
  a.click();
  URL.revokeObjectURL(url);
  showToast(`Exported ${filtered.length} log lines`);
});

copyBtn.addEventListener('click', () => {
  const filtered = state.logs.filter(entryMatchesFilter);
  const textContent = filtered.map((l) => `[${l.timestamp || ''}] [${(l.level || 'INFO')}] ${l.message || ''}`).join('\n');
  navigator.clipboard.writeText(textContent).then(() => {
    showToast(`Copied ${filtered.length} log lines to clipboard`);
  }).catch(() => {
    showToast('Failed to copy', true);
  });
});

// Setup Modal
setupBtn.addEventListener('click', () => setupModal.classList.add('open'));
closeModalBtn.addEventListener('click', () => setupModal.classList.remove('open'));
setupModal.addEventListener('click', (e) => {
  if (e.target === setupModal) setupModal.classList.remove('open');
});

// Copy snippet buttons inside modal
document.querySelectorAll('.copy-mini-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    const targetId = btn.dataset.target;
    const targetEl = document.getElementById(targetId);
    if (targetEl) {
      navigator.clipboard.writeText(targetEl.textContent.trim()).then(() => {
        const orig = btn.textContent;
        btn.textContent = 'Copied!';
        setTimeout(() => btn.textContent = orig, 1500);
      });
    }
  });
});

// Initialize
initSSE();
updateStatus();
setInterval(updateStatus, 3000);

// --- Navigation Tab Switching (Home vs Live Logs) ---
const viewHome = document.getElementById('view-home');
const viewLogs = document.getElementById('view-logs');
const navHomeBtn = document.getElementById('nav-home-btn');
const navLogsBtn = document.getElementById('nav-logs-btn');
const heroGotoLogsBtn = document.getElementById('hero-goto-logs-btn');
const heroSetupBtn = document.getElementById('hero-setup-btn');
const cardLogsLink = document.getElementById('card-logs-link');
const cardSetupLink = document.getElementById('card-setup-link');

const homeServerStatus = document.getElementById('home-server-status');
const homeServerHost = document.getElementById('home-server-host');
const homeTotalEvents = document.getElementById('home-total-events');

function switchView(viewName) {
  if (viewName === 'logs') {
    if (viewHome) viewHome.style.display = 'none';
    if (viewLogs) viewLogs.style.display = 'block';
    if (navHomeBtn) navHomeBtn.classList.remove('active');
    if (navLogsBtn) navLogsBtn.classList.add('active');
    renderLogs();
    if (state.autoScroll) scrollToBottom();
  } else {
    if (viewHome) viewHome.style.display = 'flex';
    if (viewLogs) viewLogs.style.display = 'none';
    if (navHomeBtn) navHomeBtn.classList.add('active');
    if (navLogsBtn) navLogsBtn.classList.remove('active');
  }
}

if (navHomeBtn) navHomeBtn.addEventListener('click', () => switchView('home'));
if (navLogsBtn) navLogsBtn.addEventListener('click', () => switchView('logs'));
if (heroGotoLogsBtn) heroGotoLogsBtn.addEventListener('click', () => switchView('logs'));
if (cardLogsLink) cardLogsLink.addEventListener('click', () => switchView('logs'));

if (heroSetupBtn) heroSetupBtn.addEventListener('click', () => setupModal.classList.add('open'));
if (cardSetupLink) cardSetupLink.addEventListener('click', () => setupModal.classList.add('open'));

// Sync home indicators with status polling
const origUpdateStatus = updateStatus;
updateStatus = async function() {
  await origUpdateStatus();
  if (homeServerStatus) {
    if (state.agentOnline) {
      homeServerStatus.innerHTML = '<span class="status-dot"></span> <span style="color:var(--accent-emerald);">ONLINE (Active)</span>';
    } else {
      homeServerStatus.innerHTML = '<span class="status-dot" style="background:#94a3b8;box-shadow:none;"></span> <span style="color:#94a3b8;">OFFLINE (Waiting)</span>';
    }
  }
  if (homeServerHost && metricHost) {
    homeServerHost.textContent = metricHost.textContent;
  }
  if (homeTotalEvents) {
    homeTotalEvents.textContent = state.logs.length;
  }
};
