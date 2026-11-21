const { spawn } = require('node:child_process');
const { existsSync } = require('node:fs');
const path = require('node:path');

async function main() {
  const { CRL_PUBLIC_URL, CRL_WORKER_TOKEN, BAND_PAGE_URL } = process.env;
  if (!CRL_PUBLIC_URL || !CRL_WORKER_TOKEN || !BAND_PAGE_URL) {
    throw new Error('.env.worker에 CRL_PUBLIC_URL, CRL_WORKER_TOKEN, BAND_PAGE_URL을 설정해 주세요.');
  }
  let url;
  try {
    url = new URL(CRL_PUBLIC_URL);
  } catch {
    throw new Error('CRL_PUBLIC_URL에 Vercel 운영 주소를 입력해 주세요.');
  }
  if (url.protocol !== 'https:' || url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
    throw new Error('CRL_PUBLIC_URL에는 HTTPS 운영 주소만 입력해 주세요.');
  }
  const project = path.join(__dirname, '..');
  const python = process.env.CRL_PYTHON || path.join(project, '.venv', 'Scripts', 'python.exe');
  if (!existsSync(python)) throw new Error('Windows의 Python 가상환경을 먼저 설치해 주세요.');
  const base = `${url.origin}/api/crl/internal`;
  const check = await fetch(`${base}/ping`, {
    redirect: 'error', signal: AbortSignal.timeout(15000),
    headers: { Authorization: `Bearer ${CRL_WORKER_TOKEN}` }
  }).catch(() => null);
  const response = check?.ok ? await check.json().catch(() => null) : null;
  if (response?.ok !== true) {
    throw new Error('Vercel 연결을 확인할 수 없습니다. 운영 주소, 배포 보호 설정, 봇 토큰을 확인해 주세요.');
  }
  console.log('Vercel 연결 확인 완료. 열린 브라우저에서 필요하면 BAND에 로그인하세요.');
  const child = spawn(python, ['-u', 'worker/integrated_worker.py'], {
    cwd: project,
    env: { ...process.env, CRL_ENDPOINT: `${base}/command`,
      CRL_SECRET: CRL_WORKER_TOKEN, PYTHONIOENCODING: 'utf-8' },
    stdio: 'inherit'
  });
  child.once('error', () => {
    console.error('Python 실행에 실패했습니다. 가상환경 경로를 확인해 주세요.');
    process.exitCode = 1;
  });
  child.once('close', code => { process.exitCode = code ?? 1; });
}

main().catch(error => {
  console.error(error.message);
  process.exitCode = 1;
});
