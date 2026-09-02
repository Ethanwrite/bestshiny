const API = window.AI_DIRECTOR_API
  || (location.hostname === "127.0.0.1" && location.port === "18081"
    ? "http://127.0.0.1:18080"
    : "/api");
const CSRF_COOKIE_NAME = "ai_director_csrf";
const DEFAULT_SKU = "creator_50";
const WALLETCONNECT_PROJECT_KEY = "depay:wallets:wc2:projectId";

const paymentState = {
  user: null,
  workspace: null,
  config: null,
  billing: null,
  checkout: null,
  walletAccount: "",
  selectedSku: DEFAULT_SKU,
  pollTimer: null,
  unmountWidget: null,
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
    const detail = typeof body.detail === "object"
      ? body.detail?.message || body.detail?.code
      : body.detail;
    throw new Error(detail || `Request failed (${response.status})`);
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
  const relayed = paymentState.config?.relayed_usdc_configured;
  const qrConfigured = relayed && paymentState.config?.reown_configured;
  element("walletProviderLabel").textContent = relayed
    ? "WalletConnect · Base USDC"
    : "DePay · Base USDC";
  element("walletSettlement").textContent = relayed ? "BestShiny Relayer" : "DePay";
  element("walletCreditingStatus").textContent = relayed
    || paymentState.config?.depay_dynamic_configured
    ? "Ready"
    : "Not configured yet";
  element("walletTitle").textContent = isPro ? "Top up credits" : "Upgrade to Pro";
  element("walletDescription").textContent = isPro
    ? `Pick a package — credits are added when the Base USDC transfer confirms.${relayed ? " Network fees are sponsored." : ""}`
    : `Any package unlocks Pro permanently and adds its credits. No subscription, no auto-renewal.${relayed ? " No Base ETH is required." : ""}`;
  renderPlans();
  element("payUsdcBtn").textContent = plan
    ? (qrConfigured
      ? `Scan to pay ${price}`
      : (isPro ? `Pay ${price}` : `Upgrade — ${price}`))
    : "Pay with USDC";
  element("payUsdcBtn").disabled = paymentState.busy
    || !paymentState.workspace
    || !(relayed || paymentState.config?.depay_dynamic_configured)
    || !plan;
  const browserPay = element("payBrowserWalletBtn");
  browserPay.hidden = !(qrConfigured && window.ethereum?.request);
  browserPay.disabled = paymentState.busy;
  if (plan
    && !paymentState.busy
    && !element("walletStatus").textContent
    && !element("walletError").textContent) {
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
    if (!(paymentState.config.relayed_usdc_configured
      || paymentState.config.depay_dynamic_configured)) {
      setMessage("Card and wallet payments are not switched on yet.");
    }
  } catch (error) {
    setMessage("", error.message);
  }
  render();
}

// Hand the screen back to our own sheet once the purchase reaches a terminal
// state. Until then the widget owns it, and reopening a modal <dialog> would
// bury the widget in the top layer.
function finishWidget() {
  if (paymentState.unmountWidget) {
    try {
      paymentState.unmountWidget();
    } catch {
      // The widget may already be gone; nothing here is worth surfacing.
    }
    paymentState.unmountWidget = null;
  }
  const dialog = element("walletDialog");
  if (!dialog.open) dialog.showModal();
}

// Success is whatever BestShiny's own settlement says it is. The widget
// reporting a sent transaction means the user paid, not that we were paid.
async function pollCheckout(checkoutId) {
  window.clearTimeout(paymentState.pollTimer);
  try {
    const checkout = await api(
      `/v1/workspaces/${paymentState.workspace.id}/depay-checkouts/${checkoutId}`,
    );
    if (checkout.status === "PAID") {
      finishWidget();
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
      finishWidget();
      setMessage("", `This payment did not complete (${humanStatus(checkout.status)}). Our team can restore it.`);
      return;
    }
    paymentState.pollTimer = window.setTimeout(() => pollCheckout(checkoutId), 3000);
  } catch (error) {
    setMessage("", error.message);
  }
}

async function createCheckout() {
  if (paymentState.config?.relayed_usdc_configured) {
    return createRelayedCheckout();
  }
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
    // Payment() resolves with { unmount } the moment the widget *mounts* — the
    // user has not connected a wallet, let alone paid. So the progress story
    // comes from the widget's own callbacks, not from this promise, and our
    // sheet stays out of the way until the purchase is actually finished.
    const { unmount } = await DePayWidgets.Payment({
      integration: checkout.integration_id,
      payload: { checkout_token: checkout.checkout_token },
      sent: () => setMessage("Payment sent — waiting for Base to confirm…"),
      succeeded: () => setMessage("Confirmed on Base — posting your credits…"),
      failed: () => setMessage("", "The payment failed on Base. Nothing was charged."),
      error: (widgetError) => setMessage("", String(widgetError)),
    });
    paymentState.unmountWidget = unmount;
    setMessage("Complete the payment in the DePay window.");
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

function clearWalletConnectQr() {
  const host = element("walletConnectQr");
  host.replaceChildren();
  host.hidden = true;
}

async function connectBaseWalletWithQr() {
  const projectId = String(paymentState.config?.reown_project_id || "").trim();
  if (!projectId) {
    throw new Error("QR wallet connection is not configured yet. Set REOWN_PROJECT_ID first.");
  }
  localStorage.setItem(WALLETCONNECT_PROJECT_KEY, projectId);
  const [{ wallets }, { default: QRCodeStyling }] = await Promise.all([
    import("@depay/web3-wallets"),
    import("qr-code-styling"),
  ]);
  const wallet = new wallets.WalletConnectV2();
  await wallet.connect({
    connect: ({ uri }) => {
      if (!uri) return;
      const host = element("walletConnectQr");
      host.replaceChildren();
      host.hidden = false;
      new QRCodeStyling({
        width: 240,
        height: 240,
        type: "svg",
        data: uri,
        margin: 2,
        dotsOptions: { color: "#171714", type: "rounded" },
        backgroundOptions: { color: "#ffffff" },
        cornersSquareOptions: { color: "#ee2f7b", type: "extra-rounded" },
        cornersDotOptions: { color: "#171714", type: "dot" },
      }).append(host);
      setMessage("Scan this QR code with the wallet that will pay the Base USDC.");
    },
  });
  clearWalletConnectQr();
  const baseAccountId = (wallet.session?.namespaces?.eip155?.accounts || [])
    .find((accountId) => String(accountId).toLowerCase().startsWith("eip155:8453:0x"));
  const account = String(baseAccountId || "").split(":")[2]?.toLowerCase() || "";
  if (!/^0x[0-9a-f]{40}$/.test(account)) {
    throw new Error("The scanned wallet did not approve a valid Base Mainnet account.");
  }
  paymentState.walletAccount = account;
  return {
    account,
    signTypedData: (typedData) => wallet.signClient.request({
      topic: wallet.session.topic,
      chainId: `eip155:${typedData.domain.chainId}`,
      request: {
        method: "eth_signTypedData_v4",
        params: [account, JSON.stringify(typedData)],
      },
    }),
  };
}

async function connectInjectedBaseWallet() {
  const provider = window.ethereum;
  if (!provider?.request) {
    throw new Error("No browser wallet was found. Use the WalletConnect QR code instead.");
  }
  try {
    await provider.request({
      method: "wallet_switchEthereumChain",
      params: [{ chainId: "0x2105" }],
    });
  } catch (error) {
    if (Number(error?.code) !== 4902) throw error;
    await provider.request({
      method: "wallet_addEthereumChain",
      params: [{
        chainId: "0x2105",
        chainName: "Base",
        nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 },
        rpcUrls: ["https://mainnet.base.org"],
        blockExplorerUrls: ["https://basescan.org"],
      }],
    });
  }
  const accounts = await provider.request({ method: "eth_requestAccounts" });
  const account = String(accounts?.[0] || "").toLowerCase();
  if (!/^0x[0-9a-f]{40}$/.test(account)) throw new Error("The wallet returned an invalid account.");
  paymentState.walletAccount = account;
  return {
    account,
    signTypedData: (typedData) => provider.request({
      method: "eth_signTypedData_v4",
      params: [account, JSON.stringify(typedData)],
    }),
  };
}

async function connectBaseWallet(mode = "qr") {
  if (mode === "qr" && paymentState.config?.reown_configured) {
    return connectBaseWalletWithQr();
  }
  return connectInjectedBaseWallet();
}

async function pollRelayedAuthorization(authorizationId) {
  window.clearTimeout(paymentState.pollTimer);
  try {
    const result = await api(
      `/v1/workspaces/${paymentState.workspace.id}/relayed-authorizations/${authorizationId}/reconcile`,
      { method: "POST", body: "{}" },
    );
    if (result.status === "CONFIRMED") {
      await refreshBilling();
      window.dispatchEvent(new CustomEvent("ai-director:plan-changed", {
        detail: {
          workspaceId: paymentState.workspace.id,
          planTier: paymentState.billing.plan_tier,
        },
      }));
      setMessage(
        paymentState.checkout?.purchase_kind === "UPGRADE_PRO_AND_CREDITS"
          ? `Pro unlocked. ${result.credits_granted.toLocaleString()} credits posted.`
          : `${result.credits_granted.toLocaleString()} credits posted.`,
      );
      return;
    }
    if (["FAILED", "EXPIRED", "RECONCILIATION_REQUIRED"].includes(result.status)) {
      setMessage("", `This payment did not complete (${humanStatus(result.status)}).`);
      return;
    }
    paymentState.pollTimer = window.setTimeout(
      () => pollRelayedAuthorization(authorizationId),
      3000,
    );
  } catch (error) {
    setMessage("Payment is still being confirmed on Base…", error.message);
    paymentState.pollTimer = window.setTimeout(
      () => pollRelayedAuthorization(authorizationId),
      5000,
    );
  }
}

async function createRelayedCheckout(connectionMode = "qr") {
  const plan = selectedPackage();
  if (!plan) return;
  let checkout = null;
  setBusy(true);
  setMessage("Connect your wallet to authorize the Base USDC payment…");
  try {
    const { account, signTypedData } = await connectBaseWallet(connectionMode);
    checkout = await api("/v1/payments/relayed-checkout", {
      method: "POST",
      body: JSON.stringify({
        workspace_id: paymentState.workspace.id,
        sku: plan.sku,
        from_address: account,
      }),
    });
    paymentState.checkout = checkout;
    setMessage(
      `The connected wallet will pay ${checkout.amount_usdc} USDC. Review and sign the authorization; no Base ETH is required.`,
    );
    const signature = await signTypedData(checkout.typed_data);
    setMessage(
      "Authorization signed. Processing the USDC payment — no further wallet action is required; BestShiny pays the Gas…",
    );
    const submitted = await api(
      `/v1/workspaces/${paymentState.workspace.id}/relayed-authorizations/${checkout.id}/submit`,
      { method: "POST", body: JSON.stringify({ signature }) },
    );
    setMessage("Submitted on Base — waiting for confirmation…");
    if (submitted.status === "CONFIRMED") {
      await pollRelayedAuthorization(checkout.id);
    } else {
      pollRelayedAuthorization(checkout.id);
    }
  } catch (error) {
    clearWalletConnectQr();
    if (Number(error?.code) === 4001 || String(error).includes("User rejected")) {
      if (checkout?.id) {
        await api(
          `/v1/workspaces/${paymentState.workspace.id}/relayed-authorizations/${checkout.id}/cancel`,
          { method: "POST", body: "{}" },
        ).catch(() => {});
      }
      setMessage("Authorization cancelled — nothing was transferred.");
    } else {
      setMessage("Payment was not submitted — no USDC was transferred.", error.message || String(error));
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
  clearWalletConnectQr();
  element("walletDialog").close();
});
element("payUsdcBtn").addEventListener("click", createCheckout);
element("payBrowserWalletBtn").addEventListener("click", () => createRelayedCheckout("browser"));
window.addEventListener("ai-director:auth", (event) => {
  initializeForUser(event.detail).catch((error) => setMessage("", error.message));
});
render();
