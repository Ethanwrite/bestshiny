import QRCode from "qrcode";

const API = window.AI_DIRECTOR_API
  || (location.hostname === "127.0.0.1" && location.port === "18081"
    ? "http://127.0.0.1:18080"
    : "/api");
const CSRF_COOKIE_NAME = "ai_director_csrf";

const paymentState = {
  user: null,
  workspace: null,
  config: null,
  billing: null,
  checkout: null,
  checkoutUrl: "",
  pollTimer: null,
  busy: false,
};

const element = (id) => document.getElementById(id);
const humanStatus = (status = "") => String(status).replaceAll("_", " ").toLowerCase();

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

function render() {
  const offer = paymentState.config?.depay_offer;
  const isPro = paymentState.billing?.plan_tier === "PRO"
    || paymentState.workspace?.plan_tier === "PRO";
  const credits = Number(offer?.credits || 0).toLocaleString();
  const price = `${offer?.amount_usdc || "30.00"} USDC`;

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
  element("walletCreditingStatus").textContent = paymentState.config?.depay_callback_configured
    ? "Ready"
    : "Not configured yet";
  element("walletTitle").textContent = isPro ? "Top up credits" : "Upgrade to Pro";
  element("walletDescription").textContent = isPro
    ? `Each payment of ${price} adds ${credits} more credits.`
    : `One payment of ${price} unlocks Pro permanently and includes ${credits} credits. No subscription, no auto-renewal.`;
  element("walletOfferLabel").textContent = isPro ? "Top up credits" : "Upgrade to Pro";
  element("walletOfferPrice").textContent = price;
  element("walletOfferBenefits").innerHTML = isPro
    ? `+${credits} credits`
    : `✓ Unlock Pro<br>✓ Includes ${credits} credits`;
  element("payUsdcBtn").textContent = isPro ? `Pay ${price}` : "Upgrade to Pro";
  element("payUsdcBtn").disabled = paymentState.busy
    || !paymentState.workspace
    || !paymentState.config?.depay_checkout_configured
    || !paymentState.config?.depay_callback_configured
    || !offer;
  element("depayCheckout").hidden = !paymentState.checkoutUrl;
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
  paymentState.checkoutUrl = "";
  if (!user || !paymentState.workspace) {
    paymentState.billing = null;
    render();
    return;
  }
  try {
    paymentState.config = await api("/v1/payments/config");
    await refreshBilling();
    if (!paymentState.config.depay_checkout_configured) {
      setMessage("The DePay checkout link is not configured yet.");
    }
  } catch (error) {
    setMessage("", error.message);
  }
  render();
}

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
      setMessage("", `This payment did not complete (${humanStatus(checkout.status)}). An administrator can restore it.`);
      return;
    }
    paymentState.pollTimer = window.setTimeout(() => pollCheckout(checkoutId), 3000);
  } catch (error) {
    setMessage("", error.message);
  }
}

async function createCheckout() {
  setBusy(true);
  setMessage("Preparing your payment…");
  try {
    const checkout = await api(
      `/v1/workspaces/${paymentState.workspace.id}/depay-checkouts`,
      { method: "POST", body: "{}" },
    );
    paymentState.checkout = checkout;
    paymentState.checkoutUrl = checkout.checkout_url;
    element("depayQrCode").src = await QRCode.toDataURL(checkout.checkout_url, {
      width: 440,
      margin: 2,
      errorCorrectionLevel: "M",
      color: { dark: "#07131f", light: "#ffffff" },
    });
    element("depayCheckoutSummary").textContent =
      `${checkout.expected_usdc} USDC · ${checkout.expected_credits.toLocaleString()} credits`;
    setMessage("Scan the code or open DePay to confirm the Base USDC payment.");
    render();
    pollCheckout(checkout.id);
  } catch (error) {
    setMessage("", error.message);
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
element("openDePayBtn").addEventListener("click", () => {
  if (paymentState.checkoutUrl) window.open(paymentState.checkoutUrl, "_blank", "noopener,noreferrer");
});
element("copyDePayBtn").addEventListener("click", async () => {
  if (!paymentState.checkoutUrl) return;
  await navigator.clipboard.writeText(paymentState.checkoutUrl);
  setMessage("Payment link copied.");
});
window.addEventListener("ai-director:auth", (event) => {
  initializeForUser(event.detail).catch((error) => setMessage("", error.message));
});
render();
