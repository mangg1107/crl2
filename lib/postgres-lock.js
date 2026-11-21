const { createHash } = require('node:crypto');
const { Pool } = require('pg');

function createPostgresLock({ connectionString, pool } = {}) {
  if (!pool && connectionString) {
    pool = new Pool({ connectionString, max: 2, connectionTimeoutMillis: 5000,
      idleTimeoutMillis: 5000, allowExitOnIdle: true });
    pool.on('error', error => console.error('CRL2 lock connection:', error.code || error.name));
  }
  return async function runExclusive(spreadsheetId, operation) {
    if (!pool) throw Object.assign(new Error('DATABASE_URL을 설정해 주세요.'), { status: 503 });
    const hash = createHash('sha256').update(`crl2:${spreadsheetId}`).digest();
    const key = hash.readBigInt64BE().toString();
    const client = await pool.connect();
    let connectionError;
    const recordError = error => { connectionError = error; };
    client.on('error', recordError);
    try {
      await client.query('BEGIN');
      await client.query("SET LOCAL lock_timeout = '8s'");
      await client.query('SELECT pg_advisory_xact_lock($1::bigint)', [key]);
      const result = await operation();
      // Sheets has already returned. Close the lock transaction before responding.
      await client.query('COMMIT');
      return result;
    } catch (error) {
      try {
        await client.query('ROLLBACK');
      } catch (rollbackError) {
        connectionError = rollbackError;
      }
      if (error.code === '55P03') {
        throw Object.assign(new Error('정산 처리 중입니다. 잠시 후 다시 시도해 주세요.'), { status: 503 });
      }
      throw error;
    } finally {
      client.removeListener('error', recordError);
      client.release(connectionError);
    }
  };
}

module.exports = { createPostgresLock };
