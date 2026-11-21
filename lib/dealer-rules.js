const { randomInt } = require('node:crypto');

const RED = new Set([1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36]);
const BETWEEN = Array.from({ length: 36 }, (_, index) => index + 1);
const OUTSIDE = {
  red: [...RED],
  black: BETWEEN.filter(number => !RED.has(number)),
  even: BETWEEN.filter(number => number % 2 === 0),
  odd: BETWEEN.filter(number => number % 2 !== 0),
  low: BETWEEN.slice(0, 18),
  high: BETWEEN.slice(18)
};
const DOZENS = [0, 1, 2].map(index => BETWEEN.slice(index * 12, (index + 1) * 12));
const COLUMNS = [1, 2, 3].map(index => BETWEEN.filter(number => (number - index) % 3 === 0));
const NUMBER_RATES = [0, 40, 20, 13, 9];
const NUMBER_TYPES = { number: 0, straight: 1, split: 2, street: 3, corner: 4 };

function invalidBet(message) {
  return Object.assign(new Error(message), { status: 400 });
}

function payoutChips(amount, rate) {
  if (!Number.isSafeInteger(amount) || amount < 0) {
    throw invalidBet('베팅 수량은 0 이상의 정수여야 합니다.');
  }
  const profit = Number(BigInt(amount) * BigInt(Math.round(rate * 10)) / 10n);
  if (!Number.isSafeInteger(profit)) throw invalidBet('정산 가능한 칩 범위를 초과했습니다.');
  return profit;
}

function rouletteNumbers(pick, detail) {
  const type = String(pick ?? '').toLowerCase();
  const selection = String(detail ?? '').trim();
  if (Object.hasOwn(NUMBER_TYPES, type)) {
    if (!/^(?:0|[1-9][0-9]?)(?:(?:\s*,\s*|\s+)(?:0|[1-9][0-9]?))*$/.test(selection)) {
      throw invalidBet('0~36의 번호를 쉼표나 공백으로 구분해 주세요.');
    }
    const numbers = selection.split(/[\s,]+/).map(Number);
    if (numbers.length > 4) throw invalidBet('룰렛 번호는 1~4개를 선택해 주세요.');
    if (NUMBER_TYPES[type] && NUMBER_TYPES[type] !== numbers.length) {
      throw invalidBet('룰렛 유형에 맞는 개수의 번호를 선택해 주세요.');
    }
    return numbers;
  }
  if (type === 'dozen' || type === 'column') {
    const ordinal = /^([1-3])(?:st|nd|rd)?$/i.exec(selection)?.[1];
    if (!ordinal) throw invalidBet('1, 2, 3 중 하나를 선택해 주세요.');
    return (type === 'dozen' ? DOZENS : COLUMNS)[Number(ordinal) - 1];
  }
  const outside = { '1-18': 'low', '19-36': 'high', small: 'low', big: 'high' }[type] || type;
  if (Object.hasOwn(OUTSIDE, outside)) return OUTSIDE[outside];
  throw invalidBet('룰렛 베팅 유형을 확인해 주세요.');
}

function settleRoulette(amount, numbers, draw = () => randomInt(37)) {
  if (!Array.isArray(numbers) || !numbers.length || new Set(numbers).size !== numbers.length ||
      numbers.some(number => !Number.isInteger(number) || number < 0 || number > 36)) {
    throw invalidBet('룰렛 번호는 0~36에서 중복 없이 선택해 주세요.');
  }
  const sameSelection = allowed => allowed.length === numbers.length && numbers.every(number => allowed.includes(number));
  let payout = NUMBER_RATES[numbers.length];
  if (!payout && [...DOZENS, ...COLUMNS].some(sameSelection)) payout = 2.2;
  if (!payout && Object.values(OUTSIDE).some(sameSelection)) payout = 1.2;
  if (!payout) throw invalidBet('번호는 1~4개까지, 그 외에는 한 가지 룰렛 옵션을 선택해 주세요.');
  const result = draw();
  const win = numbers.includes(result);
  return { result, win, payout, delta: win ? payoutChips(amount, payout) : -amount };
}

module.exports = { payoutChips, rouletteNumbers, settleRoulette };
