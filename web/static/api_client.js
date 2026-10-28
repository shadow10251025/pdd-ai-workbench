/* The local session token stays in memory and never enters a saved draft. */
(() => {
  const nativeFetch = window.fetch.bind(window);
  let session;
  function bootstrap() {
    return session || (session = nativeFetch('/api/session', {cache:'no-store', credentials:'same-origin'})
      .then(async r => { if (!r.ok) throw new Error('请通过本地管理端地址打开页面'); return (await r.json()).session_token; })
      .catch(e => { session = undefined; throw e; }));
  }
  window.fetch = async (input, init = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    const method = (init.method || (typeof input === 'object' && input.method) || 'GET').toUpperCase();
    if (url.origin === location.origin && !['GET','HEAD','OPTIONS'].includes(method)) {
      const headers = new Headers(init.headers || (typeof input === 'object' && input.headers) || {});
      headers.set('X-PDD-Session', await bootstrap());
      return nativeFetch(input, {...init, headers, credentials:'same-origin'});
    }
    return nativeFetch(input, init);
  };
})();
