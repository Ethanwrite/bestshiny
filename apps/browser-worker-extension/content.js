/* MIT-derived message bridge pattern from flowkit/extension/content.js. */
(function injectMainWorld() {
  const script = document.createElement('script');
  script.src = chrome.runtime.getURL('injected.js');
  script.onload = () => script.remove();
  (document.head || document.documentElement).appendChild(script);
})();

chrome.runtime.onMessage.addListener((message, _sender, reply) => {
  if (message.type !== 'GET_CAPTCHA_WITH_USER_ACTION') return;
  const handler = (event) => {
    if (event.detail?.requestId !== message.requestId) return;
    window.removeEventListener('PLATFORM_AUTHORIZATION_RESULT', handler);
    clearTimeout(timer);
    reply({token: event.detail.token, error: event.detail.error});
  };
  const timer = setTimeout(() => {
    window.removeEventListener('PLATFORM_AUTHORIZATION_RESULT', handler);
    reply({error: 'Authorization timed out'});
  }, 30000);
  window.addEventListener('PLATFORM_AUTHORIZATION_RESULT', handler);
  window.dispatchEvent(new CustomEvent('PLATFORM_AUTHORIZE_NEXT', {
    detail: {requestId: message.requestId, pageAction: message.pageAction},
  }));
  return true;
});

