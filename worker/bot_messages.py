"""베팅 호출·봇 출력 판별과 자동 멘션이 포함된 답글 비교."""

import re


COMMAND_MENTION = "@**ALEA**"
_CALLED = re.compile(r"(?<![\w@/])(?:@\*\*ALEA\*\*|\*\*@ALEA\*\*|@ALEA)(?![\w*])", re.IGNORECASE)
_BARE_MARKER = re.compile(r"\s*(?:\*\*ALEA\*\*|ALEA)(?![\w*])", re.IGNORECASE)
_BET_DATA = re.compile(r"[\d/+-]")
THREAD_TOPICS = {"룰렛": "roulette", "바카라": "baccarat", "블랙잭": "blackjack", "환전": "convert"}


def thread_topic(root):
    """로그인한 ALEA 계정이 작성한 원댓글의 첫 줄로 종목을 확인한다."""
    if not root or not root["is_self"] or len(root["author"]) != 1:
        return None
    if root["author"][0].strip().strip("*").casefold() != "alea":
        return None
    lines = root["content"].strip().splitlines()
    return THREAD_TOPICS.get(lines[0].strip()) if lines else None


def bet_command_text(value, *, is_reply=False):
    """호출이 있는 본문을 반환한다. 답글 앞의 멘션은 분석에서만 제외한다."""
    if not isinstance(value, str):
        return None
    match = _CALLED.search(value)
    if match is None:
        return None
    if not is_reply:
        return value

    # 별도 줄의 자동 멘션을 먼저 제거한다. 호출 앞에 있는 베팅 내용은 보존한다.
    before, after = value[:match.start()], value[match.start():]
    lines = before.lstrip().splitlines(keepends=True)
    while (lines and lines[0].endswith(("\r", "\n"))
           and _is_mention_prefix(lines[0]) and not _BET_DATA.search(lines[0])):
        lines.pop(0)
    prefix = "".join(lines)
    if _is_mention_prefix(prefix):
        if not _BET_DATA.search(prefix):
            prefix = ""
        else:
            prefix = re.sub(r"^(?:@\S+\s+)+", "", prefix)
    # BAND가 원댓글 작성자의 멘션을 이미 붙였으면, 참가자가 직접 입력한 호출을 사용한다.
    second = _CALLED.match(after[match.end() - match.start():].lstrip())
    if second and not prefix.strip():
        return after[match.end() - match.start():].lstrip()
    return prefix + after if before.strip() else value


def bet_body_candidates(value):
    """호출을 뺀 본문과, 앞에 인사말이 붙은 경우 호출 뒤의 본문을 반환한다."""
    match = _CALLED.search(value) or _BARE_MARKER.match(value)
    if match is None:
        return None, None
    before, after = value[:match.start()].strip(), value[match.end():].strip()
    if _CALLED.search(after):
        raise ValueError("한 댓글에는 베팅 호출을 한 번만 입력해 주세요.")

    def strip_period(text):
        return text.rstrip(".。．").rstrip()

    body = " ".join(part for part in (before, after) if part)
    # 호출 앞쪽의 숫자, 슬래시, 부호는 베팅의 일부일 수 있으므로 버리지 않는다.
    fallback = strip_period(after) if before and after and not _BET_DATA.search(before) else None
    return strip_period(body), fallback


def is_bet_command(value, *, is_reply=False):
    return bet_command_text(value, is_reply=is_reply) is not None


def normalized_text(value):
    return "\n".join(line.rstrip() for line in value.replace("\r\n", "\n").split("\n")).strip()


def _is_mention_prefix(value):
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    return bool(lines) and all(line.startswith("@") for line in lines)


def has_target_mention(value, author):
    if not isinstance(value, str) or not isinstance(author, str) or not author.strip():
        return False
    mention = '@' + ' '.join(author.split())
    mentions = ' '.join(value.split())
    return _is_mention_prefix(value) and (mentions == mention or mentions.endswith(' ' + mention))


def matches_reply(value, target, body):
    """자동 멘션과 표시용 줄바꿈이 추가돼도 대상에게 보낸 같은 답글을 찾는다."""
    if not isinstance(value, str) or not isinstance(body, str) or not body.strip():
        return False
    pattern = r"\s+".join(re.escape(word) for word in body.split())
    match = re.fullmatch(r"(?P<prefix>.*?)" + pattern, normalized_text(value), re.DOTALL)
    if match is None:
        return False
    prefix = match["prefix"]
    if not prefix:
        return True
    if not prefix[-1].isspace() or not _is_mention_prefix(prefix):
        return False
    author = target.get("author")
    if not isinstance(author, str) or not author.strip():
        return False
    # 원댓글 작성자 등의 자동 멘션 뒤에도 직접 선택한 베팅 작성자가 있어야 한다.
    return has_target_mention(prefix, author)


def is_bot_message(value):
    """수정 메뉴가 아직 없어도 결과/입력 오류를 새 베팅으로 분석하지 않는다."""
    if not isinstance(value, str):
        return False
    text = normalized_text(value)
    for start in re.finditer(r"(?<!\S)(?:당첨 번호:|주사위:|입력 오류:|참여 제한:)", text):
        prefix = text[:start.start()]
        if prefix:
            if not _is_mention_prefix(prefix):
                continue
            # 베팅 명령 뒤에 결과/오류 문구를 덧붙인 입력을 봇 출력으로 오인하지 않는다.
            if _CALLED.search(prefix):
                continue
        body = " ".join(text[start.start():].split())
        if re.fullmatch(
            r"당첨 번호: (?:[0-9]|[12][0-9]|3[0-6]) 결과: [+-]\d+(?:\.\d+)? C"
            r"|주사위: [1-6] \+ [1-6] \+ [1-6] = (?:[3-9]|1[0-8]) 결과: [+-]\d+ C"
            r"|입력 오류: .+|참여 제한: DICE!는 러닝 중 한 번만 참여할 수 있습니다\.", body,
        ):
            return True
    return False
