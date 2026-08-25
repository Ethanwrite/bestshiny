/**
 * Path router for the two shells.
 *
 * The public site and the application never share chrome, so routing is a
 * question of which shell is mounted, not which panel is visible. Auth state
 * arrives asynchronously from app.js; until it does, neither shell is shown,
 * which avoids the login form flashing in front of an already-signed-in user.
 */

export const MARKETING_ROUTES = ["/", "/product", "/models", "/pricing"];
export const AUTH_ROUTES = ["/login", "/signup"];
export const APP_ROUTE = "/app";
export const ADMIN_ROUTE = "/admin";

const listeners = new Set();
const state = { route: normalize(location.pathname), user: null, authResolved: false };

function normalize(pathname) {
  const path = (pathname || "/").replace(/\/+$/, "") || "/";
  if (MARKETING_ROUTES.includes(path) || AUTH_ROUTES.includes(path) || path === APP_ROUTE) return path;
  if (path.startsWith(`${APP_ROUTE}/`)) return path;
  if (path === ADMIN_ROUTE || path.startsWith(`${ADMIN_ROUTE}/`)) return path;
  return "/";
}

export function currentRoute() {
  return state.route;
}

export function isAppRoute(route = state.route) {
  return route === APP_ROUTE || route.startsWith(`${APP_ROUTE}/`);
}

export function isAdminRoute(route = state.route) {
  return route === ADMIN_ROUTE || route.startsWith(`${ADMIN_ROUTE}/`);
}

export function currentUser() {
  return state.user;
}

/** Where the router thinks we should be, given the route and who is signed in. */
function resolve(route) {
  if (!state.authResolved) return route;
  if (isAppRoute(route) && !state.user) return "/login";
  if (isAdminRoute(route) && !state.user) return "/login";
  if (isAdminRoute(route) && !["ADMIN", "SUPER_ADMIN"].includes(state.user?.platform_role)) return APP_ROUTE;
  if (AUTH_ROUTES.includes(route) && state.user) return APP_ROUTE;
  return route;
}

function apply({ replace = false } = {}) {
  const target = resolve(state.route);
  if (target !== state.route) {
    state.route = target;
    history.replaceState({}, "", target);
  } else if (replace) {
    history.replaceState({}, "", target);
  }

  const publicShell = document.getElementById("publicShell");
  const appShell = document.getElementById("appShell");
  const adminShell = document.getElementById("adminShell");
  const ready = state.authResolved;
  const onAdmin = ready && isAdminRoute(state.route);
  const onApp = ready && isAppRoute(state.route) && !onAdmin;

  publicShell.hidden = !ready || onApp || onAdmin;
  appShell.hidden = !onApp;
  if (adminShell) adminShell.hidden = !onAdmin;
  appShell.classList.toggle("auth-locked", !onApp);
  document.body.classList.toggle("public-route", ready && !onApp && !onAdmin);
  document.body.classList.toggle("admin-route", onAdmin);

  document.querySelectorAll("[data-nav]").forEach((link) => {
    link.classList.toggle("active", link.dataset.nav === state.route);
  });

  listeners.forEach((listener) => listener(state.route, state.user));
}

export function navigate(path, { replace = false } = {}) {
  const next = normalize(path);
  const target = resolve(next);
  state.route = next;
  if (replace) history.replaceState({}, "", target);
  else history.pushState({}, "", target);
  apply();
  if (!isAppRoute(state.route) && !isAdminRoute(state.route)) window.scrollTo({ top: 0, behavior: "instant" });
}

export function onRoute(listener) {
  listeners.add(listener);
  if (state.authResolved) listener(state.route, state.user);
  return () => listeners.delete(listener);
}

/** Called by app.js once /api/auth/me has answered — with a user or with null. */
export function setAuth(user) {
  state.user = user || null;
  const first = !state.authResolved;
  state.authResolved = true;
  if (first && state.route === "/" && state.user) {
    // A signed-in user landing on the marketing root still gets the marketing
    // root. Only an explicit /app, or a sign-in, opens the workspace.
    apply();
    return;
  }
  apply();
}

document.addEventListener("click", (event) => {
  const link = event.target.closest("a[data-link]");
  if (!link) return;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
  const url = new URL(link.href, location.origin);
  if (url.origin !== location.origin) return;
  event.preventDefault();
  navigate(url.pathname);
});

window.addEventListener("popstate", () => {
  state.route = normalize(location.pathname);
  apply();
});

apply({ replace: true });
