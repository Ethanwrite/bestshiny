const API = window.AI_DIRECTOR_API
  || (location.hostname === "127.0.0.1" && location.port === "18081"
    ? "http://127.0.0.1:18080"
    : "/api");
const CSRF_COOKIE_NAME = "ai_director_csrf";
// Same 32px stroke style as app.js's ICON_FRAME / ICON_PROJECT / ICON_ALERT —
// the two modules never share code, so the glyph is redrawn here to match.
const ICON_RECEIPT = `<svg viewBox="0 0 32 32" fill="none" aria-hidden="true">
  <path d="M8 3.5h16v25l-3-2-3 2-3-2-3 2-3-2-3 2v-25a2.7 2.7 0 0 1 2.7-2.7Z" transform="translate(1.6 0)"
        stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>
  <path d="M12.4 11h9.2M12.4 15.8h9.2M12.4 20.6h5.6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" opacity=".6"/>
</svg>`;
const DEFAULT_SKU = "creator_50";
const WALLETCONNECT_PROJECT_KEY = "depay:wallets:wc2:projectId";
const PLAN_CONTENT = {
  starter_20: {
    name: "Essential",
    kicker: "Full experience",
    description: "A complete creative workflow for occasional image and video projects.",
    features: [
      "Unlock the paid quality levels",
      "Create with Shinier and Shiniest",
      "Ideal for personal work and concept tests",
      "Credits never expire",
    ],
    cta: "Choose Essential",
  },
  creator_50: {
    name: "Most Popular",
    kicker: "Creator choice",
    badge: "Recommended",
    description: "More room to create regularly, explore ideas, and iterate with confidence.",
    features: [
      "Everything in Essential",
      "Built for ongoing image and video work",
      "More versions, experiments, and iterations",
      "Ideal for shorts, ads, and content creators",
    ],
    cta: "Choose recommended pack",
  },
  pro_100: {
    name: "Professional",
    kicker: "Production ready",
    description: "Made for real creative projects, so your momentum never runs out of credits.",
    features: [
      "Everything in Most Popular",
      "Designed for frequent image and video work",
      "Supports complete production workflows",
      "Ideal for professionals and commercial projects",
    ],
    cta: "Choose Professional",
  },
};

// Every way a payment can end without credits, said in words. Three call sites
// used to print the raw settlement enum ("reconciliation required") straight at
// a paying customer, each with a different promise after it. The reason varies;
// the promise does not, so the promise is written exactly once.
const PAYMENT_FAILURE = {
  EXPIRED: "This payment expired before it was confirmed.",
  CANCELLED: "This payment was cancelled.",
  FAILED: "This payment did not go through.",
  RECONCILIATION_REQUIRED: "We could not confirm this payment automatically.",
};
const failureReason = (status) => PAYMENT_FAILURE[status] || "This payment did not complete.";
const paymentFailureMessage = (status) => `${failureReason(status)} No credits were added.`
  + " If money left your wallet, contact us and we will restore it.";

const paymentState = {
  user: null,
  workspace: null,
  config: null,
  billing: null,
  checkout: null,
  walletAccount: "",
  selectedSku: DEFAULT_SKU,
  selectedProvider: "xunhupay",
  pollTimer: null,
  unmountWidget: null,
  balanceAnimation: null,
  busy: false,
};

const element = (id) => document.getElementById(id);
const humanStatus = (status = "") => String(status).replaceAll("_", " ").toLowerCase();
const packages = () => (paymentState.selectedProvider === "xunhupay"
  ? paymentState.config?.xunhupay_packages
  : paymentState.config?.payment_packages) || [];
const packageFor = (provider, sku) => {
  const source = provider === "xunhupay"
    ? paymentState.config?.xunhupay_packages
    : paymentState.config?.payment_packages;
  return (source || []).find((plan) => plan.sku === sku) || null;
};
const paymentMethods = () => {
  const configured = paymentState.config?.payment_methods || [];
  const fromApi = (provider) => configured.find((method) => method.provider === provider);
  return [
    fromApi("xunhupay") || {
      provider: "xunhupay",
      configured: Boolean(paymentState.config?.xunhupay_configured),
    },
    fromApi("depay") || {
      provider: "depay",
      configured: Boolean(
        paymentState.config?.relayed_usdc_configured
        || paymentState.config?.depay_dynamic_configured,
      ),
    },
  ];
};
const selectedPackage = () =>
  packages().find((plan) => plan.sku === paymentState.selectedSku) || null;
const selectedMethod = () => paymentMethods()
  .find((method) => method.provider === paymentState.selectedProvider) || null;

function formatPrice(plan) {
  if (!plan) return "—";
  const amount = Number(plan.amount);
  if (!Number.isFinite(amount)) return "—";
  return plan.currency === "CNY"
    ? `¥${amount.toLocaleString("zh-CN", { maximumFractionDigits: 2 })}`
    : `${amount.toLocaleString("en-US", { maximumFractionDigits: 2 })} USDC`;
}

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
  const planIds = [...new Set([
    ...(paymentState.config?.payment_packages || []).map((plan) => plan.sku),
    ...(paymentState.config?.xunhupay_packages || []).map((plan) => plan.sku),
  ])];
  if (!planIds.length) {
    host.replaceChildren();
    return;
  }
  if (!selectedPackage()) {
    paymentState.selectedSku = planIds.includes(DEFAULT_SKU) ? DEFAULT_SKU : planIds[0];
  }
  // The plan cards are a radio group, so they are ONE tab stop: only the
  // checked card is tabbable and the arrows move between the cards inside it.
  // Choosing a card re-renders the whole group, which destroys the card the
  // user was standing on — so focus has to be put back on the new one by hand.
  const focusPlanCard = (sku) => {
    [...host.children].find((node) => node.dataset.sku === sku)?.focus();
  };
  const movePlanSelection = (step) => {
    const from = Math.max(0, planIds.indexOf(paymentState.selectedSku));
    const to = planIds[(from + step + planIds.length) % planIds.length];
    if (!to) return;
    paymentState.selectedSku = to;
    setMessage();
    render();
    focusPlanCard(to);
  };
  host.replaceChildren(...planIds.map((sku) => {
    const usdcPlan = packageFor("depay", sku);
    const cnyPlan = packageFor("xunhupay", sku);
    const plan = usdcPlan || cnyPlan;
    const copy = PLAN_CONTENT[sku] || {
      name: sku,
      kicker: "Credit pack",
      description: "Add credits for your next creation.",
      features: ["Credits never expire"],
      cta: "Choose pack",
    };
    const card = document.createElement("article");
    card.className = "wallet-plan";
    card.role = "radio";
    card.dataset.sku = sku;
    const chosen = sku === paymentState.selectedSku;
    card.tabIndex = chosen ? 0 : -1;
    card.setAttribute("aria-checked", String(chosen));
    if (chosen) card.classList.add("is-selected");
    if (copy.badge) card.classList.add("is-recommended");

    const kicker = document.createElement("div");
    kicker.className = "wallet-plan-kicker";
    const kickerText = document.createElement("span");
    kickerText.textContent = copy.kicker;
    kicker.append(kickerText);
    if (copy.badge) {
      const badge = document.createElement("em");
      badge.className = "wallet-plan-badge";
      badge.textContent = copy.badge;
      kicker.append(badge);
    }
    const name = document.createElement("h4");
    name.textContent = copy.name;
    const credits = document.createElement("span");
    credits.className = "wallet-plan-credits";
    credits.textContent = `${Number(plan?.credits || 0).toLocaleString()} Credits`;

    const prices = document.createElement("div");
    prices.className = "wallet-plan-prices";
    [
      ["depay", "USDC", usdcPlan],
      ["xunhupay", "WeChat Pay", cnyPlan],
    ].forEach(([provider, labelText, providerPlan]) => {
      const price = document.createElement("span");
      price.className = "wallet-plan-price";
      if (paymentState.selectedProvider === provider) price.classList.add("is-active");
      const label = document.createElement("small");
      label.textContent = labelText;
      const value = document.createElement("strong");
      value.textContent = providerPlan ? formatPrice(providerPlan) : "Unavailable";
      price.append(label, value);
      prices.append(price);
    });

    const description = document.createElement("p");
    description.className = "wallet-plan-desc";
    description.textContent = copy.description;
    const features = document.createElement("ul");
    features.className = "wallet-plan-features";
    copy.features.forEach((feature) => {
      const item = document.createElement("li");
      item.textContent = feature;
      features.append(item);
    });
    const action = document.createElement("button");
    action.type = "button";
    action.className = "btn btn-secondary wallet-plan-cta";
    action.textContent = chosen ? "Selected" : copy.cta;
    const select = () => {
      paymentState.selectedSku = sku;
      setMessage();
      render();
    };
    action.addEventListener("click", (event) => {
      event.stopPropagation();
      select();
    });
    card.addEventListener("click", select);
    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        select();
        return;
      }
      const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
      if (!step) return;
      event.preventDefault();
      movePlanSelection(step);
    });
    card.append(kicker, name, credits, prices, description, features, action);
    return card;
  }));
}

function renderMethods() {
  const host = element("walletMethods");
  const methods = paymentMethods();
  if (!methods.length) {
    host.replaceChildren();
    return;
  }
  if (!selectedMethod()?.configured) {
    paymentState.selectedProvider = methods.find((method) => method.configured)?.provider
      || methods[0].provider;
  }
  host.replaceChildren(...methods.map((method) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "wallet-method";
    button.role = "radio";
    button.dataset.provider = method.provider;
    const chosen = method.provider === paymentState.selectedProvider;
    button.setAttribute("aria-checked", String(chosen));
    if (chosen) button.classList.add("is-selected");
    button.disabled = !method.configured || paymentState.busy;
    const icon = document.createElement("span");
    icon.className = "wallet-method-icon";
    icon.textContent = method.provider === "xunhupay" ? "W" : "◈";
    const label = document.createElement("span");
    label.className = "wallet-method-label";
    const title = document.createElement("strong");
    title.textContent = method.provider === "xunhupay" ? "WeChat Pay" : "USDC payment";
    const detail = document.createElement("small");
    detail.textContent = method.provider === "xunhupay"
      ? "Scan with WeChat for a quick, convenient payment."
      : "Pay with a digital wallet. Credits are added after confirmation.";
    label.append(title, detail);
    const state = document.createElement("span");
    state.className = "wallet-method-state";
    const methodPlan = packageFor(method.provider, paymentState.selectedSku);
    state.textContent = method.configured && methodPlan ? formatPrice(methodPlan) : "Unavailable";
    button.append(icon, label, state);
    button.addEventListener("click", () => {
      paymentState.selectedProvider = method.provider;
      clearWalletConnectQr();
      clearXunhuPayQr();
      render();
    });
    return button;
  }));
}

function render() {
  const methods = paymentMethods();
  const activeMethod = methods.find(
    (method) => method.provider === paymentState.selectedProvider && method.configured,
  );
  if (!activeMethod) {
    paymentState.selectedProvider = methods.find((method) => method.configured)?.provider
      || paymentState.selectedProvider;
  }
  const plan = selectedPackage();
  const isPro = paymentState.billing?.plan_tier === "PRO"
    || paymentState.workspace?.plan_tier === "PRO";
  const credits = Number(plan?.credits || 0).toLocaleString();
  const price = formatPrice(plan);
  const isXunhuPay = paymentState.selectedProvider === "xunhupay";

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
  element("walletNetwork").textContent = isXunhuPay
    ? "WeChat"
    : (paymentState.config?.network === "BASE_MAINNET" ? "Base Mainnet" : "Base");
  element("walletCurrency").textContent = isXunhuPay ? "CNY" : "Native USDC";
  const relayed = paymentState.config?.relayed_usdc_configured;
  const qrConfigured = relayed && paymentState.config?.reown_configured;
  element("walletProviderLabel").textContent = "Fuel your next creation";
  // The Channel fact names what the customer is paying WITH, not which of our
  // two USDC integrations happens to be wired up — that distinction is ours.
  element("walletSettlement").textContent = isXunhuPay ? "WeChat Pay" : "USDC wallet";
  element("walletCreditingStatus").textContent = (isXunhuPay
    ? paymentState.config?.xunhupay_configured
    : relayed || paymentState.config?.depay_dynamic_configured)
    ? "Automatic"
    : "Unavailable";
  element("walletTitle").textContent = "Fuel your next creation";
  element("walletDescription").textContent = "Choose the right credit pack. Start anytime, upgrade anytime.";
  renderMethods();
  renderPlans();
  const copy = PLAN_CONTENT[paymentState.selectedSku] || { name: paymentState.selectedSku };
  element("walletConfirmPlan").textContent = `${copy.name}${isPro ? " top-up" : " · Upgrades you to Pro"}`;
  element("walletConfirmPrice").textContent = price;
  element("walletConfirmCredits").textContent = `${credits} Credits`;
  element("payUsdcBtn").textContent = plan
    ? (isXunhuPay
      ? `Confirm and pay ${price} with WeChat`
      : qrConfigured
      ? `Confirm and scan to pay ${price}`
      : `Confirm and pay ${price}`)
    : "Choose a credit pack";
  element("payUsdcBtn").disabled = paymentState.busy
    || !paymentState.workspace
    || !selectedMethod()?.configured
    || !plan;
  const browserPay = element("payBrowserWalletBtn");
  browserPay.hidden = isXunhuPay || !(qrConfigured && window.ethereum?.request);
  browserPay.disabled = paymentState.busy;
  element("walletConfirmation").classList.toggle("is-confirming", paymentState.busy);
  if (plan
    && !paymentState.busy
    && !element("walletStatus").textContent
    && !element("walletError").textContent) {
    setMessage("Confirm your pack and payment method to continue.");
  }
}

async function refreshBilling() {
  if (!paymentState.workspace) return;
  paymentState.billing = await api(`/v1/workspaces/${paymentState.workspace.id}/billing`);
  paymentState.workspace.plan_tier = paymentState.billing.plan_tier;
  render();
}

function resetWalletView() {
  element("walletPurchaseView").hidden = false;
  element("walletSuccessView").hidden = true;
  element("walletSuccessView").classList.remove("is-animating");
  element("walletHistoryPanel").hidden = true;
  element("walletHistoryList").replaceChildren();
}

/** Spec §11 row 20: an empty history is not a dead end — "See the plans"
 *  swaps back to the purchase view (out of the success/history views this
 *  panel can only be reached from) and scrolls the pack grid into frame. */
function walletEmptyBlock() {
  const block = document.createElement("div");
  block.className = "empty-block is-compact";
  const icon = document.createElement("span");
  icon.className = "empty-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.innerHTML = ICON_RECEIPT;
  const headline = document.createElement("strong");
  headline.textContent = "No top-ups yet";
  const body = document.createElement("p");
  body.textContent = "Your receipts appear here the moment a payment settles.";
  const cta = document.createElement("button");
  cta.type = "button";
  cta.className = "btn btn-tertiary";
  cta.textContent = "See the plans";
  cta.addEventListener("click", () => {
    resetWalletView();
    element("walletPlans").scrollIntoView({ block: "center", behavior: "smooth" });
  });
  block.append(icon, headline, body, cta);
  return block;
}

function animateCreditBalance(fromBalance, toBalance) {
  if (paymentState.balanceAnimation) cancelAnimationFrame(paymentState.balanceAnimation);
  const start = Number.isFinite(Number(fromBalance)) ? Number(fromBalance) : Number(toBalance);
  const end = Number(toBalance);
  const duration = 760;
  const startedAt = performance.now();
  const successBalance = element("walletSuccessBalance");
  successBalance.classList.remove("is-counting");
  void successBalance.offsetWidth;
  successBalance.classList.add("is-counting");

  const paint = (value) => {
    const formatted = Math.round(value).toLocaleString();
    element("walletSuccessBalance").textContent = `${formatted} Credits`;
    element("walletCreditBalance").textContent = `${formatted} Credits`;
    const topBalance = element("creditsAmount");
    if (topBalance) topBalance.textContent = `${formatted} credits`;
  };
  const tick = (now) => {
    const progress = Math.min(1, (now - startedAt) / duration);
    const eased = 1 - ((1 - progress) ** 3);
    paint(start + ((end - start) * eased));
    if (progress < 1) paymentState.balanceAnimation = requestAnimationFrame(tick);
    else paymentState.balanceAnimation = null;
  };
  paymentState.balanceAnimation = requestAnimationFrame(tick);
}

function showPaymentSuccess(payment, balanceBefore) {
  finishWidget();
  clearWalletConnectQr();
  clearXunhuPayQr();
  setMessage();
  const provider = payment.provider || paymentState.selectedProvider;
  const planId = payment.plan_id || payment.sku || paymentState.checkout?.plan_id
    || paymentState.checkout?.sku || paymentState.selectedSku;
  const copy = PLAN_CONTENT[planId] || { name: planId };
  const plan = packageFor(provider, planId);
  const amount = payment.amount ?? payment.amount_usdc ?? plan?.amount;
  const currency = payment.currency || plan?.currency || (provider === "xunhupay" ? "CNY" : "USDC");
  const credits = Number(payment.credits_granted ?? payment.credits ?? plan?.credits ?? 0);
  const providerLabel = provider === "xunhupay" ? "WeChat Pay" : "USDC payment";

  element("walletSuccessPlan").textContent = `${copy.name} pack`;
  element("walletSuccessAmount").textContent = `${providerLabel} · ${formatPrice({ amount, currency })}`;
  element("walletSuccessCredits").textContent = `+${credits.toLocaleString()} Credits`;
  element("walletPurchaseView").hidden = true;
  const success = element("walletSuccessView");
  success.hidden = false;
  success.classList.remove("is-animating");
  void success.offsetWidth;
  success.classList.add("is-animating");
  element("walletHistoryPanel").hidden = true;
  animateCreditBalance(balanceBefore, paymentState.billing?.credit_balance || balanceBefore);
}

async function showPaymentHistory() {
  if (!paymentState.workspace) return;
  const panel = element("walletHistoryPanel");
  const list = element("walletHistoryList");
  panel.hidden = false;
  list.replaceChildren();
  const loading = document.createElement("div");
  loading.className = "wallet-history-empty";
  loading.textContent = "Loading top-up history…";
  list.append(loading);
  element("walletHistoryBtn").disabled = true;
  try {
    const response = await api(`/v1/workspaces/${paymentState.workspace.id}/payments/history`);
    list.replaceChildren();
    if (!response.items?.length) {
      list.append(walletEmptyBlock());
      return;
    }
    response.items.forEach((item) => {
      const row = document.createElement("div");
      row.className = "wallet-history-row";
      const title = document.createElement("strong");
      const copy = PLAN_CONTENT[item.plan_id] || { name: item.plan_id };
      title.textContent = `${copy.name} pack · ${item.provider === "xunhupay" ? "WeChat Pay" : "USDC payment"}`;
      const amount = document.createElement("b");
      amount.textContent = formatPrice(item);
      const detail = document.createElement("small");
      const status = ({ PAID: "Credited", PENDING: "Pending", CANCELLED: "Cancelled", EXPIRED: "Expired", RECONCILIATION_REQUIRED: "Reconciling" })[item.status] || humanStatus(item.status);
      detail.textContent = `${Number(item.credits).toLocaleString()} Credits · ${status}`;
      const date = document.createElement("small");
      date.textContent = new Date(item.paid_at || item.created_at).toLocaleString("en-US", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      });
      row.append(title, amount, detail, date);
      list.append(row);
    });
  } catch (error) {
    list.replaceChildren();
    const failed = document.createElement("div");
    failed.className = "wallet-history-empty";
    failed.textContent = error.message;
    list.append(failed);
  } finally {
    element("walletHistoryBtn").disabled = false;
  }
}

async function initializeForUser(user) {
  window.clearTimeout(paymentState.pollTimer);
  paymentState.pollTimer = null;
  paymentState.user = user;
  paymentState.workspace = chooseWorkspace(user);
  paymentState.checkout = null;
  resetWalletView();
  if (!user || !paymentState.workspace) {
    paymentState.billing = null;
    render();
    return;
  }
  try {
    paymentState.config = await api("/v1/payments/config");
    paymentState.selectedProvider = paymentState.config.xunhupay_configured
      ? "xunhupay"
      : "depay";
    await refreshBilling();
    if (!(paymentState.config.relayed_usdc_configured
      || paymentState.config.depay_dynamic_configured
      || paymentState.config.xunhupay_configured)) {
      setMessage("Payments are not switched on yet. Nothing can be topped up right now.");
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
      const balanceBefore = paymentState.billing?.credit_balance || 0;
      await refreshBilling();
      window.dispatchEvent(new CustomEvent("ai-director:plan-changed", {
        detail: {
          workspaceId: paymentState.workspace.id,
          planTier: paymentState.billing.plan_tier,
        },
      }));
      showPaymentSuccess({ ...checkout, provider: "depay" }, balanceBefore);
      return;
    }
    if (["EXPIRED", "CANCELLED", "RECONCILIATION_REQUIRED"].includes(checkout.status)) {
      finishWidget();
      setMessage("", paymentFailureMessage(checkout.status));
      return;
    }
    paymentState.pollTimer = window.setTimeout(() => pollCheckout(checkoutId), 3000);
  } catch (error) {
    setMessage("", error.message);
  }
}

async function createCheckout() {
  if (paymentState.selectedProvider === "xunhupay") {
    return createXunhuPayCheckout();
  }
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
        provider: "depay",
        plan_id: plan.sku,
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

function clearXunhuPayQr() {
  const host = element("xunhupayQr");
  host.replaceChildren();
  host.hidden = true;
}

function showXunhuPayCheckout(checkout) {
  const host = element("xunhupayQr");
  host.replaceChildren();
  if (checkout.url_qrcode) {
    const image = document.createElement("img");
    image.src = checkout.url_qrcode;
    image.alt = `Scan to pay ${formatPrice(checkout)}`;
    image.referrerPolicy = "no-referrer";
    host.append(image);
  }
  if (checkout.url) {
    const link = document.createElement("a");
    link.className = "btn btn-secondary xunhupay-open";
    link.href = checkout.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "Open mobile payment page";
    host.append(link);
  }
  host.hidden = host.childElementCount === 0;
}

async function pollXunhuPayCheckout(checkoutId) {
  window.clearTimeout(paymentState.pollTimer);
  try {
    const checkout = await api(
      `/v1/workspaces/${paymentState.workspace.id}/xunhupay-checkouts/${checkoutId}`,
    );
    if (checkout.status === "PAID") {
      clearXunhuPayQr();
      const balanceBefore = paymentState.billing?.credit_balance || 0;
      await refreshBilling();
      window.dispatchEvent(new CustomEvent("ai-director:plan-changed", {
        detail: {
          workspaceId: paymentState.workspace.id,
          planTier: paymentState.billing.plan_tier,
        },
      }));
      showPaymentSuccess({ ...checkout, provider: "xunhupay" }, balanceBefore);
      return;
    }
    if (["EXPIRED", "CANCELLED", "RECONCILIATION_REQUIRED"].includes(checkout.status)) {
      clearXunhuPayQr();
      setMessage("", paymentFailureMessage(checkout.status));
      return;
    }
    paymentState.pollTimer = window.setTimeout(
      () => pollXunhuPayCheckout(checkoutId),
      3000,
    );
  } catch (error) {
    setMessage("Waiting for WeChat Pay to confirm…", error.message);
    paymentState.pollTimer = window.setTimeout(
      () => pollXunhuPayCheckout(checkoutId),
      5000,
    );
  }
}

async function createXunhuPayCheckout() {
  const plan = selectedPackage();
  if (!plan) return;
  setBusy(true);
  clearWalletConnectQr();
  clearXunhuPayQr();
  setMessage("Creating your WeChat Pay order…");
  try {
    const checkout = await api("/v1/payments/checkout", {
      method: "POST",
      body: JSON.stringify({
        workspace_id: paymentState.workspace.id,
        provider: "xunhupay",
        plan_id: plan.sku,
      }),
    });
    paymentState.checkout = checkout;
    showXunhuPayCheckout(checkout);
    setMessage(`Complete the ${formatPrice(checkout)} payment. Credits are added only after confirmation.`);
    pollXunhuPayCheckout(checkout.id);
  } catch (error) {
    setMessage("", error.message || String(error));
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
    // The missing setting is REOWN_PROJECT_ID, but an env-var name is an
    // operator's problem: the customer gets the two routes that still work.
    throw new Error("Paying by QR code is not available right now. Use a browser wallet, or pay with WeChat Pay.");
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
        // Hard-coded on purpose, and the one place in the product where that is
        // correct: a QR code is read by a camera, not by a person, so it must
        // stay high-contrast dark-on-white whatever the surface around it does.
        // The corner squares are amber ink (--brand-text's value), not the old
        // magenta, which matched nothing anywhere in BestShiny.
        dotsOptions: { color: "#16181D", type: "rounded" },
        backgroundOptions: { color: "#ffffff" },
        cornersSquareOptions: { color: "#8A5606", type: "extra-rounded" },
        cornersDotOptions: { color: "#16181D", type: "dot" },
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
    throw new Error("No browser wallet was found. Use the QR code instead.");
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
      const balanceBefore = paymentState.billing?.credit_balance || 0;
      await refreshBilling();
      window.dispatchEvent(new CustomEvent("ai-director:plan-changed", {
        detail: {
          workspaceId: paymentState.workspace.id,
          planTier: paymentState.billing.plan_tier,
        },
      }));
      showPaymentSuccess(
        {
          ...paymentState.checkout,
          ...result,
          provider: "depay",
          amount: paymentState.checkout?.amount_usdc,
          currency: "USDC",
        },
        balanceBefore,
      );
      return;
    }
    if (["FAILED", "EXPIRED", "RECONCILIATION_REQUIRED"].includes(result.status)) {
      setMessage("", paymentFailureMessage(result.status));
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
      "Authorization signed. Processing the USDC payment — no further wallet action is needed, and BestShiny covers the network fee.",
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
  resetWalletView();
  if (!element("walletDialog").open) element("walletDialog").showModal();
});
element("closeWalletBtn").addEventListener("click", () => {
  window.clearTimeout(paymentState.pollTimer);
  paymentState.pollTimer = null;
  clearWalletConnectQr();
  clearXunhuPayQr();
  resetWalletView();
  element("walletDialog").close();
});
element("walletContinueBtn").addEventListener("click", () => {
  resetWalletView();
  element("walletDialog").close();
});
element("walletHistoryBtn").addEventListener("click", showPaymentHistory);
element("walletHistoryCloseBtn").addEventListener("click", () => {
  element("walletHistoryPanel").hidden = true;
});
element("payUsdcBtn").addEventListener("click", createCheckout);
element("payBrowserWalletBtn").addEventListener("click", () => createRelayedCheckout("browser"));
window.addEventListener("ai-director:auth", (event) => {
  initializeForUser(event.detail).catch((error) => setMessage("", error.message));
});
render();
