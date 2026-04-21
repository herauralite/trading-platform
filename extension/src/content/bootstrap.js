(() => {
  window.postMessage({ source: 'talitrade-extension', event: 'content_bootstrap_ready' }, '*');
})();
