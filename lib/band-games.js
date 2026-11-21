const { randomInt, randomUUID } = require('node:crypto');
const { settleRoulette } = require('./dealer-rules');

const TITLE = 'BAND 게임';
const RANGE = "'BAND 게임'!A:H";
const HEADER = ['key', 'kind', 'playerId', 'bandName', 'color', 'state', 'reply', 'createdAt'];
const GAME_NAMES = { roulette: '룰렛', baccarat: '바카라', blackjack: '블랙잭' };
const DEFAULT_COLOR = 'red';
const HELP = '각 원댓글 아래에서 입력하세요. 칩 색 생략 시 Red입니다. 룰렛: 블루 10 / 레드, 바카라: 10 / 플레이어, 블랙잭: 10, 환전: 레드, 5, 그린.';

function cell(value) {
  const type = typeof value === 'number' ? 'numberValue' : 'stringValue';
  return { userEnteredValue: { [type]: value } };
}

function append(sheetId, values) {
  return { appendCells: { sheetId, rows: [{ values: values.map(cell) }], fields: 'userEnteredValue' } };
}

function inputError(message) {
  return Object.assign(new Error(message), { status: 400 });
}

function readEvents(rows) {
  const bindings = new Map();
  const hands = new Map();
  const replies = new Map();
  for (const [key, kind, id, bandName, color, rawState, reply] of rows) {
    const playerId = Number(id);
    if (kind === 'bind') {
      bindings.set(bandName, { bandName, playerId, color: DEFAULT_COLOR });
      hands.delete(playerId);
    } else if (kind === 'unlink') {
      bindings.delete(bandName);
      hands.delete(playerId);
    } else if (kind === 'command') {
      hands.set(playerId, JSON.parse(rawState));
      replies.set(key, reply);
    } else {
      throw new Error('BAND 게임 시트에 알 수 없는 기록이 있습니다.');
    }
  }
  return { bindings, hands, replies };
}

function createBandGames(core, drawRouletteNumber = () => randomInt(37)) {
  const { sheets, spreadsheetId } = core;
  const api = sheets.spreadsheets;
  let gameSheetId;

  function batch(requests) {
    return api.batchUpdate({ spreadsheetId, requestBody: { requests } }, { retry: false });
  }

  async function sheetIds() {
    const result = await api.get({ spreadsheetId, fields: 'sheets.properties(sheetId,title)' });
    return new Map(result.data.sheets.map(({ properties }) => [properties.title, properties.sheetId]));
  }

  async function events({ create = false } = {}) {
    if (gameSheetId === undefined) {
      gameSheetId = (await sheetIds()).get(TITLE);
    }
    if (gameSheetId === undefined && !create) return readEvents([]);
    if (gameSheetId === undefined) {
      const sheetId = 187920364;
      await batch([
        { addSheet: { properties: { sheetId, title: TITLE, gridProperties: { frozenRowCount: 1 } } } },
        append(sheetId, HEADER)
      ]);
      gameSheetId = sheetId;
    }
    const result = await api.values.get({ spreadsheetId, range: RANGE });
    const [header, ...rows] = result.data.values || [];
    if (!HEADER.every((name, index) => header?.[index] === name)) {
      throw new Error('BAND 게임 시트 헤더를 확인해 주세요.');
    }
    return readEvents(rows);
  }

  async function list() {
    const { bindings, hands } = await events();
    return [...bindings.values()].map(binding => {
      const state = hands.get(binding.playerId);
      const hand = state?.hand;
      return { ...binding, verified: true, playing: Boolean(hand),
        activeGame: hand ? hand.game || 'blackjack' : null };
    });
  }

  async function bind({ bandName, playerId }) {
    bandName = typeof bandName === 'string' ? bandName.trim() : '';
    playerId = Number(playerId);
    if (!bandName || bandName.length > 80 || /[\r\n]/.test(bandName) ||
        !Number.isSafeInteger(playerId)) {
      throw inputError('BAND 표시 이름과 RLCM 참가자를 확인해 주세요.');
    }
    if (!await core.loadPlayer(playerId)) throw inputError('RLCM 참가자를 찾을 수 없습니다.');
    const { bindings } = await events({ create: true });
    const current = bindings.get(bandName);
    if (current?.playerId === playerId) return;
    if (current || [...bindings.values()].some(binding => binding.playerId === playerId)) {
      throw inputError('이미 연결된 이름 또는 참가자입니다. 연결을 해제한 뒤 다시 등록하세요.');
    }
    await batch([append(gameSheetId, [randomUUID(), 'bind', playerId, bandName, DEFAULT_COLOR, '', '', new Date().toISOString()])]);
  }

  async function unlink(bandName) {
    const { bindings, hands } = await events();
    const binding = bindings.get(bandName);
    if (!binding) return;
    if (hands.get(binding.playerId)?.hand) {
      throw inputError('진행 중인 카드 게임을 마친 뒤 연결을 해제하세요.');
    }
    await batch([append(gameSheetId, [randomUUID(), 'unlink', binding.playerId, bandName, '', '', '', new Date().toISOString()])]);
  }

  function blackjackReply(hand) {
    const state = core.getBlackjackState(hand);
    const result = state.done
      ? `딜러 ${state.dealerCards.join(', ')} (${state.dealerValue})\n${state.result} · ${state.resultChipText}`
      : `딜러 ${state.openCard}, ?\n히트 또는 스탠드를 입력해 주세요.`;
    return `블랙잭 · 내 패 ${state.playerCards.join(', ')} (${state.playerValue})\n${result}`;
  }

  function cardReply(hand) {
    if (hand.game !== 'baccarat') return blackjackReply(hand);
    const state = core.getBaccaratState(hand);
    const cards = `PLAYER ${state.playerCards.join(', ')} (${state.playerTotal})\nBANKER ${state.bankerCards.join(', ')} (${state.bankerTotal})`;
    const verdict = state.push ? '베팅금 반환' : state.win ? '승리' : '패배';
    const result = state.done
      ? `${state.outcome} · ${verdict} · ${state.resultChipText}`
      : '히트 또는 스탠드를 입력해 주세요.';
    return `바카라 · ${state.side} 베팅\n${cards}\n${result}`;
  }

  function advance(player, color, state, command) {
    const { game, amount, side, numbers, action } = command;
    if (action === 'hit' || action === 'stand') {
      const hand = state.hand;
      if (!hand) throw inputError('진행 중인 바카라나 블랙잭이 없습니다.');
      if (game && game !== (hand.game || 'blackjack')) {
        throw inputError('진행 중인 게임의 원댓글에서 히트 또는 스탠드를 입력해 주세요.');
      }
      if (hand.game === 'baccarat') {
        player[color] += hand.bet;
        core.applyBaccaratAction(player, hand, action);
        state.hand = null;
        return { reply: cardReply(hand), type: 'baccarat', log: true };
      }
      if (action === 'hit') hand.playerCards.push(core.drawBlackjackCard(hand.deck));
      if (action === 'stand' || core.getBlackjackState(hand).playerValue > 21) {
        // finishBlackjackSession expects a balance that still includes the original bet.
        player[color] += hand.bet;
        core.finishBlackjackSession(player, hand);
        state.hand = null;
      }
      return { reply: blackjackReply(hand), type: 'blackjack', log: hand.done };
    }
    if (!Object.hasOwn(GAME_NAMES, game) || !Number.isSafeInteger(amount) || amount < 0) {
      throw inputError(HELP);
    }
    if (!core.validateColor(color)) throw inputError('칩 색이 올바르지 않습니다.');
    const balance = player[color];
    if (balance < amount) throw inputError('보유 칩이 부족합니다.');
    if (game === 'roulette') {
      const { result, delta } = settleRoulette(amount, numbers, drawRouletteNumber);
      player[color] += delta;
      const settlement = core.makeChipResultText(color, delta, player[color]);
      return { reply: `룰렛 · 당첨 번호 ${result}\n${settlement}`, type: game, log: true };
    }
    if (state.hand) throw inputError('진행 중인 카드 게임을 먼저 마쳐 주세요.');
    if (game === 'baccarat') {
      const { session } = core.startBaccaratHand(player, color, amount, side);
      session.game = 'baccarat';
      if (session.done) return { reply: cardReply(session), type: game, log: true };
      state.hand = session;
    } else {
      const deck = core.createBlackjackDeck();
      const draw = () => core.drawBlackjackCard(deck);
      state.hand = {
        playerId: player.id, color, bet: amount, deck,
        playerCards: [draw(), draw()], dealerCards: [draw(), draw()]
      };
    }
    player[color] -= amount;
    return { reply: `${cardReply(state.hand)}\n${color} ${amount}칩 베팅 · 가용 잔액 ${player[color]}` };
  }

  async function convert(player, { fromColor, toColor, amount }) {
    const received = await core.convertChips(player, fromColor, toColor, amount);
    const from = core.getChipLabel(fromColor);
    const to = core.getChipLabel(toColor);
    return {
      reply: `환전 완료 · ${from} ${amount} → ${to} ${received}\n잔액 · ${from} ${player[fromColor]} / ${to} ${player[toColor]}`,
      type: 'convert',
      log: true,
      publicText: core.makeConvertPublicLog(player, fromColor, toColor, amount, received)
    };
  }

  async function handle({ key, author, command }) {
    if (!/^[a-f0-9]{64}$/.test(key) || typeof author !== 'string' || !command || typeof command !== 'object') {
      throw inputError('BAND 요청이 올바르지 않습니다.');
    }
    const { bindings, hands, replies } = await events();
    if (replies.has(key)) return { reply: replies.get(key) };
    const binding = bindings.get(author);
    if (!binding) return { reply: '운영자에게 BAND 표시 이름과 RLCM 참가자 연결을 요청해 주세요.' };
    const { playerId, color: defaultColor } = binding;
    const state = hands.get(playerId);
    const current = await core.loadPlayer(playerId);
    if (!current) return { reply: '연결된 RLCM 참가자를 찾을 수 없습니다.' };
    const player = { ...current };
    const identity = `${player.name} · ${defaultColor} 잔액 ${player[defaultColor]}`;
    const next = state || { hand: null, lastReply: '' };
    if (command.action === 'status') {
      return { reply: next.hand ? cardReply(next.hand) : next.lastReply || identity };
    }
    if (command.action === 'verify' && state) return { reply: identity };
    const turn = command.action === 'hit' || command.action === 'stand';
    const wagerColor = command.action ? defaultColor : command.color ?? defaultColor;
    const color = turn ? next.hand?.color ?? defaultColor : wagerColor;
    let result;
    try {
      if (command.action === 'verify') {
        result = { reply: `${identity}\n${HELP}` };
      } else if (command.action === 'convert') {
        result = await convert(player, command);
      } else {
        result = advance(player, color, next, command);
      }
    } catch (error) {
      if (error.status === 400) return { reply: error.message };
      throw error;
    }
    const changedColors = core.colors.filter(chip => player[chip] !== current[chip]);
    if (changedColors.some(chip => !Number.isSafeInteger(player[chip]) || player[chip] < 0)) {
      return { reply: '정산 가능한 칩 범위를 초과했습니다.' };
    }
    if (result.log) next.lastReply = result.reply;
    const createdAt = new Date().toISOString();
    const marker = `band_${key}`;
    const requests = [
      { addNamedRange: { namedRange: {
        namedRangeId: marker, name: marker, range: {
          sheetId: gameSheetId, startRowIndex: 0, endRowIndex: 1,
          startColumnIndex: 0, endColumnIndex: 1
        }
      } } },
      append(gameSheetId, [key, 'command', playerId, author, color, JSON.stringify(next), result.reply, createdAt])
    ];
    if (changedColors.length || result.log) {
      if (result.log) await core.ensureLogSheetHeader();
      const ids = await sheetIds();
      for (const chip of changedColors) {
        requests.push({ updateCells: {
          start: { sheetId: ids.get(core.playerSheet), rowIndex: current._rowNumber - 1,
            columnIndex: 3 + core.colors.indexOf(chip) },
          rows: [{ values: [cell(player[chip])] }], fields: 'userEnteredValue'
        } });
      }
      if (result.log) {
        const text = `BAND · ${player.name}\n${result.reply}`;
        requests.push(append(ids.get(core.logSheet),
          [Date.now(), result.type, text, result.publicText || text, createdAt, marker]));
      }
    }
    try {
      await batch(requests);
    } finally {
      // A lost response can still mean that Sheets committed the change.
      if (changedColors.length) core.invalidatePlayersCache();
    }
    return { reply: result.reply };
  }

  return { list, bind, unlink, handle };
}

module.exports = { createBandGames };
