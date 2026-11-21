const $ = id => document.getElementById(id);
const names = new Map();
const states = { stopped: '봇 정지', starting: 'BAND 연결 대기', running: '댓글 감시 중',
  stopping: '종료 중', failed: '실행 오류' };
let workerState = 'stopped';
let busy = false;
let hasPlayers = false;
let updatingBindings = false;
let hosted = false;

async function api(path, method = 'GET', data) {
  const response = await crlFetch(`/api/${path}`, {
    method,
    headers: data === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: data === undefined ? undefined : JSON.stringify(data)
  });
  if (response.status === 204) return;
  const result = await response.json();
  if (!response.ok) throw new Error(result.error);
  return result;
}

function note(id, text, error = false) {
  $(id).textContent = text;
  $(id).toggleAttribute('data-error', error);
}

function showStatus(info) {
  if (info.mode === 'hosted') {
    hosted = true;
    $('status').textContent = 'Vercel 운영 모드';
    $('toggle').hidden = true;
    $('server-note').textContent = 'BAND 봇은 Windows에서 npm run worker로 실행하세요.';
    $('server-note').hidden = false;
    $('log').textContent = 'BAND 봇 실행 기록은 Windows 콘솔에 표시됩니다.';
    return;
  }
  workerState = info.state;
  const stoppable = ['running', 'starting'].includes(workerState);
  $('status').dataset.state = workerState;
  const label = states[workerState];
  if ($('status').textContent !== label) $('status').textContent = label;
  $('toggle').textContent = stoppable ? '봇 정지' : '봇 시작';
  $('toggle').disabled = busy || workerState === 'stopping' || (!stoppable && (!info.configured || !info.pythonReady));
  $('server-note').hidden = info.configured && info.pythonReady;
  $('server-note').textContent = '봇을 시작하려면 .env의 BAND_PAGE_URL과 사이트용 Python 가상환경을 설정해 주세요.';
  const log = info.messages.join('\n') || '아직 실행 기록이 없습니다.';
  if ($('log').textContent !== log) {
    $('log').textContent = log;
    $('log').scrollTop = $('log').scrollHeight;
  }
}

async function updateStatus() {
  try {
    showStatus(await api('crl/status'));
  } catch {
    $('status').textContent = '서버 연결 끊김';
    $('toggle').disabled = true;
  }
}

function tableRow(values) {
  const row = document.createElement('tr');
  for (const value of values) row.insertCell().textContent = value;
  return row;
}

function showBindings(bindings) {
  const rows = bindings.map(binding => {
    const activeGame = binding.activeGame === 'baccarat' ? '바카라' : '블랙잭';
    const row = tableRow([binding.bandName, names.get(binding.playerId) || `#${binding.playerId}`,
      binding.playing ? `${activeGame} 진행 중` : binding.verified ? '확인 완료' : '확인 대기']);
    row.cells[2].toggleAttribute('data-playing', binding.playing);
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'quiet';
    button.textContent = '해제';
    button.setAttribute('aria-label', `${binding.bandName} 연결 해제`);
    button.disabled = binding.playing;
    button.addEventListener('click', () => changeBinding('DELETE', { bandName: binding.bandName }));
    row.insertCell().append(button);
    return row;
  });
  if (!rows.length) {
    const empty = tableRow(['연결된 참가자가 없습니다.']);
    empty.cells[0].colSpan = 4;
    rows.push(empty);
  }
  $('bindings').replaceChildren(...rows);
  const playing = bindings.filter(binding => binding.playing).length;
  $('player-count').textContent = `연결 ${bindings.length}명 · 진행 중 ${playing}명`;
  $('player-count').hidden = false;
}

async function refreshBindings() {
  if (updatingBindings) return;
  try {
    showBindings(await api('crl/bindings'));
  } catch (error) {
    if ($('player-count').hidden) {
      const row = tableRow(['참가자 연결을 불러오지 못했습니다.']);
      row.cells[0].colSpan = 4;
      $('bindings').replaceChildren(row);
    }
    note('binding-note', error.message, true);
  }
}

async function loadPlayers() {
  try {
    const players = await api('players');
    $('player').replaceChildren(new Option('참가자 선택', ''));
    for (const player of players) {
      names.set(Number(player.id), player.name);
      $('player').add(new Option(`${player.name} · #${player.id}`, player.id));
    }
    hasPlayers = players.length > 0;
  } catch (error) {
    $('player').replaceChildren(new Option('Google Sheets 설정 확인', ''));
    note('binding-note', error.message, true);
  }
  $('player').disabled = !hasPlayers;
  $('bind-button').disabled = !hasPlayers;
  await refreshBindings();
}

async function changeBinding(method, data) {
  if (updatingBindings) return;
  updatingBindings = true;
  $('bind-button').disabled = true;
  try {
    await api('crl/bindings', method, data);
    if (method === 'POST') $('bind').reset();
    note('binding-note', method === 'POST' ? '참가자를 연결했습니다.' : '연결을 해제했습니다.');
  } catch (error) {
    note('binding-note', error.message, true);
  } finally {
    updatingBindings = false;
    $('bind-button').disabled = !hasPlayers;
    await refreshBindings();
  }
}

$('bind').addEventListener('submit', event => {
  event.preventDefault();
  changeBinding('POST', { bandName: $('band-name').value, playerId: $('player').value });
});

$('toggle').addEventListener('click', async () => {
  busy = true;
  $('toggle').disabled = true;
  try {
    const action = ['running', 'starting'].includes(workerState) ? 'stop' : 'start';
    await api(`crl/${action}`, 'POST', {});
    note('binding-note', '');
  } catch (error) {
    note('binding-note', error.message, true);
  } finally {
    busy = false;
    await updateStatus();
  }
});

updateStatus();
loadPlayers();
setInterval(() => {
  if (!hosted && !document.hidden) updateStatus();
}, 3000);
setInterval(() => {
  if (!document.hidden && (hosted || ['running', 'starting'].includes(workerState))) {
    refreshBindings();
  }
}, 30000);
