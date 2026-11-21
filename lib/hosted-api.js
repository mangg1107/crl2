const express = require('express');
const { createApiApp } = require('./rlcm');
const { createBandGames } = require('./band-games');
const { createHostedAccess, matchesSecret } = require('./hosted-access');
const { createPostgresLock } = require('./postgres-lock');

function createHostedApi({ env = process.env, sheets, lock } = {}) {
  const app = express();
  app.disable('x-powered-by');
  const password = env.CRL_ADMIN_PASSWORD;
  const workerToken = env.CRL_WORKER_TOKEN;
  const spreadsheetId = env.GOOGLE_SPREADSHEET_ID?.trim();
  const access = createHostedAccess({ password });
  const runExclusive = lock || createPostgresLock({ connectionString: env.DATABASE_URL });

  app.use((_req, res, next) => {
    res.set('Cache-Control', 'private, no-store');
    next();
  });
  app.use(express.json({ limit: '16kb' }));

  function locked(operation) {
    if (!spreadsheetId) {
      throw Object.assign(new Error('GOOGLE_SPREADSHEET_ID를 설정해 주세요.'), { status: 503 });
    }
    return runExclusive(spreadsheetId, operation);
  }

  function workerAccess(req, res, next) {
    const expected = workerToken && `Bearer ${workerToken}`;
    if (!matchesSecret(req.get('authorization'), expected)) {
      return res.status(403).json({ error: 'BAND 봇 인증에 실패했습니다.' });
    }
    next();
  }

  function bandOperation(operation, status = 200) {
    return async (req, res, next) => {
      try {
        const body = await locked(async () => {
          const { bandContext } = createApiApp({ sheets, spreadsheetId });
          return operation(createBandGames(bandContext), req.body);
        });
        return status === 204 ? res.sendStatus(status) : res.status(status).json(body);
      } catch (error) {
        return next(error);
      }
    };
  }

  app.get('/api/crl/sign-in', access.signIn);
  app.get('/api/crl/internal/ping', workerAccess, (_req, res) => res.json({ ok: true }));
  app.post('/api/crl/internal/command', workerAccess,
    bandOperation((games, body) => games.handle(body)));

  app.use('/api', access.authorize);
  app.get('/api/crl/status', (_req, res) => res.json({ mode: 'hosted' }));
  app.get('/api/crl/bindings', bandOperation(games => games.list()));
  app.post('/api/crl/bindings', bandOperation((games, body) => games.bind(body), 204));
  app.delete('/api/crl/bindings', bandOperation((games, body) => games.unlink(body.bandName), 204));
  app.use('/api', (req, res, next) => {
    if (req.path.startsWith('/crl/') || ['/debug/env', '/logs/test'].includes(req.path)) {
      return res.status(404).json({ error: '경로를 찾을 수 없습니다.' });
    }
    // Each hosted request reads fresh Sheets state after obtaining the shared lock.
    const { app: rlcm } = createApiApp({ sheets, spreadsheetId, runExclusive: locked });
    return rlcm(req, res, next);
  });
  app.use((_req, res) => res.status(404).json({ error: '경로를 찾을 수 없습니다.' }));
  app.use((error, _req, res, _next) => {
    const expected = [400, 409, 413, 503].includes(error.status);
    if (!expected) console.error('CRL2 hosted API:', error.code || error.name);
    return res.status(expected ? error.status : 503).json({
      error: expected ? error.message : '정산 서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.'
    });
  });
  return app;
}

module.exports = { createHostedApi };
