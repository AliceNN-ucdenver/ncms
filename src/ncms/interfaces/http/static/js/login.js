// ── NCMS Dashboard — Login ───────────────────────────────────────────
// JWT-based auth. Token stored in sessionStorage.

const _AUTH_TOKEN_KEY = 'ncms_token';
const _AUTH_USER_KEY = 'ncms_user';

function getAuthToken() {
  return sessionStorage.getItem(_AUTH_TOKEN_KEY);
}

function getAuthUser() {
  try {
    return JSON.parse(sessionStorage.getItem(_AUTH_USER_KEY) || 'null');
  } catch { return null; }
}

function setAuth(token, user) {
  sessionStorage.setItem(_AUTH_TOKEN_KEY, token);
  sessionStorage.setItem(_AUTH_USER_KEY, JSON.stringify(user));
}

function clearAuth() {
  sessionStorage.removeItem(_AUTH_TOKEN_KEY);
  sessionStorage.removeItem(_AUTH_USER_KEY);
}

// ── Patched fetch — auto-injects Authorization header ───────────────

const _originalFetch = window.fetch;
window.fetch = function(url, options = {}) {
  const token = getAuthToken();
  if (token) {
    options.headers = options.headers || {};
    if (options.headers instanceof Headers) {
      if (!options.headers.has('Authorization')) {
        options.headers.set('Authorization', 'Bearer ' + token);
      }
    } else {
      if (!options.headers['Authorization']) {
        options.headers['Authorization'] = 'Bearer ' + token;
      }
    }
  }
  return _originalFetch(url, options).then(resp => {
    // Auto-redirect to login on 401
    if (resp.status === 401 && !String(url).includes('/auth/')) {
      clearAuth();
      showLoginOverlay();
    }
    return resp;
  });
};

// ── Login Overlay ───────────────────────────────────────────────────

function showLoginOverlay() {
  // Hide main content
  const main = document.getElementById('main-layout');
  if (main) main.style.display = 'none';

  // Remove existing overlay
  const existing = document.getElementById('login-overlay');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.id = 'login-overlay';
  overlay.className = 'login-overlay';
  overlay.innerHTML = `
    <div class="login-card">
      <div class="login-header">
        <img src="/img/ncms-logo.png" alt="NCMS" class="login-logo-img">
        <span class="login-subtitle">Cognitive Memory System</span>
      </div>
      <form class="login-form" onsubmit="handleLogin(event)">
        <input type="text" id="login-username" class="login-input"
               placeholder="Username" autocomplete="username" autofocus>
        <input type="password" id="login-password" class="login-input"
               placeholder="Password" autocomplete="current-password">
        <button type="submit" class="login-btn" id="login-btn">Sign In</button>
        <div class="login-error" id="login-error"></div>
      </form>
    </div>
  `;
  document.body.appendChild(overlay);
}

function hideLoginOverlay() {
  const overlay = document.getElementById('login-overlay');
  if (overlay) overlay.remove();
  const main = document.getElementById('main-layout');
  if (main) main.style.display = '';
}

async function handleLogin(event) {
  event.preventDefault();
  const username = document.getElementById('login-username').value.trim();
  const password = document.getElementById('login-password').value;
  const errorEl = document.getElementById('login-error');
  const btnEl = document.getElementById('login-btn');

  if (!username || !password) {
    errorEl.textContent = 'Enter username and password';
    return;
  }

  btnEl.disabled = true;
  btnEl.textContent = 'Signing in...';
  errorEl.textContent = '';

  try {
    const resp = await _originalFetch(HUB_API + '/api/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });

    if (!resp.ok) {
      const data = await resp.json();
      errorEl.textContent = data.error || 'Login failed';
      btnEl.disabled = false;
      btnEl.textContent = 'Sign In';
      return;
    }

    const data = await resp.json();
    setAuth(data.token, data.user);
    hideLoginOverlay();
    updateUserBadge();

    // Reload data now that we're authenticated
    if (typeof loadProjects === 'function') loadProjects();
  } catch (e) {
    errorEl.textContent = 'Connection failed';
    btnEl.disabled = false;
    btnEl.textContent = 'Sign In';
  }
}

function logout() {
  clearAuth();
  showLoginOverlay();
}

function updateUserBadge() {
  let user = getAuthUser();
  // Fallback: decode name from JWT if user object is missing
  if (!user && getAuthToken()) {
    try {
      const payload = JSON.parse(atob(getAuthToken().split('.')[1]));
      user = { username: payload.sub || 'user', display_name: payload.name || payload.sub };
      sessionStorage.setItem(_AUTH_USER_KEY, JSON.stringify(user));
    } catch { /* ignore */ }
  }
  const nameEl = document.getElementById('user-display-name');
  if (nameEl) {
    nameEl.textContent = user ? (user.display_name || user.username) : '';
  }
}

function toggleUserMenu() {
  const menu = document.getElementById('user-menu');
  if (menu) {
    menu.style.display = menu.style.display === 'none' ? 'block' : 'none';
  }
}

function updateSealBadge(count) {
  const badge = document.getElementById('seal-badge');
  if (badge) {
    if (count > 0) {
      badge.textContent = count;
      badge.style.display = 'flex';
    } else {
      badge.style.display = 'none';
    }
  }
}

// Close user menu on outside click
document.addEventListener('click', (e) => {
  const menu = document.getElementById('user-menu');
  const info = document.getElementById('user-info');
  if (menu && menu.style.display !== 'none'
      && !menu.contains(e.target) && !info?.contains(e.target)) {
    menu.style.display = 'none';
  }
});

// ── Init — check auth on page load ──────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  if (!getAuthToken()) {
    showLoginOverlay();
  } else {
    updateUserBadge();
  }
});
