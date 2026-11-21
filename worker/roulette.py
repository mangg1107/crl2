"""베팅 검증, 유럽식 단일 0 룰렛 추첨 및 사용자 지정 순이익 계산."""

import re

from bot_messages import COMMAND_MENTION, bet_body_candidates

RED = frozenset({
    1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36,
})
BLACK = frozenset(range(1, 37)) - RED
OPTIONS = {
    "dozen": {"1st", "2nd", "3rd"},
    "column": {"1st", "2nd", "3rd"},
    "color": {"red", "black"},
    "parity": {"even", "odd"},
    "size": {"big", "small"},
}
_QUANTITY = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)"
_CHIP_AMOUNT_PATTERN = (
    r"(?<!\S)(?:칩\s*(?P<prefix>{quantity})\s*(?:개)?|"
    r"(?P<suffix>{quantity})\s*(?:칩\s*(?:개)?|개|C)?)\s*"
)
_CHIP_AMOUNT = re.compile(_CHIP_AMOUNT_PATTERN.format(quantity=_QUANTITY) + r"$", re.IGNORECASE)
# 부호나 소수점이 있어도 구분자는 찾아서 일반 베팅과 같은 수량 검증을 적용한다.
_COMMA_AMOUNT = re.compile(
    _CHIP_AMOUNT_PATTERN.format(quantity=rf"[+-]?{_QUANTITY}(?:\.[0-9]+)?"), re.IGNORECASE,
)
_TARGET_BOUNDARY = re.compile(r"\s+|[,.!?。．]+|(?:으로|로|에)(?=\s|$)")


def _normalize_comma_separator(body):
    if "/" in body:
        return body
    # 천 단위까지 포함한 첫 수량을 읽고, 바로 뒤의 쉼표만 구분자로 쓴다.
    amount = _COMMA_AMOUNT.search(body)
    if amount and body[amount.end():].startswith(",") and not re.search(r"[\d+-]", body[:amount.start()]):
        end = amount.end()
        return body[:end] + "/" + body[end + 1:]
    return body


def parse_standard_bet(content):
    """기본 호출 양식은 직접 해석한다. 다른 자연어 입력은 None을 반환한다."""
    body, fallback = bet_body_candidates(content)
    if body is None:
        return None
    body = _normalize_comma_separator(body)
    if fallback is not None:
        fallback = _normalize_comma_separator(fallback)
    error = None
    for candidate in (body, fallback):
        if candidate is None:
            continue
        try:
            result = _parse_standard_body(candidate)
        except ValueError as exc:
            error = exc
        else:
            if result is not None:
                return result
    if error is not None and not _has_slash_bet_in_prose(body):
        raise error
    return None


def _has_slash_bet_in_prose(body):
    """문장 속 기본 베팅을 찾아 전체 댓글을 자연어 분석에 보낼지 판별한다."""
    if body.count("/") != 1:
        return False
    left, right = (part.strip() for part in body.split("/"))
    amount = _CHIP_AMOUNT.search(left)
    if amount is None:
        return False
    opening = left[:amount.start()].strip()
    if opening and (not any(char.isalpha() for char in opening) or re.search(r"[\d+-]", opening)):
        return False

    # 긴 대상부터 시도해 여러 번호나 여러 단어로 된 옵션을 온전히 검사한다.
    ends = [match.start() for match in _TARGET_BOUNDARY.finditer(right)]
    ends.append(len(right))
    for end in reversed(ends):
        try:
            _parse_standard_body(f"{amount[0]} / {right[:end]}")
        except ValueError:
            continue
        closing = right[end:].strip(" \t\r\n,.!?。．")
        if not closing:
            return bool(opening)
        if not any(char.isalpha() for char in closing) or closing[0].isdigit():
            return False
        try:
            _parse_standard_body(f"1 / {closing}")
        except ValueError:
            return True
        # 뒤쪽이 그 자체로 또 다른 베팅 대상이면 일반 문장으로 보지 않는다.
        return False
    return False


def _parse_standard_body(body):
    if "/" not in body:
        # 단위가 없어도 옵션/번호 표기와 수량이 구별되면 로컬에서 처리한다.
        # '10 17'처럼 숫자만 나열된 입력은 어느 쪽이 수량인지 추측하지 않는다.
        candidates = []
        for pattern in (rf"\s*(?P<amount>{_QUANTITY})\s+(?P<target>.+?)\s*",
                        rf"\s*(?P<target>.+?)\s+(?P<amount>{_QUANTITY})\s*"):
            match = re.fullmatch(pattern, body, re.DOTALL)
            if match is None or not re.search(r"[^0-9,\s]", match["target"]):
                continue
            try:
                value = _parse_standard_body(f"{match['amount']} / {match['target']}")
            except ValueError:
                continue
            if value not in candidates:
                candidates.append(value)
        return candidates[0] if len(candidates) == 1 else None
    parts = body.split("/")
    if len(parts) != 2:
        raise ValueError(f"{COMMAND_MENTION} 룰렛 칩 개수 / 걸 곳 형식으로 입력해 주세요.")
    amount = _CHIP_AMOUNT.fullmatch(parts[0].strip())
    if amount is None:
        raise ValueError("베팅 수량은 0 이상의 정수로 입력해 주세요.")
    chips = int((amount["prefix"] or amount["suffix"]).replace(",", ""))
    target = parts[1].strip()
    value = {"bet_type": "number", "bet_option": None,
             "numbers": [], "chip_count": chips}
    number = r"(?:0|[1-9][0-9]*)(?:\s*번)?"
    if re.fullmatch(rf"{number}(?:(?:\s*,\s*|\s+){number})*", target):
        value["numbers"] = [int(n) for n in re.findall(r"[0-9]+", target)]
    else:
        compact = re.sub(r"\s+", "", target).casefold()
        aliases = {
            ("color", "red"): ("빨강", "빨간색", "레드", "red"),
            ("color", "black"): ("검정", "검은색", "블랙", "black"),
            ("parity", "even"): ("짝수", "짝", "이븐", "even"),
            ("parity", "odd"): ("홀수", "홀", "오드", "odd"),
            ("size", "small"): ("스몰", "작은수", "small"),
            ("size", "big"): ("빅", "큰수", "big"),
        }
        for ordinal, korean, short, english in zip(
            ("1st", "2nd", "3rd"), ("첫번째", "두번째", "세번째"),
            ("첫째", "둘째", "셋째"), ("first", "second", "third"),
        ):
            digit = ordinal[0]
            prefixes = (korean, short, digit, digit + "번", ordinal, english)
            # 프롬프트에서 허용한 한영 혼합 표현도 로컬 경로에서 인식한다.
            for kind, suffixes in (("dozen", ("더즌", "도즌", "dozen")),
                                   ("column", ("열", "컬럼", "칼럼", "column"))):
                aliases[(kind, ordinal)] = tuple(
                    prefix + suffix for prefix in prefixes for suffix in suffixes
                )
        for (kind, option), names in aliases.items():
            if compact in names:
                value.update(bet_type=kind, bet_option=option)
                break
        else:
            raise ValueError("걸 곳은 숫자 1~4개 또는 한 가지 룰렛 옵션으로 입력해 주세요.")
    return validate_bet(value)


def validate_bet(bet: dict) -> dict:
    """AI 응답도 신뢰하지 않고 필드·자료형·허용 범위를 검사한다."""
    if not isinstance(bet, dict) or set(bet) != {
        "bet_type", "bet_option", "numbers", "chip_count",
    }:
        raise ValueError("베팅 형식이 올바르지 않습니다.")
    kind, option = bet["bet_type"], bet["bet_option"]
    numbers, chips = bet["numbers"], bet["chip_count"]
    if kind == "invalid":
        raise ValueError(f"베팅 내용을 해석하지 못했습니다. {COMMAND_MENTION} 룰렛 칩 개수 / 걸 곳 형식으로 입력해 주세요.")
    if type(chips) is not int or chips < 0:
        raise ValueError("베팅 수량은 0 이상의 정수로 입력해 주세요.")
    if not isinstance(kind, str) or not isinstance(numbers, list):
        raise ValueError("베팅 형식이 올바르지 않습니다.")
    if kind == "number":
        if option is not None or not 1 <= len(numbers) <= 4:
            raise ValueError("Number는 숫자 1~4개만 선택할 수 있습니다.")
        if any(type(n) is not int or not 0 <= n <= 36 for n in numbers):
            raise ValueError("숫자는 0~36의 정수만 가능합니다.")
        if len(set(numbers)) != len(numbers):
            raise ValueError("같은 숫자를 중복 선택할 수 없습니다.")
    elif kind not in OPTIONS:
        raise ValueError("베팅 내용이 불명확하거나 허용되지 않습니다.")
    elif not isinstance(option, str) or option not in OPTIONS[kind] or numbers:
        raise ValueError("한 가지 베팅 유형과 한 가지 옵션만 선택해 주세요.")
    return bet


def covered_numbers(bet: dict) -> list[int]:
    """선택한 베팅이 포함하는 번호를 돌려준다."""
    validate_bet(bet)
    kind, option = bet["bet_type"], bet["bet_option"]
    if kind == "number":
        numbers = bet["numbers"]
    elif kind == "dozen":
        start = 1 + 12 * ("1st", "2nd", "3rd").index(option)
        numbers = range(start, start + 12)
    elif kind == "column":
        start = 1 + ("1st", "2nd", "3rd").index(option)
        numbers = range(start, 37, 3)
    elif kind == "color":
        numbers = RED if option == "red" else BLACK
    elif kind == "parity":
        numbers = range(2 if option == "even" else 1, 37, 2)
    else:
        numbers = range(19, 37) if option == "big" else range(1, 19)
    return sorted(numbers)


