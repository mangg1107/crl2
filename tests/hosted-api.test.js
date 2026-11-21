const assert = require('node:assert/strict');
const { once, EventEmitter } = require('node:events');
const { IncomingMessage, ServerResponse } = require('node:http');
const { Duplex } = require('node:stream');
const { test } = require('node:test');
const { createHostedApi } = require('../lib/hosted-api');
const { createPostgresLock } = require('../lib/postgres-lock');

async function request(app, url, { method = 'GET', headers = {}, body } = {}) {
  const chunks = [];
  const socket = new Duplex({ read() {}, write(chunk, _encoding, callback) {
    chunks.push(chunk);
    callback();
  } });
  const req = new IncomingMessage(socket);
  req.method = method;
  req.url = url;
  req.httpVersionMajor = 1;
  req.httpVersionMinor = 1;
  req.headers = { host: 'crl2.example', ...headers };
  req.complete = true;
  if (body) {
    const json = JSON.stringify(body);
    req.headers['content-type'] = 'application/json';
    req.headers['content-length'] = String(Buffer.byteLength(json));
    req.push(Buffer.from(json));
  }
  req.push(null);
  const res = new ServerResponse(req);
  res.assignSocket(socket);
  const finished = once(res, 'finish');
  app(req, res);
  await finished;
  return { status: res.statusCode, headers: res.getHeaders(), data: Buffer.concat(chunks).toString() };
}

const env = {
  CRL_WORKER_TOKEN: 'test-worker-token',
  GOOGLE_SPREADSHEET_ID: 'test-spreadsheet'
};

test('hosted operator APIs are public without a login', async () => {
  const app = createHostedApi({ env });
  assert.equal((await request(app, '/api/crl/sign-in')).status, 404);
  const status = await request(app, '/api/crl/status');
  assert.equal(status.status, 200);
  assert.match(status.data, /"mode":"hosted"/);
  const publicStatus = await request(app, '/api/crl/status', { headers: { origin: 'https://other.example' } });
  assert.equal(publicStatus.status, 200);
  assert.equal((await request(app, '/api/debug/env')).status, 404);
});

test('worker token protects internal routes and reaches settlement', async () => {
  const calls = [];
  const sheets = { spreadsheets: { async get() {
    calls.push('sheets');
    return { data: { sheets: [] } };
  } } };
  const app = createHostedApi({ env, sheets, lock: async (id, operation) => {
    assert.equal(id, env.GOOGLE_SPREADSHEET_ID);
    calls.push('acquired');
    const body = await operation();
    calls.push('released');
    return body;
  } });
  const headers = { authorization: 'Bearer test-worker-token' };
  assert.equal((await request(app, '/api/crl/internal/ping')).status, 403);
  assert.equal((await request(app, '/api/crl/internal/ping', { headers })).status, 200);
  assert.equal((await request(app, '/api/crl/internal/command', { method: 'POST' })).status, 403);
  const result = await request(app, '/api/crl/internal/command', { method: 'POST', headers,
    body: { key: 'a'.repeat(64), author: '참가자', command: { action: 'verify' } } });
  assert.equal(result.status, 200);
  assert.match(result.data, /연결을 요청해 주세요/);
  assert.deepEqual(calls, ['acquired', 'sheets', 'released']);
});

test('an RLCM response waits for the hosted lock transaction to finish', async () => {
  const handlerDone = Promise.withResolvers();
  const releaseLock = Promise.withResolvers();
  const app = createHostedApi({ env, sheets: {}, lock: async (_id, operation) => {
    await operation();
    handlerDone.resolve();
    await releaseLock.promise;
  } });
  let sent = false;
  const response = request(app, '/api/').then(result => {
    sent = true;
    return result;
  });
  await handlerDone.promise;
  assert.equal(sent, false);
  releaseLock.resolve();
  const result = await response;
  assert.equal(result.status, 302);
  assert.equal(result.headers.location, '/dashboard.html');
});

test('the hosted API starts and settles PvP card games from persisted Sheets state', async () => {
  const today = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Seoul' }).format(new Date());
  const tables = {
    '설정': [['key', 'value'], ['lastGameLimitResetDate', today]],
    '플레이어': [
      ['id', 'name', 'team', 'red', 'blue', 'green', 'yellow', 'white',
        'rouletteLimit', 'highlowLimit', 'baccaratLimit', 'blackjackLimit', 'redblackLimit'],
      [1, 'A', 'A', 20, 0, 0, 0, 0, 3, 3, 3, 3, 3],
      [2, 'B', 'B', 20, 0, 0, 0, 0, 3, 3, 3, 3, 3]
    ],
    '로그': [['id', 'type', 'text', 'publicText', 'createdAt', 'groupId']],
    'PVP세션': [['id', 'gameType', 'playerOneId', 'playerTwoId', 'color', 'bet', 'deck', 'shoe',
      'playerOneCards', 'playerTwoCards', 'playerOneAction', 'playerTwoAction', 'turn', 'done',
      'winnerKey', 'result', 'updatedAt']]
  };
  function cells(range) {
    const [, title, start, from, end, to] = /^'([^']+)'!([A-Z])(\d*):([A-Z])(\d*)$/.exec(range);
    return { rows: tables[title], from: Number(from || 1) - 1, to: to ? Number(to) : undefined,
      start: start.charCodeAt(0) - 65, end: end.charCodeAt(0) - 64 };
  }
  function read(range) {
    const { rows, from, to, start, end } = cells(range);
    return rows.slice(from, to).map(row => row.slice(start, end));
  }
  const sheets = { spreadsheets: {
    async get() { return { data: { sheets: Object.keys(tables).map(title => ({ properties: { title } })) } }; },
    values: {
      async get({ range }) { return { data: { values: read(range) } }; },
      async batchGet({ ranges }) { return { data: { valueRanges: ranges.map(range => ({ values: read(range) })) } }; },
      async append({ range, requestBody }) {
        const { rows } = cells(range);
        rows.push(requestBody.values[0]);
        return { data: { updates: { updatedRange: range.replace(/!([A-Z]):([A-Z])$/, `!$1${rows.length}:$2${rows.length}`) } } };
      },
      async update({ range, requestBody }) {
        const { rows, from } = cells(range);
        rows[from] = requestBody.values[0];
      },
      async batchUpdate({ requestBody }) {
        for (const entry of requestBody.data) {
          const { rows, from } = cells(entry.range);
          rows[from] = entry.values[0];
        }
      },
      async clear({ range }) {
        const { rows, from } = cells(range);
        rows[from] = [];
      }
    }
  } };
  const app = createHostedApi({ env, sheets, lock: (_id, operation) => operation() });
  async function post(url, body) {
    const response = await request(app, url, { method: 'POST', body });
    assert.equal(response.status, 200, response.data);
    return JSON.parse(response.data.split('\r\n\r\n')[1]);
  }
  for (const [game, amount] of [['blackjack', 0], ['baccarat', 5]]) {
    const opened = await post(`/api/pvp-${game}/start`, { playerId: 1, opponentId: 2, color: 'red', amount });
    assert.equal(opened.state.turn, 'playerOne');
    const next = await post('/api/pvp/action', { sessionId: opened.state.id, action: 'stand' });
    assert.equal(next.state.turn, 'playerTwo');
    const finished = await post('/api/pvp/action', { sessionId: opened.state.id, action: 'stand' });
    assert.equal(finished.state.done, true);
    assert.equal(tables['플레이어'][1][3] + tables['플레이어'][2][3], 40);
    if (!amount) assert.deepEqual(tables['플레이어'].slice(1).map(row => row[3]), [20, 20]);
  }
});

test('Postgres lock holds through the operation and releases connections on success and failure', async () => {
  const statements = [];
  const client = new EventEmitter();
  client.query = async (sql, parameters) => { statements.push(parameters ? [sql, parameters] : sql); };
  client.release = () => { statements.push('release'); };
  const lock = createPostgresLock({ pool: { connect: async () => client } });
  const result = await lock('sheet', async () => {
    statements.push('operation');
    return 'ok';
  });
  assert.equal(result, 'ok');
  assert.deepEqual(statements.slice(-3), ['operation', 'COMMIT', 'release']);
  const firstKey = statements[2][1][0];
  await assert.rejects(lock('sheet', async () => { throw new Error('Sheets failed'); }), /Sheets failed/);
  assert.equal(statements[8][1][0], firstKey);
  assert.deepEqual(statements.slice(-2), ['ROLLBACK', 'release']);
  assert.equal(client.listenerCount('error'), 0);
});
