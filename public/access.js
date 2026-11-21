async function crlFetch(url, options) {
  const response = await fetch(url, options);
  if (response.status === 401) {
    const login = new URL('/api/crl/sign-in', location.origin);
    login.searchParams.set('next', location.pathname);
    location.replace(login);
  }
  return response;
}
