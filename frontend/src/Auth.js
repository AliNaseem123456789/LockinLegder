// auth.js — how the chatbot learns which company it is working for
// =============================================================================
// The chatbot has no login of its own. The operator is already signed in to
// LockInLedger; this borrows that session.
//
//   1. App loads with no token
//        -> full-page redirect to  demo.lockinledger.com/ai/authorize.php
//           ?return=<this app's URL>
//
//   2. authorize.php reads the PHP session cookie. It can only do that on a
//      TOP-LEVEL navigation - SameSite=Lax blocks the cookie on a cross-site
//      fetch() - which is why this is a redirect and not an API call.
//
//   3. It sends the browser back to  <this app's URL>#token=<handoff JWT>
//      A fragment, not a query string: fragments are never sent to a server
//      and never land in an access log.
//
//   4. This file reads that token, immediately trades it at
//      POST /api/auth/exchange for an 8-hour session token, and scrubs the
//      hash out of the address bar.
//
//   5. Every API call after that carries  Authorization: Bearer <session>.
//
// The app never hardcodes its own URL - window.location.origin is used
// everywhere - so moving it to a different Vercel domain is a PHP-only change.
// =============================================================================

// ---------------------------------------------------------------- config
// Overridable from index.html, same convention as API_BASE_URL:
//     <script>window.__LEDGER_BASE_URL__ = "https://demo.lockinledger.com";</script>
const LEDGER_BASE =
  (typeof window !== 'undefined' && window.__LEDGER_BASE_URL__) ||
  'https://demo.lockinledger.com';

const AUTHORIZE_URL = `${LEDGER_BASE}/ai/authorize.php`;

// Set window.__LEDGER_AUTH__ = false in index.html to turn the whole handshake
// off while developing locally. The backend still answers without a token as
// long as REQUIRE_LEDGER_AUTH=0, so the app runs exactly as it did before.
const AUTH_ENABLED =
  typeof window === 'undefined' || window.__LEDGER_AUTH__ !== false;

const STORE_KEY = 'ledgerassist.session';
const LOOP_KEY = 'ledgerassist.handshake_at';

// ---------------------------------------------------------------- state
let sessionToken = null;
let sessionClaims = null;

// ---------------------------------------------------------------- helpers
// Read a JWT's payload WITHOUT verifying it. Only ever used to show a name
// and to notice an obviously expired token before spending a request on it -
// the server verifies the signature, and nothing here is trusted.
const peek = (token) => {
  try {
    const body = token.split('.')[1];
    return JSON.parse(atob(body.replace(/-/g, '+').replace(/_/g, '/')));
  } catch {
    return null;
  }
};

const secondsLeft = (token) => {
  const c = peek(token);
  return c?.exp ? c.exp - Math.floor(Date.now() / 1000) : -1;
};

// sessionStorage, not memory: a page refresh would otherwise mean a full
// round trip through PHP every time, and this is an internal tool where a
// visible flash on every F5 is the bigger cost. It dies with the tab, and it
// is never readable from another site.
const store = {
  get() {
    try { return sessionStorage.getItem(STORE_KEY); } catch { return null; }
  },
  set(v) {
    try { v ? sessionStorage.setItem(STORE_KEY, v) : sessionStorage.removeItem(STORE_KEY); }
    catch { /* private mode - fall back to memory only */ }
  },
};

// Where authorize.php should send the browser back to. Origin + path, never
// the hash or the query - so it is one stable string that can be whitelisted.
export const returnUrl = () =>
  `${window.location.origin}${window.location.pathname}`;

// ---------------------------------------------------------------- redirect
// A full-page navigation, deliberately. Nothing else carries the PHP cookie.
//
// The timestamp guards against a redirect loop: if authorize.php sends us back
// without a token (misconfigured $ALLOWED_RETURNS is the usual reason) we would
// otherwise bounce forever and never show an error anyone can read.
export function goAuthorize() {
  let last = 0;
  try { last = Number(sessionStorage.getItem(LOOP_KEY) || 0); } catch { /* ignore */ }
  if (Date.now() - last < 10000) {
    throw new Error(
      'Sent to LockInLedger to sign in, but came back without a token.\n\n'
      + 'Usually this means the chatbot address is not in $ALLOWED_RETURNS '
      + `inside ai/authorize.php. It needs to allow:\n${returnUrl()}`);
  }
  try { sessionStorage.setItem(LOOP_KEY, String(Date.now())); } catch { /* ignore */ }
  window.location.replace(
    `${AUTHORIZE_URL}?return=${encodeURIComponent(returnUrl())}`);
}

// ---------------------------------------------------------------- bootstrap
/**
 * Work out who we are, once, at startup.
 *
 * Returns { authenticated, systemId, userId, name } or redirects and never
 * resolves. Throws only when something is genuinely misconfigured, so the app
 * can show the reason instead of a blank screen.
 */
export async function bootstrapAuth(apiBaseUrl) {
  if (!AUTH_ENABLED) {
    return { authenticated: false, name: '', systemId: null, userId: null,
             disabled: true };
  }

  // --- 1. did we just come back from authorize.php? ----------------------
  const hash = window.location.hash || '';
  const match = hash.match(/[#&]token=([^&]+)/);
  if (match) {
    const handoff = decodeURIComponent(match[1]);
    // Out of the address bar before anything else can copy it, and before the
    // user can bookmark a URL with a live token in it.
    window.history.replaceState(null, '', returnUrl());

    const res = await fetch(`${apiBaseUrl}/api/auth/exchange`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${handoff}` },
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(
        `LockInLedger signed you in, but the assistant would not accept the `
        + `token (${res.status}): ${detail.detail || 'unknown reason'}.\n\n`
        + `Most often the two sides are using different secrets - check `
        + `LEDGERASSIST_JWT_SECRET matches ledgerassist_secret.php.`);
    }
    const data = await res.json();
    sessionToken = data.token;
    sessionClaims = peek(sessionToken);
    store.set(sessionToken);
    try { sessionStorage.removeItem(LOOP_KEY); } catch { /* ignore */ }
    return { authenticated: true, systemId: data.system_id,
             userId: data.user_id, name: data.name || '' };
  }

  // --- 2. do we already have a live session token? -----------------------
  const saved = store.get();
  if (saved && secondsLeft(saved) > 60) {
    sessionToken = saved;
    sessionClaims = peek(saved);
    return { authenticated: true,
             systemId: Number(sessionClaims?.sid) || null,
             userId: sessionClaims?.uid || null,
             name: sessionClaims?.name || '' };
  }

  // --- 3. nothing. go and get one. ---------------------------------------
  store.set(null);
  goAuthorize();
  return new Promise(() => {});   // the page is navigating away
}

// ---------------------------------------------------------------- accessors
export const getToken = () => sessionToken;
export const getClaims = () => sessionClaims;
export const isEnabled = () => AUTH_ENABLED;

/** Throw the session away and start the handshake again. */
export function reauthenticate() {
  sessionToken = null;
  sessionClaims = null;
  store.set(null);
  goAuthorize();
}

// ---------------------------------------------------------------- axios
/**
 * Put the token on every request, and handle the one failure that matters.
 *
 * A 401 means the session ran out (or was revoked in LockInLedger). There is
 * nothing to retry and nothing to ask the user - the right move is to go
 * straight back through the handshake, which either returns silently with a
 * fresh token or lands them on the LockInLedger login page because they are
 * no longer signed in. Either outcome is correct.
 */
export function attachAuth(axiosInstance) {
  axiosInstance.interceptors.request.use((config) => {
    if (sessionToken) {
      config.headers = config.headers || {};
      config.headers.Authorization = `Bearer ${sessionToken}`;
    }
    return config;
  });

  axiosInstance.interceptors.response.use(
    (r) => r,
    (error) => {
      if (AUTH_ENABLED && error?.response?.status === 401) {
        reauthenticate();
      }
      return Promise.reject(error);
    });

  return axiosInstance;
}