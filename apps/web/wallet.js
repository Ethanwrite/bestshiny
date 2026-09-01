const API = window.AI_DIRECTOR_API
  || (location.hostname === "127.0.0.1" && location.port === "18081"
    ? "http://127.0.0.1:18080"
    : "/api");
const CSRF_COOKIE_NAME = "ai_director_csrf";
const DEFAULT_SKU = "creator_50";

const paymentState = {
  user: null,
  workspace: null,
  config: null,
  billing: null,
  checkout: null,
  selectedSku: DEFAULT_SKU,
  pollTimer: null,
  busy: false,
};

const element = (id) => document.getElementById(id);
const humanStatus = (status = "") => String(status).replaceAll("_", " ").toLowerCase();
const packages = () => paymentState.config?.payment_packages || [];
const selectedPackage = () =>
  packages().find((plan) => plan.sku === paymentState.selectedSku) || null;

function cookieValue(name) {
  const prefix = `${encodeURIComponent(name)}=`;
  const item = document.cookie.split("; ").find((entry) => entry.startsWith(prefix));
  return item ? decodeURIComponent(item.slice(prefix.length)) : "";
}

async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (["POST", "PUT", "PATCH", "DELETE"].includes(method.toUpperCase())) {
    const csrf = cookieValue(CSRF_COOKIE_NAME);
    if (csrf) headers["X-CSRF-Token"] = csrf;
  }
  const response = await fetch(`${API}${path}`, {
    ...options,
    credentials: "include",
    headers,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(body.detail || `Request failed (${response.status})`);
  }
  return response.status === 204 ? null : response.json();
}

function chooseWorkspace(user) {
  const workspaces = user?.workspaces || [];
  return workspaces.find((workspace) => ["OWNER", "ADMIN"].includes(workspace.role))
    || workspaces[0]
    || null;
}

function setMessage(message = "", error = "") {
  element("walletStatus").textContent = message;
  element("walletError").textContent = error;
}

function setBusy(value) {
  paymentState.busy = value;
  render();
}

function renderPlans() {
  const host = element("walletPlans");
  const available = packages();
  if (!available.length) {
    host.replaceChildren();
    return;
  }
  if (!selectedPackage()) {
    const fallback = available.find((plan) => plan.recommended) || available[0];
    paymentState.selectedSku = fallback.sku;
  }
  host.replaceChildren(...available.map((plan) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "wallet-plan";
    card.role = "radio";
    card.dataset.sku = plan.sku;
    const chosen = plan.sku === paymentState.selectedSku;
    card.setAttribute("aria-checked", String(chosen));
    if (chosen) card.classList.add("is-selected");
    if (plan.recommended) card.classList.add("is-recommended");

    const price = document.createElement("strong");
    price.className = "wallet-plan-price";
    price.textContent = `${Math.round(Number(plan.amount))} USDC`;
    const credits = document.createElement("span");
    credits.className = "wallet-plan-credits";
    credits.textContent = `${Number(plan.credits).toLocaleString()} credits`;
    card.append(price, credits);
    if (plan.recommended) {
      const badge = document.createElement("em");
      badge.className = "wallet-plan-badge";
      badge.textContent = "Recommended";
      card.append(badge);
    }
    card.addEventListener("click", () => {
      paymentState.selectedSku = plan.sku;
      render();
    });
    return card;
  }));
}

function render() {
  const plan = selectedPackage();
  const isPro = paymentState.billing?.plan_tier === "PRO"
    || paymentState.workspace?.plan_tier === "PRO";
  const credits = Number(plan?.credits || 0).toLocaleString();
  const price = plan ? `${Math.round(Number(plan.amount))} USDC` : "—";

  // The top bar trigger is a credits pill with its own markup: update the
  // number inside it, never the button's text content.
  const amount = element("creditsAmount");
  if (amount && paymentState.billing) {
    amount.textContent = `${paymentState.billing.credit_balance.toLocaleString()} credits`;
  }
  element("walletBtn").title = isPro ? "Top up credits" : "Upgrade to Pro";

  element("walletCreditBalance").textContent = paymentState.billing
    ? `${paymentState.billing.credit_balance.toLocaleString()} credits`
    : "—";
  element("walletNetwork").textContent = paymentState.config?.network === "BASE_MAINNET"
    ? "Base Mainnet"
    : "Base";
  element("walletCreditingStatus").textContent = paymentState.config?.depay_dynamic_configured
    ? "Ready"
    : "Not configured yet";
  element("walletTitle").textContent = isPro ? "Top up credits" : "Upgrade to Pro";
  element("walletDescription").textContent = isPro
    ? `Pick a package — credits are added the moment the payment is confirmed.`
    : `Any package unlocks Pro permanently and adds its credits. No subscription, no auto-renewal.`;
  renderPlans();
  element("payUsdcBtn").textContent = plan
    ? (isPro ? `Pay ${price}` : `Upgrade — ${price}`)
    : "Pay with USDC";
  element("payUsdcBtn").disabled = paymentState.busy
    || !paymentState.workspace
    || !paymentState.config?.depay_dynamic_configured
    || !plan;
  if (plan && !paymentState.busy && !element("walletStatus").textContent) {
    setMessage(`${price} · ${credits} credits`);
  }
}

async function refreshBilling() {
  if (!paymentState.workspace) return;
  paymentState.billing = await api(`/v1/workspaces/${paymentState.workspace.id}/billing`);
  paymentState.workspace.plan_tier = paymentState.billing.plan_tier;
  render();
}

async function initializeForUser(user) {
  window.clearTimeout(paymentState.pollTimer);
  paymentState.pollTimer = null;
  paymentState.user = user;
  paymentState.workspace = chooseWorkspace(user);
  paymentState.checkout = null;
  if (!user || !paymentState.workspace) {
    paymentState.billing = null;
    render();
    return;
  }
  try {
    paymentState.config = await api("/v1/payments/config");
    await refreshBilling();
    if (!paymentState.config.depay_dynamic_configured) {
      setMessage("Card and wallet payments are not switched on yet.");
    }
  } catch (error) {
    setMessage("", error.message);
  }
  render();
}

// Success is whatever BestShiny's own settlement says it is. The widget
// closing means the user paid, not that we were paid.
async function pollCheckout(checkoutId) {
  window.clearTimeout(paymentState.pollTimer);
  try {
    const checkout = await api(
      `/v1/workspaces/${paymentState.workspace.id}/depay-checkouts/${checkoutId}`,
    );
    if (checkout.status === "PAID") {
      await refreshBilling();
      window.dispatchEvent(new CustomEvent("ai-director:plan-changed", {
        detail: {
          workspaceId: paymentState.workspace.id,
          planTier: paymentState.billing.plan_tier,
        },
      }));
      setMessage(
        checkout.purchase_kind === "UPGRADE_PRO_AND_CREDITS"
          ? `Pro unlocked. ${checkout.credits_granted.toLocaleString()} credits posted.`
          : `${checkout.credits_granted.toLocaleString()} credits posted.`,
      );
      return;
    }
    if (["EXPIRED", "CANCELLED", "RECONCILIATION_REQUIRED"].includes(checkout.status)) {
      setMessage("", `This payment did not complete (${humanStatus(checkout.status)}). Our team can restore it.`);
      return;
    }
    paymentState.pollTimer = window.setTimeout(() => pollCheckout(checkoutId), 3000);
  } catch (error) {
    setMessage("", error.message);
  }
}

async function createCheckout() {
  const plan = selectedPackage();
  if (!plan) return;
  setBusy(true);
  setMessage("Preparing your payment…");
  const dialog = element("walletDialog");
  try {
    // The browser sends a SKU. Amount, currency and credits are decided and
    // frozen by the server, and DePay reads them back from /depay/config.
    const checkout = await api("/v1/payments/checkout", {
      method: "POST",
      body: JSON.stringify({
        workspace_id: paymentState.workspace.id,
        sku: plan.sku,
      }),
    });
    paymentState.checkout = checkout;
    pollCheckout(checkout.id);
    setMessage("Opening the payment window…");
    // Loaded on click, not on boot: the widget carries the whole wallet stack
    // and would otherwise be ~3 MB of JavaScript on every page view.
    const { default: DePayWidgets } = await import("@depay/widgets");
    // The widget mounts with document.body.appendChild, and a <dialog> opened
    // via showModal() lives in the browser's top layer — so anything appended
    // to the body paints *behind* it and its backdrop. Our sheet has to step
    // aside, or the widget is present, initialized and completely invisible.
    if (dialog.open) dialog.close();
    try {
      await DePayWidgets.Payment({
        integration: checkout.integration_id,
        payload: { checkout_token: checkout.checkout_token },
      });
      setMessage("Waiting for the payment to confirm on Base…");
    } finally {
      if (!dialog.open) dialog.showModal();
    }
  } catch (error) {
    if (!dialog.open) dialog.showModal();
    // Closing the window without paying is a normal choice, not a failure.
    if (String(error).includes("USER_CLOSED_DIALOG")) {
      setMessage("Payment window closed — nothing has been charged.");
    } else {
      setMessage("", error.message || String(error));
    }
  } finally {
    setBusy(false);
  }
}

element("walletBtn").addEventListener("click", async () => {
  if (!paymentState.user) {
    try {
      await initializeForUser(await api("/api/auth/me"));
    } catch (error) {
      setMessage("", error.message);
    }
  }
  if (!element("walletDialog").open) element("walletDialog").showModal();
});
element("closeWalletBtn").addEventListener("click", () => {
  window.clearTimeout(paymentState.pollTimer);
  paymentState.pollTimer = null;
  element("walletDialog").close();
});
element("payUsdcBtn").addEventListener("click", createCheckout);
window.addEventListener("ai-director:auth", (event) => {
  initializeForUser(event.detail).catch((error) => setMessage("", error.message));
});
render();
