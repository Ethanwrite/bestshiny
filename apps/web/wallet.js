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
    throw new Error(body.detail || `请求失败 (${response.status})`);
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
  element("walletCreditBalance").textContent = paymentState.billing
    ? `${paymentState.billing.credit_balance.toLocaleString()} 积分`
    : "—";
  element("walletNetwork").textContent = paymentState.config?.network === "BASE_MAINNET"
    ? "Base Mainnet"
    : "Base";
  element("walletCreditingStatus").textContent = paymentState.config?.depay_callback_configured
    ? "签名回调已配置"
    : "尚未完成配置";
  element("walletBtn").textContent = isPro ? "Top Up Credits" : "Upgrade to Pro";
  element("walletTitle").textContent = isPro ? "Top Up Credits" : "Upgrade to Pro";
  element("walletDescription").textContent = isPro
    ? `每次支付 ${price}，再充值 ${credits} Credits。`
    : `一次支付 ${price}，永久解锁 Pro 并获得 ${credits} Credits。没有订阅或自动续费。`;
  element("walletOfferLabel").textContent = isPro ? "Top Up Credits" : "Upgrade to Pro";
  element("walletOfferPrice").textContent = price;
  element("walletOfferBenefits").innerHTML = isPro
    ? `+${credits} Credits`
    : `✓ Unlock Pro<br>✓ Includes ${credits} Credits`;
  element("payUsdcBtn").textContent = isPro ? `充值 ${credits} Credits` : "升级 Pro";
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
      setMessage("DePay 收款链接尚未完成配置。");
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
          ? `Pro 已开通，${checkout.credits_granted.toLocaleString()} Credits 已入账。`
          : `${checkout.credits_granted.toLocaleString()} Credits 已入账。`,
      );
      return;
    }
    if (["EXPIRED", "CANCELLED", "RECONCILIATION_REQUIRED"].includes(checkout.status)) {
      setMessage("", `支付状态为 ${checkout.status}，请联系管理员对账。`);
      return;
    }
    paymentState.pollTimer = window.setTimeout(() => pollCheckout(checkoutId), 3000);
  } catch (error) {
    setMessage("", error.message);
  }
}

async function createCheckout() {
  setBusy(true);
  setMessage("正在创建 PaymentIntent…");
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
      `${checkout.expected_usdc} USDC · ${checkout.expected_credits.toLocaleString()} Credits · ${checkout.order_ref}`;
    setMessage("请扫码或打开 DePay，确认 Base USDC 支付。");
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
  setMessage("付款链接已复制。");
});
window.addEventListener("ai-director:auth", (event) => {
  initializeForUser(event.detail).catch((error) => setMessage("", error.message));
});
render();
