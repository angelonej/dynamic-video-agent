/**
 * app.js — Dynamic Video AI Agent Platform Frontend
 * ==================================================
 * Pure vanilla JS (ES2022+), no build step required.
 * Works with the plain HTML + Tailwind CDN setup.
 *
 * Features:
 *   • Create agents (POST /create-agent)
 *   • Switch between agents (sidebar)
 *   • Send text messages (POST /chat)
 *   • Push-to-talk via Web Speech API
 *   • Display agent text + video avatar + audio fallback
 *   • Session persistence (localStorage)
 *   • Loading states for each pipeline stage
 */

'use strict';

// ─────────────────────────────────────────────────────────────────────────────
// Configuration
// ─────────────────────────────────────────────────────────────────────────────

/**
 * API base URL — set this to your SAM local or deployed API Gateway URL.
 * Options (in priority order):
 *   1. window.API_BASE_URL  (set in a <script> block before app.js)
 *   2. ?api= query param   (http://localhost:5500?api=http://localhost:3001)
 *   3. Default localhost
 */
const API_BASE_URL = (() => {
  if (window.API_BASE_URL) return window.API_BASE_URL.replace(/\/$/, '');
  const params = new URLSearchParams(window.location.search);
  if (params.get('api')) return params.get('api').replace(/\/$/, '');
  return 'http://localhost:3001';
})();

console.log('[VideoAgents] API base:', API_BASE_URL);

// ─────────────────────────────────────────────────────────────────────────────
// State
// ─────────────────────────────────────────────────────────────────────────────

const state = {
  /** @type {Array<{agent_id: string, name: string, voice_id: string, avatar_id: string, created_at: number, emoji: string}>} */
  agents: [],

  /** Currently selected agent_id */
  activeAgentId: null,

  /** Current session_id (one per agent) */
  sessions: {},   // { [agent_id]: session_id }

  /** Is a chat request in-flight? */
  loading: false,

  /** Is the microphone currently recording? */
  recording: false,

  /** Web Speech Recognition instance */
  recognition: null,
};

// Persist agents list to localStorage for page refreshes
const STORAGE_KEY = 'video_agents_poc_agents';

// ─────────────────────────────────────────────────────────────────────────────
// DOM refs
// ─────────────────────────────────────────────────────────────────────────────

const $ = (id) => document.getElementById(id);

const dom = {
  agentList:          $('agent-list'),
  agentListEmpty:     $('agent-list-empty'),
  btnOpenCreate:      $('btn-open-create'),
  btnCloseModal:      $('btn-close-modal'),
  btnCancelCreate:    $('btn-cancel-create'),
  btnSubmitCreate:    $('btn-submit-create'),
  btnSubmitText:      $('btn-submit-text'),
  btnSubmitSpinner:   $('btn-submit-spinner'),
  modalCreateAgent:   $('modal-create-agent'),
  formCreateAgent:    $('form-create-agent'),
  agentNameInput:     $('agent-name-input'),
  agentPromptInput:   $('agent-prompt-input'),
  agentVoiceInput:    $('agent-voice-input'),
  agentAvatarInput:   $('agent-avatar-input'),
  createAgentError:   $('create-agent-error'),
  messageList:        $('message-list'),
  welcomeScreen:      $('welcome-screen'),
  chatInput:          $('chat-input'),
  btnSend:            $('btn-send'),
  btnMic:             $('btn-mic'),
  micIcon:            $('mic-icon'),
  micStatus:          $('mic-status'),
  loadingBar:         $('loading-bar'),
  loadingText:        $('loading-text'),
  toggleVideo:        $('toggle-video'),
  btnNewSession:      $('btn-new-session'),
  chatAgentName:      $('chat-agent-name'),
  chatAgentMeta:      $('chat-agent-meta'),
  agentAvatarBadge:   $('agent-avatar-badge'),
  statusDot:          $('status-dot'),
  statusText:         $('status-text'),
  apiBaseDisplay:     $('api-base-display'),
};

// ─────────────────────────────────────────────────────────────────────────────
// Emoji palette for agent cards
// ─────────────────────────────────────────────────────────────────────────────

const EMOJI_PALETTE = ['🤖','🧠','🎭','🏛️','💸','🔬','📚','🌍','⚗️','🗿','🎓','🔭','🏺','⚔️','🌌'];

function pickEmoji(name) {
  // Deterministic emoji from name hash
  let h = 0;
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) & 0xffff;
  return EMOJI_PALETTE[h % EMOJI_PALETTE.length];
}

// ─────────────────────────────────────────────────────────────────────────────
// LocalStorage helpers
// ─────────────────────────────────────────────────────────────────────────────

function saveAgents() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state.agents));
  } catch (e) {
    console.warn('[VideoAgents] Could not save agents to localStorage:', e);
  }
}

function loadAgents() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) state.agents = JSON.parse(raw);
  } catch (e) {
    state.agents = [];
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// API helpers
// ─────────────────────────────────────────────────────────────────────────────

async function apiPost(path, body) {
  const resp = await fetch(`${API_BASE_URL}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await resp.json();
  if (!resp.ok) {
    const msg = data?.error || `HTTP ${resp.status}`;
    throw new Error(msg);
  }
  return data;
}

async function apiGet(path) {
  const resp = await fetch(`${API_BASE_URL}${path}`);
  const data = await resp.json();
  if (!resp.ok) throw new Error(data?.error || `HTTP ${resp.status}`);
  return data;
}

// ─────────────────────────────────────────────────────────────────────────────
// Health check + connection status
// ─────────────────────────────────────────────────────────────────────────────

async function checkHealth() {
  dom.apiBaseDisplay.textContent = API_BASE_URL;
  try {
    const data = await apiGet('/health');
    dom.statusDot.className = 'w-2 h-2 rounded-full bg-green-400';
    dom.statusText.textContent = `Connected · ${data.agents_loaded ?? 0} agents`;
  } catch {
    dom.statusDot.className = 'w-2 h-2 rounded-full bg-red-400 animate-pulse';
    dom.statusText.textContent = 'API unreachable';
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Loading state management
// ─────────────────────────────────────────────────────────────────────────────

const LOADING_STAGES = {
  thinking:   '🧠 Thinking…',
  tts:        '🔊 Generating speech…',
  video:      '🎬 Rendering your video…',
  sending:    '📤 Sending…',
};

function setLoading(stage) {
  if (stage) {
    state.loading = true;
    dom.loadingBar.classList.remove('hidden');
    dom.loadingText.textContent = LOADING_STAGES[stage] ?? stage;
    dom.btnSend.disabled = true;
    dom.chatInput.disabled = true;
    dom.btnMic.disabled = true;
  } else {
    state.loading = false;
    dom.loadingBar.classList.add('hidden');
    dom.loadingText.textContent = '';
    dom.btnSend.disabled = !state.activeAgentId;
    dom.chatInput.disabled = !state.activeAgentId;
    dom.btnMic.disabled = !state.activeAgentId || !state.recognition;
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Sidebar: render agent list
// ─────────────────────────────────────────────────────────────────────────────

function renderAgentList() {
  // Clear existing cards (keep empty-state element)
  const cards = dom.agentList.querySelectorAll('.agent-card-wrapper');
  cards.forEach(c => c.remove());

  if (state.agents.length === 0) {
    dom.agentListEmpty.classList.remove('hidden');
    return;
  }

  dom.agentListEmpty.classList.add('hidden');

  const tpl = document.getElementById('tpl-agent-card');
  for (const agent of [...state.agents].reverse()) {
    const wrapper = document.createElement('div');
    wrapper.className = 'agent-card-wrapper';
    wrapper.dataset.agentId = agent.agent_id;

    const clone = tpl.content.cloneNode(true);
    const btn = clone.querySelector('.agent-card');
    btn.dataset.agentId = agent.agent_id;

    clone.querySelector('.agent-emoji').textContent = agent.emoji ?? '🤖';
    clone.querySelector('.agent-name').textContent = agent.name;
    clone.querySelector('.agent-created').textContent = formatRelTime(agent.created_at);

    // Active state
    if (agent.agent_id === state.activeAgentId) {
      btn.classList.add('bg-surface-700', 'border-brand-500/50', '!border-brand-500');
    }

    btn.addEventListener('click', () => selectAgent(agent.agent_id));

    wrapper.appendChild(clone);
    dom.agentList.appendChild(wrapper);
  }
}

function formatRelTime(ts) {
  const diff = Date.now() / 1000 - ts;
  if (diff < 60)     return 'Just now';
  if (diff < 3600)   return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400)  return `${Math.floor(diff / 3600)}h ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

// ─────────────────────────────────────────────────────────────────────────────
// Select active agent
// ─────────────────────────────────────────────────────────────────────────────

function selectAgent(agentId) {
  const agent = state.agents.find(a => a.agent_id === agentId);
  if (!agent) return;

  state.activeAgentId = agentId;

  // Update header
  dom.chatAgentName.textContent = agent.name;
  dom.chatAgentMeta.textContent = `Agent ready · Session ${getSessionId(agentId).slice(0, 8)}…`;
  dom.agentAvatarBadge.textContent = agent.emoji ?? '🤖';

  // Enable inputs
  dom.chatInput.disabled = false;
  dom.btnSend.disabled = false;
  if (state.recognition) dom.btnMic.disabled = false;

  // Hide welcome, show chat area
  dom.welcomeScreen.classList.add('hidden');

  // Re-render sidebar to update active state
  renderAgentList();

  // Focus input
  dom.chatInput.focus();
}

function getSessionId(agentId) {
  if (!state.sessions[agentId]) {
    state.sessions[agentId] = crypto.randomUUID();
  }
  return state.sessions[agentId];
}

// ─────────────────────────────────────────────────────────────────────────────
// Create agent (modal form)
// ─────────────────────────────────────────────────────────────────────────────

function openCreateModal() {
  dom.modalCreateAgent.classList.remove('hidden');
  dom.agentNameInput.focus();
}

function closeCreateModal() {
  dom.modalCreateAgent.classList.add('hidden');
  dom.createAgentError.classList.add('hidden');
  dom.createAgentError.textContent = '';
}

function setCreateLoading(loading) {
  dom.btnSubmitCreate.disabled = loading;
  dom.btnSubmitText.textContent = loading ? 'Creating…' : 'Create Agent';
  dom.btnSubmitSpinner.classList.toggle('hidden', !loading);
}

dom.formCreateAgent.addEventListener('submit', async (e) => {
  e.preventDefault();

  const name = dom.agentNameInput.value.trim();
  const systemPrompt = dom.agentPromptInput.value.trim();
  const voiceId = dom.agentVoiceInput.value.trim() || undefined;
  const avatarId = dom.agentAvatarInput.value.trim() || undefined;

  // Client-side validation
  if (!name) {
    showCreateError('Agent name is required.');
    dom.agentNameInput.focus();
    return;
  }
  if (!systemPrompt || systemPrompt.length < 10) {
    showCreateError('System prompt must be at least 10 characters.');
    dom.agentPromptInput.focus();
    return;
  }

  setCreateLoading(true);
  hideCreateError();

  try {
    const result = await apiPost('/create-agent', {
      name,
      system_prompt: systemPrompt,
      voice_id: voiceId,
      avatar_id: avatarId,
    });

    // Add emoji + store locally
    const agent = {
      ...result,
      emoji: pickEmoji(name),
    };
    state.agents.push(agent);
    saveAgents();

    // Reset form
    dom.agentNameInput.value = '';
    dom.agentPromptInput.value = '';
    dom.agentVoiceInput.value = '';
    dom.agentAvatarInput.value = '';

    closeCreateModal();
    renderAgentList();
    selectAgent(agent.agent_id);

    // Update health status counter
    checkHealth();

  } catch (err) {
    showCreateError(err.message || 'Failed to create agent. Is the API running?');
  } finally {
    setCreateLoading(false);
  }
});

function showCreateError(msg) {
  dom.createAgentError.textContent = msg;
  dom.createAgentError.classList.remove('hidden');
}
function hideCreateError() {
  dom.createAgentError.classList.add('hidden');
  dom.createAgentError.textContent = '';
}

// Quick prompt buttons
document.querySelectorAll('.quick-prompt').forEach(btn => {
  btn.addEventListener('click', () => {
    dom.agentNameInput.value = btn.dataset.name;
    dom.agentPromptInput.value = btn.dataset.prompt;
    dom.agentNameInput.focus();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Chat: send message
// ─────────────────────────────────────────────────────────────────────────────

async function sendMessage(text) {
  text = text.trim();
  if (!text || state.loading || !state.activeAgentId) return;

  const agentId = state.activeAgentId;
  const sessionId = getSessionId(agentId);
  const generateVideo = dom.toggleVideo.checked;

  // Append user bubble
  appendUserMessage(text);

  // Clear input
  dom.chatInput.value = '';
  autoResizeTextarea(dom.chatInput);

  // Stage 1: thinking
  setLoading('thinking');

  try {
    // If video is on, show pipeline stages
    if (generateVideo) {
      // We update the loading text asynchronously based on timing expectations
      const videoStageTimer = setTimeout(() => setLoading('tts'), 4000);
      const renderStageTimer = setTimeout(() => setLoading('video'), 9000);

      let result;
      try {
        result = await apiPost('/chat', {
          agent_id: agentId,
          session_id: sessionId,
          message: text,
          generate_video: generateVideo,
        });
      } finally {
        clearTimeout(videoStageTimer);
        clearTimeout(renderStageTimer);
      }

      appendAgentMessage(result);
    } else {
      const result = await apiPost('/chat', {
        agent_id: agentId,
        session_id: sessionId,
        message: text,
        generate_video: false,
      });
      appendAgentMessage(result);
    }

  } catch (err) {
    appendErrorMessage(err.message || 'Request failed. Check that the API is running.');
  } finally {
    setLoading(null);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Message rendering
// ─────────────────────────────────────────────────────────────────────────────

function now() {
  return new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function appendUserMessage(text) {
  const tpl = document.getElementById('tpl-message-user');
  const clone = tpl.content.cloneNode(true);
  const bubble = clone.querySelector('div > div');
  const timeEl = clone.querySelector('span');

  bubble.querySelector('div').textContent = text;
  timeEl.textContent = now();

  dom.messageList.appendChild(clone);
  scrollToBottom();
}

function appendAgentMessage(data) {
  const agent = state.agents.find(a => a.agent_id === state.activeAgentId);
  const tpl = document.getElementById('tpl-message-agent');
  const clone = tpl.content.cloneNode(true);

  // Set emoji
  clone.querySelector('.w-8.h-8').textContent = agent?.emoji ?? '🤖';

  // Text content
  clone.querySelector('.bg-surface-700').textContent = data.reply || '(empty reply)';

  // Video
  if (data.video_url) {
    const videoContainer = clone.querySelector('.video-container');
    const video = videoContainer.querySelector('video');
    video.querySelector('source').src = data.video_url;
    videoContainer.classList.remove('hidden');
    // Force load
    video.load();
    video.play().catch(() => {});  // autoplay may be blocked — user click will still work
  }

  // Audio fallback
  if (!data.video_url && data.audio_url) {
    const audioContainer = clone.querySelector('.audio-container');
    const audio = audioContainer.querySelector('audio');
    audio.querySelector('source').src = data.audio_url;
    audioContainer.classList.remove('hidden');
    audio.load();
  }

  // Timestamp + token info
  const usageStr = data.usage?.total_tokens
    ? ` · ${data.usage.total_tokens} tokens`
    : '';
  clone.querySelector('span').textContent = now() + usageStr;

  dom.messageList.appendChild(clone);
  scrollToBottom();
}

function appendErrorMessage(msg) {
  const div = document.createElement('div');
  div.className = 'flex justify-center';
  div.innerHTML = `
    <div class="px-4 py-2 bg-red-900/30 border border-red-800/50 rounded-xl text-xs text-red-300 max-w-sm text-center">
      ⚠️ ${escapeHtml(msg)}
    </div>`;
  dom.messageList.appendChild(div);
  scrollToBottom();
}

function escapeHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function scrollToBottom() {
  requestAnimationFrame(() => {
    dom.messageList.scrollTop = dom.messageList.scrollHeight;
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Text input: auto-resize + Enter to send
// ─────────────────────────────────────────────────────────────────────────────

function autoResizeTextarea(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 160) + 'px';
}

dom.chatInput.addEventListener('input', () => autoResizeTextarea(dom.chatInput));

dom.chatInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage(dom.chatInput.value);
  }
});

dom.btnSend.addEventListener('click', () => sendMessage(dom.chatInput.value));

// ─────────────────────────────────────────────────────────────────────────────
// Push-to-talk (Web Speech API)
// ─────────────────────────────────────────────────────────────────────────────

function initSpeechRecognition() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    console.warn('[VideoAgents] Web Speech API not supported in this browser.');
    dom.btnMic.title = 'Speech recognition not supported in this browser';
    return null;
  }

  const recognition = new SpeechRecognition();
  recognition.continuous = false;
  recognition.interimResults = true;
  recognition.lang = 'en-US';

  let finalTranscript = '';
  let interimTranscript = '';

  recognition.onstart = () => {
    state.recording = true;
    finalTranscript = '';
    interimTranscript = '';
    dom.btnMic.classList.add('bg-red-500', 'border-red-400', 'shadow-glow-red');
    dom.btnMic.classList.remove('bg-surface-600', 'border-surface-500');
    dom.micStatus.classList.remove('hidden');
    // Show mic icon as "active"
    dom.micIcon.innerHTML = `
      <circle cx="12" cy="1" r="3" fill="currentColor"/>
      <path stroke-linecap="round" stroke-linejoin="round"
            d="M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3z"/>
      <path stroke-linecap="round" stroke-linejoin="round"
            d="M19 10v2a7 7 0 01-14 0v-2M12 19v4M8 23h8"/>`;
  };

  recognition.onresult = (event) => {
    interimTranscript = '';
    finalTranscript = '';
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const t = event.results[i][0].transcript;
      if (event.results[i].isFinal) finalTranscript += t;
      else interimTranscript += t;
    }
    dom.chatInput.value = finalTranscript || interimTranscript;
    autoResizeTextarea(dom.chatInput);
  };

  recognition.onend = () => {
    state.recording = false;
    dom.btnMic.classList.remove('bg-red-500', 'border-red-400', 'shadow-glow-red');
    dom.btnMic.classList.add('bg-surface-600', 'border-surface-500');
    dom.micStatus.classList.add('hidden');
    // Restore mic icon
    dom.micIcon.innerHTML = `
      <path stroke-linecap="round" stroke-linejoin="round"
            d="M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3z"/>
      <path stroke-linecap="round" stroke-linejoin="round"
            d="M19 10v2a7 7 0 01-14 0v-2M12 19v4M8 23h8"/>`;
    // Auto-send if we captured something
    if (finalTranscript.trim()) {
      sendMessage(finalTranscript.trim());
    }
  };

  recognition.onerror = (event) => {
    console.error('[VideoAgents] Speech recognition error:', event.error);
    if (event.error === 'not-allowed') {
      appendErrorMessage('Microphone access denied. Please allow microphone permission in your browser.');
    }
    state.recording = false;
    dom.micStatus.classList.add('hidden');
  };

  return recognition;
}

// Mic button: click to start, click again to stop (toggle)
dom.btnMic.addEventListener('click', () => {
  if (!state.recognition || state.loading) return;

  if (state.recording) {
    state.recognition.stop();
  } else {
    try {
      state.recognition.start();
    } catch (e) {
      console.error('[VideoAgents] Could not start recognition:', e);
    }
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Session reset
// ─────────────────────────────────────────────────────────────────────────────

dom.btnNewSession.addEventListener('click', () => {
  if (!state.activeAgentId) return;

  if (confirm('Start a new session? This clears the chat history for this agent.')) {
    delete state.sessions[state.activeAgentId];

    // Clear message list (keep welcome screen hidden)
    const rows = dom.messageList.querySelectorAll('.message-row, .flex.justify-center');
    rows.forEach(r => r.remove());

    // Update header meta
    const agent = state.agents.find(a => a.agent_id === state.activeAgentId);
    if (agent) {
      dom.chatAgentMeta.textContent = `Agent ready · Session ${getSessionId(state.activeAgentId).slice(0, 8)}…`;
    }
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Modal open/close events
// ─────────────────────────────────────────────────────────────────────────────

dom.btnOpenCreate.addEventListener('click', openCreateModal);
dom.btnCloseModal.addEventListener('click', closeCreateModal);
dom.btnCancelCreate.addEventListener('click', closeCreateModal);

// Close modal on backdrop click
dom.modalCreateAgent.addEventListener('click', (e) => {
  if (e.target === dom.modalCreateAgent) closeCreateModal();
});

// Close modal on Escape key
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !dom.modalCreateAgent.classList.contains('hidden')) {
    closeCreateModal();
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Boot sequence
// ─────────────────────────────────────────────────────────────────────────────

function boot() {
  // Load persisted agents
  loadAgents();
  renderAgentList();

  // Initialise speech recognition
  state.recognition = initSpeechRecognition();
  if (!state.recognition) {
    dom.btnMic.title = 'Speech API unavailable (use Chrome/Edge)';
  }

  // Check API health
  checkHealth();

  // Periodic health poll every 30 s
  setInterval(checkHealth, 30_000);

  // If agents exist, select the most recent one
  if (state.agents.length > 0) {
    const latest = [...state.agents].sort((a, b) => b.created_at - a.created_at)[0];
    selectAgent(latest.agent_id);
  }

  console.log('[VideoAgents] App booted. API:', API_BASE_URL);
}

boot();
