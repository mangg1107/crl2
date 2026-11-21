const { spawn } = require('node:child_process');
const { randomBytes } = require('node:crypto');
const { existsSync } = require('node:fs');
const path = require('node:path');
const express = require('express');
const { createApiApp } = require('./lib/rlcm');
const { createBandGames } = require('./lib/band-games');

function createServer({ sheets, spreadsheetId = process.env.GOOGLE_SPREADSHEET_ID } = {}) {
  const app = express();
  app.disable('x-powered-by');
  let queue = Promise.resolve();
  function runExclusive(operation) {
    const result = queue.then(operation);
    queue = result.catch(() => {});
    return result;
  }

  const { app: rlcm, bandContext } = createApiApp({ sheets, spreadsheetId, runExclusive });
  const games = createBandGames(bandContext);
  const secret = randomBytes(32).toString('hex');
  const python = process.env.CRL_PYTHON || path.join(__dirname, '.venv', 'Scripts', 'python.exe');
  const bandUrl = process.env.BAND_PAGE_URL?.trim() || '';
  const messages = [];
  let worker = null;
  let workerState = 'stopped';

  function log(line) {
    if (!line.trim()) return;
    messages.push(line.trim().slice(0, 500));
    if (messages.length > 30) messages.shift();
  }

  function status() {
    return { state: workerState, configured: Boolean(bandUrl), pythonReady: existsSync(python), messages };
  }

  function stop() {
    if (!worker || workerState === 'stopping') return;
    workerState = 'stopping';
    worker.stdin.end();
  }

  function start(port) {
    if (worker) return;
    if (!bandUrl || !existsSync(python)) {
      throw Object.assign(new Error('BAND_PAGE_URL과 Python 가상환경을 설정해 주세요.'), { status: 400 });
    }
    workerState = 'starting';
    const env = { ...process.env,
      CRL_ENDPOINT: `http://127.0.0.1:${port}/api/crl/internal/command`,
      CRL_SECRET: secret,
      PYTHONIOENCODING: 'utf-8'
    };
    delete env.GOOGLE_SERVICE_ACCOUNT_JSON;
    delete env.GOOGLE_CLIENT_EMAIL;
    delete env.GOOGLE_PRIVATE_KEY;
    const child = spawn(python, ['-u', 'worker/integrated_worker.py', '--managed'], {
      cwd: __dirname, env, windowsHide: true, stdio: ['pipe', 'ignore', 'pipe']
    });
    worker = child;
    child.stdin.on('error', () => {});
    child.on('error', () => log('Python 실행에 실패했습니다. 가상환경 경로를 확인해 주세요.'));
    child.stderr.setEncoding('utf8');
    let buffered = '';
    child.stderr.on('data', chunk => {
      buffered += chunk;
      const lines = buffered.split('\n');
      buffered = lines.pop();
      for (const line of lines) {
        if (line === 'CRL_READY') {
          if (workerState === 'starting') workerState = 'running';
        } else {
          log(line);
        }
      }
    });
    child.on('close', code => {
      log(buffered);
      worker = null;
      workerState = code === 0 ? 'stopped' : 'failed';
    });
    log('봇을 시작합니다. 전용 브라우저에서 필요하면 BAND에 로그인하세요.');
  }

  app.use((req, res, next) => {
    const port = req.socket.localPort;
    const hosts = [`127.0.0.1:${port}`, `localhost:${port}`];
    const host = req.get('host');
    const origin = req.get('origin');
    if (!hosts.includes(host) || (origin && !hosts.some(value => origin === `http://${value}`))) {
      return res.sendStatus(403);
    }
    res.set('Cache-Control', 'no-store');
    next();
  });
  app.use(express.json({ limit: '16kb' }));

  function route(handler) {
    return (req, res, next) => Promise.resolve().then(() => handler(req, res)).catch(next);
  }

  app.get('/api/crl/status', (_req, res) => res.json(status()));
  app.get('/api/crl/bindings', route(async (_req, res) => {
    res.json(await runExclusive(games.list));
  }));
  app.post('/api/crl/bindings', route(async (req, res) => {
    await runExclusive(() => games.bind(req.body));
    res.sendStatus(204);
  }));
  app.delete('/api/crl/bindings', route(async (req, res) => {
    await runExclusive(() => games.unlink(req.body.bandName));
    res.sendStatus(204);
  }));
  app.post('/api/crl/start', route(async (req, res) => {
    const bindings = await runExclusive(games.list);
    if (!bindings.length) return res.status(400).json({ error: '참가자를 한 명 이상 연결해 주세요.' });
    start(req.socket.localPort);
    res.json(status());
  }));
  app.post('/api/crl/stop', (_req, res) => {
    stop();
    res.json(status());
  });
  app.post('/api/crl/internal/command', route(async (req, res) => {
    if (req.get('authorization') !== `Bearer ${secret}`) return res.sendStatus(403);
    res.json(await runExclusive(() => games.handle(req.body)));
  }));
  app.use(express.static(path.join(__dirname, 'public')));
  app.get(['/dashboard', '/game', '/exchange'], (req, res) => res.redirect(`${req.path}.html`));
  app.use('/api', rlcm);
  app.use((error, _req, res, _next) => {
    const clientError = error.status >= 400 && error.status < 500;
    if (!clientError) console.error('통합 서버:', error.message);
    res.status(clientError ? error.status : 503).json({
      error: clientError ? error.message : 'Google Sheets에 연결하지 못했습니다. 서버 설정과 접속 상태를 확인해 주세요.'
    });
  });
  return { app, stop };
}

if (require.main === module) {
  const { app, stop } = createServer();
  const port = Number(process.env.PORT || 3000);
  const server = app.listen(port, '127.0.0.1', () => {
    console.log(`CRL2 · http://127.0.0.1:${server.address().port}`);
  });
  function shutdown() {
    stop();
    server.close();
  }
  process.once('SIGINT', shutdown);
  process.once('SIGTERM', shutdown);
}

module.exports = process.env.VERCEL
  ? require('./lib/hosted-api').createHostedApi()
  : { createServer };
