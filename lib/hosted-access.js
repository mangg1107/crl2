const { createHash, createHmac, timingSafeEqual } = require('node:crypto');

const COOKIE = '__Host-crl-session';
const SESSION_SECONDS = 8 * 60 * 60;
const PAGES = new Set(['/', '/dashboard.html', '/game.html', '/exchange.html']);

function matchesSecret(provided, expected) {
  if (!provided || !expected) return false;
  const digest = value => createHash('sha256').update(value).digest();
  return timingSafeEqual(digest(provided), digest(expected));
}

function signature(password, expires) {
  return createHmac('sha256', password).update(`crl-admin:${expires}`).digest('hex');
}

function createHostedAccess({ password, now = () => Date.now() }) {
  function signIn(req, res) {
    if (!password) return res.status(503).type('text').send('운영자 비밀번호를 설정해 주세요.');
    const header = req.get('authorization') || '';
    const encoded = /^Basic ([A-Za-z0-9+/=]+)$/i.exec(header)?.[1];
    const credentials = encoded && Buffer.from(encoded, 'base64').toString('utf8');
    if (!matchesSecret(credentials, `admin:${password}`)) {
      res.set('WWW-Authenticate', 'Basic realm="CRL2", charset="UTF-8"');
      return res.status(401).type('text').send('CRL2 운영자 계정으로 로그인해 주세요.');
    }
    const expires = Math.floor(now() / 1000) + SESSION_SECONDS;
    res.cookie(COOKIE, `${expires}.${signature(password, expires)}`, {
      maxAge: SESSION_SECONDS * 1000, path: '/', httpOnly: true, secure: true, sameSite: 'strict'
    });
    return res.redirect(303, PAGES.has(req.query.next) ? req.query.next : '/');
  }

  function authorize(req, res, next) {
    if (!password) return res.status(503).json({ error: '운영자 비밀번호를 설정해 주세요.' });
    const cookie = (req.get('cookie') || '').split(';').map(value => value.trim())
      .find(value => value.startsWith(`${COOKIE}=`))?.slice(COOKIE.length + 1);
    const session = /^(\d+)\.([a-f0-9]{64})$/.exec(cookie || '');
    if (!session || Number(session[1]) <= Math.floor(now() / 1000)
        || !matchesSecret(session[2], signature(password, session[1]))) {
      return res.status(401).json({ error: '운영자 로그인이 필요합니다.' });
    }
    const origin = req.get('origin');
    if ((origin && origin !== `https://${req.get('host')}`) || req.get('sec-fetch-site') === 'cross-site') {
      return res.status(403).json({ error: '운영 사이트에서 요청해 주세요.' });
    }
    return next();
  }

  return { signIn, authorize };
}

module.exports = { createHostedAccess, matchesSecret };
