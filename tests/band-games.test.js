const assert = require('node:assert/strict');
const { test } = require('node:test');
const { createBandGames } = require('../lib/band-games');
const { createApiApp } = require('../lib/rlcm');
const { payoutChips, rouletteNumbers, settleRoulette } = require('../lib/dealer-rules');

function fixture() {
  const rows = [];
  const logs = [];
  const markers = new Set();
  const people = [
    { id: 1, name: 'A', red: 20, blue: 10, green: 2, _rowNumber: 2 },
    { id: 2, name: 'B', red: 40, blue: 10, green: 2, _rowNumber: 3 }
  ];
  let gameSheet;
  let loseResponse = false;
  let rejectWrite = false;
  const sheets = {
    spreadsheets: {
      async get() {
        const properties = [{ title: '플레이어', sheetId: 1 }, { title: '로그', sheetId: 2 },
          { title: '칩가치', sheetId: 3 }];
        if (gameSheet !== undefined) properties.push({ title: 'BAND 게임', sheetId: gameSheet });
        return { data: { sheets: properties.map(item => ({ properties: item })) } };
      },
      async batchUpdate({ requestBody }) {
        if (rejectWrite) {
          rejectWrite = false;
          throw new Error('batch rejected');
        }
        for (const request of requestBody.requests) {
          const key = request.addNamedRange?.namedRange.namedRangeId;
          if (key) {
            assert(!markers.has(key), 'a comment must be committed at most once');
            markers.add(key);
          }
          if (request.addSheet) gameSheet = request.addSheet.properties.sheetId;
          if (request.appendCells) {
            const target = request.appendCells.sheetId === gameSheet ? rows : logs;
            target.push(request.appendCells.rows[0].values.map(value => Object.values(value.userEnteredValue)[0]));
          }
          if (request.updateCells) {
            const { start, rows: updates } = request.updateCells;
            const chip = ['red', 'blue', 'green', 'yellow', 'white'][start.columnIndex - 3];
            assert(chip, 'the write targets a chip balance column');
            people[start.rowIndex - 1][chip] = updates[0].values[0].userEnteredValue.numberValue;
          }
        }
        if (loseResponse) {
          loseResponse = false;
          throw new Error('response lost');
        }
      },
      values: {
        async get({ range }) {
          if (range === "'칩가치'!A1:B1") return { data: { values: [['color', 'value']] } };
          if (range === "'칩가치'!A:B") {
            return { data: { values: [['color', 'value'], ['red', 1.5], ['blue', 3], ['green', 5]] } };
          }
          assert.equal(range, "'BAND 게임'!A:H");
          return { data: { values: structuredClone(rows) } };
        }
      }
    }
  };
  const base = createApiApp({ sheets }).bandContext;
  const core = {
    ...base,
    async loadPlayer(id) { return people.find(player => player.id === id); },
    async ensureLogSheetHeader() {}
  };
  const slot = index => index.toString(16).padStart(64, '0');
  const command = (games, index, author, input) => games.handle({ key: slot(index), author, command: input });
  return { people, logs, games: createBandGames(core, () => 17), reopen: () => createBandGames(core, () => 0), command,
    loseNextResponse: () => { loseResponse = true; },
    rejectNextWrite: () => { rejectWrite = true; } };
}

test('two players can interleave blackjack turns and settle independently after reopening', async () => {
  const { games, reopen, people, command, logs } = fixture();
  await games.bind({ bandName: '가', playerId: 1, color: 'red' });
  await games.bind({ bandName: '나', playerId: 2, color: 'red' });
  assert.match((await command(games, 1, '가', { game: 'blackjack', amount: 10 })).reply, /먼저 확인/);
  await command(games, 2, '가', { action: 'verify' });
  await command(games, 3, '나', { action: 'verify' });
  const first = await command(games, 4, '가', { game: 'blackjack', amount: 10 });
  assert.match(first.reply, /딜러 .+, \?/);
  await command(games, 5, '나', { game: 'blackjack', amount: 15 });
  await assert.rejects(games.unlink('나'), /진행 중/);
  assert.deepEqual(people.map(player => player.red), [10, 25]);
  const restored = reopen();
  await command(restored, 6, '나', { action: 'stand' });
  assert.deepEqual((await restored.list()).map(binding => binding.playing), [true, false]);
  assert.deepEqual(await command(restored, 4, '가', { game: 'blackjack', amount: 10 }), first);
  assert.equal(people[0].red, 10);
  await command(restored, 7, '가', { action: 'hit' });
  if ((await restored.list())[0].playing) await command(restored, 8, '가', { action: 'stand' });
  assert.deepEqual((await restored.list()).map(binding => binding.playing), [false, false]);
  assert.equal(logs.length, 2);
  assert.match((await command(restored, 9, '가', { action: 'status' })).reply, /현재 잔고/);
});

test('duplicate roulette after a lost response returns original result and applies coins once', async () => {
  const { games, reopen, people, command, logs, loseNextResponse, rejectNextWrite } = fixture();
  await games.bind({ bandName: '가', playerId: 1, color: 'red' });
  await command(games, 1, '가', { action: 'verify' });
  const bet = { game: 'roulette', amount: 10, numbers: [17], profit: 450 };
  rejectNextWrite();
  await assert.rejects(command(games, 2, '가', bet), /batch rejected/);
  assert.equal(people[0].red, 20);
  assert.equal(logs.length, 0);
  loseNextResponse();
  await assert.rejects(command(games, 2, '가', bet), /response lost/);
  const restored = reopen();
  const duplicate = await command(restored, 2, '가', bet);
  assert.match(duplicate.reply, /당첨 번호 17/);
  assert.equal(people[0].red, 420);
  assert.equal(logs.length, 1);
  const insufficient = await command(games, 3, '가', { game: 'baccarat', amount: 421, side: 'PLAYER' });
  assert.match(insufficient.reply, /보유 칩이 부족/);
});

test('BAND conversion uses current web rates and transfers both balances once after a lost response', async () => {
  const { games, reopen, people, command, logs, loseNextResponse, rejectNextWrite } = fixture();
  await games.bind({ bandName: '가', playerId: 1, color: 'blue' });
  const exchange = { action: 'convert', fromColor: 'red', toColor: 'green', amount: 5 };
  assert.match((await command(games, 1, '가', exchange)).reply, /먼저 확인/);
  await command(games, 2, '가', { action: 'verify' });
  for (const [index, invalid, error] of [
    [3, { amount: 0 }, /1 이상/],
    [4, { toColor: 'red' }, /같은 색/],
    [5, { amount: 21 }, /보유 칩/],
    [6, { amount: 1 }, /결과가 0/]
  ]) {
    assert.match((await command(games, index, '가', { ...exchange, ...invalid })).reply, error);
  }
  const balances = () => [people[0].red, people[0].green, people[0].blue];
  assert.deepEqual(balances(), [20, 2, 10]);
  rejectNextWrite();
  await assert.rejects(command(games, 7, '가', exchange), /batch rejected/);
  assert.deepEqual(balances(), [20, 2, 10]);
  loseNextResponse();
  await assert.rejects(command(games, 7, '가', exchange), /response lost/);
  const response = await command(reopen(), 7, '가', exchange);
  assert.match(response.reply, /Red 5 → Green 1/);
  assert.deepEqual(balances(), [15, 3, 10]);
  assert.equal(logs.length, 1);
  assert.equal(logs[0][1], 'convert');
  assert.doesNotMatch(logs[0][3], /잔액/);
});

test('roulette accepts up to four distinct numbers and uses exact truncated payouts', () => {
  const numbers = [0, 5, 8, 17];
  for (const [index, payout] of [40, 20, 13, 9].entries()) {
    const selection = rouletteNumbers('number', numbers.slice(0, index + 1).join(', '));
    assert.equal(settleRoulette(1, selection, () => 0).delta, payout);
  }
  assert.equal(settleRoulette(3, rouletteNumbers('dozen', '2'), () => 13).delta, 6);
  assert.equal(settleRoulette(3, rouletteNumbers('column', '3'), () => 36).delta, 6);
  assert.equal(settleRoulette(4, rouletteNumbers('red'), () => 7).delta, 4);
  assert.equal(settleRoulette(4, rouletteNumbers('even'), () => 0).delta, -4);
  assert.equal(settleRoulette(0, [17], () => 17).delta, 0);
  assert.throws(() => rouletteNumbers('number', '1, 2, 3, 4, 5'), /1~4개/);
  assert.throws(() => rouletteNumbers('number', '00'), /0~36/);
  assert.throws(() => settleRoulette(1, [5, 5], () => 5), /중복 없이/);
  assert.equal(payoutChips(7, 1.2), 8);
});

test('BAND baccarat stays pending for 0–7 and pays once when the player makes a choice', async t => {
  t.mock.method(Math, 'random', () => 0);
  const { games, reopen, people, command, logs } = fixture();
  await games.bind({ bandName: '가', playerId: 1, color: 'red' });
  await command(games, 1, '가', { action: 'verify' });
  const start = await command(games, 2, '가', { game: 'baccarat', amount: 7, side: 'PLAYER' });
  assert.match(start.reply, /PLAYER.*\(3\)[\s\S]+BANKER.*\(7\)[\s\S]+히트 또는 스탠드/);
  assert.equal((await games.list())[0].activeGame, 'baccarat');
  assert.equal(people[0].red, 13);
  const restored = reopen();
  const wrongThread = await command(restored, 5, '가', { action: 'hit', game: 'blackjack' });
  assert.match(wrongThread.reply, /진행 중인 게임의 원댓글/);
  assert.equal(people[0].red, 13);
  const finished = await command(restored, 3, '가', { action: 'hit' });
  assert.match(finished.reply, /PLAYER.*\(8\)[\s\S]+승리 · Red \+8/);
  assert.equal(people[0].red, 28);
  assert.equal((await restored.list())[0].playing, false);
  assert.deepEqual(await command(restored, 3, '가', { action: 'hit' }), finished);
  assert.equal(logs.length, 1);
  const free = await command(restored, 4, '가', { game: 'blackjack', amount: 0 });
  assert.match(free.reply, /0칩 베팅/);
});

test('an explicitly chosen chip color stays with the wager through card settlement', async t => {
  t.mock.method(Math, 'random', () => 0);
  const { games, reopen, command, people } = fixture();
  await games.bind({ bandName: '가', playerId: 1, color: 'red' });
  await command(games, 1, '가', { action: 'verify' });
  const rejected = await command(games, 2, '가', { game: 'blackjack', color: 'black', amount: 5 });
  assert.match(rejected.reply, /칩 색이 올바르지/);
  const opened = await command(games, 3, '가', { game: 'blackjack', color: 'blue', amount: 5 });
  assert.match(opened.reply, /blue 5칩 베팅/);
  assert.deepEqual([people[0].red, people[0].blue], [20, 5]);
  const finished = await command(reopen(), 4, '가', { action: 'stand', game: 'blackjack' });
  assert.match(finished.reply, /Blue -5/);
  assert.deepEqual([people[0].red, people[0].blue], [20, 5]);
});

test('baccarat uses the first two cards for naturals and returns a tied PLAYER stake', t => {
  let random = () => 0;
  t.mock.method(Math, 'random', () => random());
  const core = createApiApp({ sheets: {} }).bandContext;
  const player = { id: 1, red: 30 };
  const hand = { playerId: 1, side: 'PLAYER', color: 'red', bet: 7,
    playerCards: ['1S-3', '1H-3'], bankerCards: ['1S-A', '1H-2'],
    shoe: ['1S-2', '1S-5'] };
  assert.match(core.applyBaccaratAction(player, hand, 'hit'), /무승부/);
  assert.equal(hand.bankerCards.length, 3);
  assert.equal(player.red, 30);
  const zero = { playerId: 1, side: 'TIE', color: 'red', bet: 1,
    playerCards: ['1S-10', '1H-K'], bankerCards: ['1S-A', '1H-2'],
    shoe: ['1S-7', '1S-4'] };
  assert.equal(core.applyBaccaratAction(player, zero, 'hit'), '승리');
  assert.equal(player.red, 40);
  let draw = 0;
  random = () => {
    const index = draw++ === 0 ? 6 : 0;
    return (index + .5) / (417 - draw);
  };
  const { session } = core.startBaccaratHand(player, 'red', 2, 'BANKER');
  assert.equal(session.playerAction, 'natural');
  assert.equal(session.done, true);
  assert.equal(draw, 4);
  assert.equal(player.red, 38);
});
