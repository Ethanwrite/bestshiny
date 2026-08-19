const PLATFORM_SITE_KEY = '6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV';

window.addEventListener('PLATFORM_AUTHORIZE_NEXT', async (event) => {
  const {requestId, pageAction} = event.detail;
  try {
    const started = Date.now();
    while (!window.grecaptcha?.enterprise?.execute) {
      if (Date.now() - started > 10000) throw new Error('Google verification is not ready');
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
    const token = await window.grecaptcha.enterprise.execute(PLATFORM_SITE_KEY, {action: pageAction});
    window.dispatchEvent(new CustomEvent('PLATFORM_AUTHORIZATION_RESULT', {detail: {requestId, token}}));
  } catch (error) {
    window.dispatchEvent(new CustomEvent('PLATFORM_AUTHORIZATION_RESULT', {
      detail: {requestId, error: error.message || String(error)},
    }));
  }
});

