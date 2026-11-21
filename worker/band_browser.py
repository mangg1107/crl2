"""BAND 사진 댓글 화면 조작. 공개/비공개 BAND API를 직접 호출하지 않는다."""

import hashlib
import json
import logging
import re
import uuid
from collections import Counter
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from bot_messages import has_target_mention, is_bot_message, matches_reply, normalized_text, thread_topic

LOG = logging.getLogger("band-roulette")

PHOTO_LIST = '._photoCommentListRegion:visible'
MAIN_INPUT = '.commentContainer._writeMain textarea.commentWrite:not(._replyInputRegion textarea):visible'
# 사진 댓글 목록과 사진 하단 댓글 수를 담는 가장 가까운 조상을 찾는다.
# 내부 View 이름 변경 및 display:contents인 레이아웃에도 대응한다.
# body/html까지 올라가 뒤쪽 피드의 입력창을 결합하지 않는다.
PHOTO_SCOPE = (
    'xpath=ancestor::*[not(self::html or self::body)'
    ' and .//a[contains(concat(" ", normalize-space(@class), " "), " _footerInfoComment ")]][1]'
)
ROW = '[data-viewname="DCommentView"]'
VISIBLE_ROW = ROW + ':visible'
REPLY_INPUT = ('textarea.commentWrite:is(._replyInputRegion textarea, '
               '.commentContainer._writeReply textarea):visible')
REPLY_BUTTON = re.compile(r"^답글\s*(?:쓰기|달기)?$")
REPLY_EXPAND = re.compile(r"^답글\s*(?:[\d,]+\s*개\s*)?(?:모두\s*)?보기$")

THREAD_LOOKUP = """
    const commentSelector = '[data-viewname="DCommentView"]';
    const isReply = el => !!el.parentElement?.closest(commentSelector + ', ._replyRegion');
    const visible = el => el.getClientRects().length &&
        !['hidden', 'collapse'].includes(getComputedStyle(el).visibility);
    const threadRoot = element => {
        for (let node = element.parentElement; node && !node.matches('._photoCommentListRegion'); node = node.parentElement) {
            if (node.matches(commentSelector) && !isReply(node)) return node;
            const roots = [...node.querySelectorAll(commentSelector)].filter(el => visible(el) && !isReply(el));
            if (roots.length === 1) return roots[0];
            if (roots.length > 1) break;
        }
        return null;
    };
"""

ROW_TIME = """row => {
    const times = [...row.querySelectorAll('time.time')]
        .filter(el => el.closest('[data-viewname="DCommentView"]') === row);
    return times.length === 1 ? times[0].getAttribute('title') : null;
}"""

# 화면에 렌더링된 요소만 읽는다. 중첩된 답글의 작성자/본문/수정 메뉴는 제외한다.
ROW_TEXT = "row => {\n    const readTime = " + ROW_TIME + ";\n" + THREAD_LOOKUP + """
    const read = row => {
    const own = selector => [...row.querySelectorAll(selector)].filter(
        el => el.closest('[data-viewname="DCommentView"]') === row);
    let names = [];
    // 전용 작성자 요소를 우선한다. 본문의 멘션/링크와 숨겨진 이름은 제외한다.
    for (const selector of ['[data-uiselector="authorNameButton"] .name',
        '.commentName, ._commentName, [data-uiselector="commentName"]',
        '[data-uiselector="name"], ._userName', '._name, .name']) {
        names = own(selector).filter(el => visible(el) &&
            !el.closest('p._commentContent, ._commentContent, time, ._writeReply, ._replyInputRegion'))
            .map(el => el.innerText.trim()).filter(Boolean);
        if (names.length) break;
    }
    const content = own('p._commentContent');
    return {author: [...new Set(names)],
        content: content.map(el => el.innerText).join('\\n').trim(),
        timestamp: readTime(row),
        is_reply: isReply(row),
        is_self: own('[data-uiselector="modifyButton"]').length > 0};
    };
    const data = read(row);
    const root = data.is_reply ? threadRoot(row) : null;
    // 같은 분·작성자·본문이어도 별도 DOM 댓글이면 별도 요청이다.
    // 전체 순번 대신 동일한 댓글끼리만 세므로 다른 댓글 추가/정렬에는 영향받지 않는다.
    let occurrence = 0;
    let replyOccurrence = 0;
    for (const other of row.closest('._photoCommentListRegion')?.querySelectorAll(
        '[data-viewname="DCommentView"]') || []) {
        if (other === row) break;
        if (!visible(other) || readTime(other) !== data.timestamp) continue;
        const previous = read(other);
        if (JSON.stringify(previous.author) === JSON.stringify(data.author)) {
            if (previous.content === data.content) occurrence++;
            // 자동 멘션이나 베팅 본문이 달라도 별도로 작성한 대댓글을 구분한다.
            if (data.is_reply && previous.is_reply) replyOccurrence++;
        }
    }
    return {...data, thread_root: root ? read(root) : null,
        occurrence, slot_occurrence: data.is_reply ? replyOccurrence : occurrence};
}"""

EXPANDER_ROOT = "(el) => {" + THREAD_LOOKUP + """
    const region = el.closest('._replyRegion');
    const root = region ? threadRoot(region) : null;
    return root ? (""" + ROW_TEXT + """)(root) : null;
}"""

ROW_SNAPSHOT = f"""(rows, {{times, indexes, expected, tokens}}) => {{
    const currentTimes = rows.map({ROW_TIME});
    if (currentTimes.length !== times.length || currentTimes.some((time, i) => time !== times[i]))
        return null;
    const snapshot = indexes.map(index => ({ROW_TEXT})(rows[index]));
    if (expected !== undefined) {{
        if (JSON.stringify(snapshot) !== JSON.stringify(expected)) return null;
        for (const [index, token] of tokens) rows[index].setAttribute('data-roulette-target', token);
    }}
    return snapshot;
}}"""


class BandError(Exception):
    """새로고침/재시도로 복구할 수 있는 화면 또는 연결 오류."""


class BandAuthorUnavailable(BandError):
    """댓글 하나의 작성자를 확인하지 못한 상태."""


class BandSendUncertain(BandError):
    """전송 클릭이 시도되어 재전송 없이 화면 확인만 해야 하는 상태."""


class BandTargetUnavailable(BandError):
    """대상 댓글을 화면에서 다시 찾지 못한 상태."""


def normalize_url(value):
    parsed = urlsplit(value.strip())
    if (parsed.scheme != "https" or parsed.hostname not in {"band.us", "www.band.us"}
            or parsed.username or parsed.password or parsed.port
            or not re.fullmatch(r"/band/\d+/media/\d+/?", parsed.path)):
        raise ValueError("BAND_PAGE_URL에는 https://band.us/band/숫자/media/숫자 형식의 사진 상세 주소를 입력하세요.")
    return urlunsplit(("https", "www.band.us", parsed.path.rstrip("/"), "", ""))


def text_keys(page_url, author, created_at, content, *, is_reply=False, occurrence=0,
              slot_occurrence=None):
    """본문은 디스크에 남기지 않고 화면 텍스트의 해시만 저장한다."""
    def digest(parts):
        return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()

    slot = [page_url, author, created_at]
    # 본문 해시는 기존 이력과 호환되게 유지하고, 분 단위 중복 방지에서 대댓글을 구분한다.
    guard = slot + ["reply"] if is_reply else slot
    repeated = ["occurrence", occurrence] if occurrence else []
    guard_index = occurrence if slot_occurrence is None else slot_occurrence
    guard_repeated = ["occurrence", guard_index] if guard_index else []
    return ("text-v1:" + digest(slot + [content] + repeated),
            "slot-v1:" + digest(guard + guard_repeated))


def comment_time(value):
    match = re.fullmatch(r"(\d{4})년\s*(\d+)월\s*(\d+)일\s*(오전|오후)\s*(\d+):(\d+)", value.strip())
    if not match:
        raise BandError("댓글 작성 시각을 읽지 못했습니다.")
    year, month, day, period, hour, minute = match.groups()
    hour = int(hour) % 12 + (12 if period == "오후" else 0)
    try:
        return datetime(int(year), int(month), int(day), hour, int(minute)).isoformat()
    except ValueError:
        raise BandError("댓글 작성 시각 형식이 올바르지 않습니다.") from None


def could_share_slot(other, target):
    # 작성자와 본문이 모두 없는 빈 행은 댓글로 볼 근거가 없다.
    if not other["author"] and not other["content"]:
        return False
    return (not other["is_self"] and not is_bot_message(other["content"])
            and other["is_reply"] == target["is_reply"]
            and other["slot_occurrence"] == target["slot_occurrence"]
            and (not other["author"] or target["author"][0] in other["author"])
            and (not other["timestamp"] or other["timestamp"] == target["timestamp"]))


class BandBrowser:
    def __init__(self, page, url):
        self.page = page
        self.url = normalize_url(url)
        # 입력창이 없을 때 더 바깥 조상으로 올라가 피드 입력창을 쓰지 않는다.
        self.scope = page.locator(PHOTO_LIST).locator(PHOTO_SCOPE).filter(
            has=page.locator(MAIN_INPUT),
        )
        self.started = False
        self.reply_target = None
        self.reply_regions = {}
        self.targets = {}
        self.target_identities = {}
        self.confirmed_reply_keys = set()
        self.processed = set()
        self.ignore_comments_through = None

    def ready(self):
        if self.page.is_closed():
            raise ValueError("BAND 브라우저가 닫혔습니다. 프로그램을 다시 실행하세요.")
        parsed = urlsplit(self.page.url)
        if (parsed.hostname not in {"band.us", "www.band.us"}
                or parsed.path.rstrip("/") != urlsplit(self.url).path):
            raise BandError("BAND 로그인 또는 지정한 사진 댓글 화면을 기다리는 중입니다.")
        # DOMContentLoaded 뒤에도 사진 레이어는 비동기로 만들어진다.
        # 레이아웃 자체의 크기 대신 실제 댓글 목록과 입력창을 확인한다.
        try:
            self.scope.first.wait_for(state="attached", timeout=30000)
        except PlaywrightTimeoutError:
            raise BandError(
                "사진 댓글 구조를 30초 안에 확인하지 못했습니다 "
                f"(보이는 사진 댓글 목록 {self.page.locator(PHOTO_LIST).count()}개, "
                f"최상위 입력창 {self.page.locator(MAIN_INPUT).count()}개). "
                "댓글이 보이면 BAND 화면 구조와 코드의 선택자가 맞는지 확인하세요."
            ) from None
        if self.scope.count() != 1:
            raise BandError("사진 댓글 화면이 여러 개 열려 있어 대상을 결정할 수 없습니다.")
        self.scope.locator('a._footerInfoComment:visible').wait_for(
            state="visible", timeout=10000,
        )
        self.scope.locator(MAIN_INPUT).wait_for(
            state="visible", timeout=10000,
        )

    def read_row(self, row):
        return self.comment_from_row_data(row.evaluate(ROW_TEXT))

    def comment_from_row_data(self, data):
        if len(data["author"]) != 1:
            raise BandAuthorUnavailable(
                f"댓글 작성자를 화면에서 하나로 확인하지 못했습니다 (이름 후보 {len(data['author'])}개)."
            )
        created = comment_time(data["timestamp"] or "")
        is_reply = bool(data.get("is_reply"))
        key, slot = text_keys(self.url, data["author"][0], created, data["content"],
                              is_reply=is_reply, occurrence=data.get("occurrence", 0),
                              slot_occurrence=data.get("slot_occurrence"))
        return {"comment_key": key, "slot_key": slot, "is_self": data["is_self"],
                "content": data["content"], "created_at": created,
                "is_reply": is_reply,
                "thread_root": data.get("thread_root"),
                "author": data["author"][0]}

    def expand_managed_threads(self):
        area = self.scope.locator(PHOTO_LIST)
        controls = area.get_by_role("button", name=REPLY_EXPAND).or_(
            area.get_by_role("link", name=REPLY_EXPAND))
        # 전송/다른 원댓글의 더보기와 섞이지 않도록 읽기 직전에 소속을 다시 확인한다.
        for control in reversed(controls.all()):
            try:
                node = control.element_handle(timeout=2500)
                if node and thread_topic(node.evaluate(EXPANDER_ROOT)):
                    node.click(timeout=2500)
                    node.wait_for_element_state("hidden", timeout=1500)
            except PlaywrightError:
                continue

    def fetch_comments(self, refresh=True):
        stage = "브라우저 확인"
        try:
            if self.page.is_closed():
                raise ValueError("BAND 브라우저가 닫혔습니다. 프로그램을 다시 실행하세요.")
            # 로그아웃/다른 화면에서도 지정된 URL로 돌아간다. 로그인은 사용자가 직접 한다.
            current = urlsplit(self.page.url)
            at_target = (current.hostname in {"band.us", "www.band.us"}
                         and current.path.rstrip("/") == urlsplit(self.url).path)
            logged_in = (current.hostname in {"band.us", "www.band.us"}
                         and self.page.locator('#btnShowNews').count() == 1)
            if refresh and self.page.url == self.url:
                stage = "사진 페이지 새로고침"
                self.page.reload(wait_until="domcontentloaded", timeout=45000)
                self.started = True
            elif refresh and (not self.started or at_target or logged_in):
                stage = "사진 페이지 열기"
                self.page.goto(self.url, wait_until="domcontentloaded", timeout=45000)
                self.started = True
            stage = "사진 댓글 화면과 입력창 대기"
            self.ready()
            self.expand_managed_threads()
            stage = "화면 댓글 작성 시각 읽기"
            rows = self.scope.locator(PHOTO_LIST).locator(VISIBLE_ROW)
            times = rows.evaluate_all(f"rows => rows.map({ROW_TIME})")
            indexes = []
            invalid_times = 0
            for index, timestamp in enumerate(times):
                try:
                    created = comment_time(timestamp or "")
                except BandError:
                    invalid_times += 1
                    continue
                if self.ignore_comments_through is None or created > self.ignore_comments_through:
                    indexes.append(index)

            stage = "새 댓글 내용 읽기"
            options = {"times": times, "indexes": indexes}
            snapshot = rows.evaluate_all(ROW_SNAPSHOT, options)
            if snapshot is None:
                raise BandError("댓글 목록이 변경되어 다시 읽습니다.")
            comments = []
            unreadable = []
            candidates = []
            for index, data in zip(indexes, snapshot):
                try:
                    comment = self.comment_from_row_data(data)
                except BandAuthorUnavailable:
                    unreadable.append(data)
                    continue
                comment["ambiguous"] = False
                comments.append(comment)
                if not comment["is_self"] and not is_bot_message(comment["content"]):
                    candidates.append((index, data, comment))

            slots = Counter(comment["slot_key"] for _, _, comment in candidates
                            if comment["comment_key"] not in self.processed)
            targets = {}
            identities = {}
            tokens = []
            for index, data, comment in candidates:
                comment["ambiguous"] = (
                    comment["comment_key"] not in self.processed
                    and (slots[comment["slot_key"]] > 1
                         or any(could_share_slot(other, data) for other in unreadable))
                )
                if comment["ambiguous"]:
                    continue
                token = uuid.uuid4().hex
                tokens.append((index, token))
                targets[comment["comment_key"]] = token
                identities[comment["comment_key"]] = data

            # 같은 시각의 댓글도 내용이 바뀌었는지 검증한 후 해당 DOM 요소에만 표식을 붙인다.
            if rows.evaluate_all(ROW_SNAPSHOT, {**options, "expected": snapshot, "tokens": tokens}) is None:
                raise BandError("댓글 목록이 변경되어 다시 읽습니다.")
            self.targets = targets
            self.target_identities = identities
            self.reply_regions = {}
            if invalid_times:
                LOG.warning("작성 시각을 확인할 수 없는 댓글 %d개를 이번 조회에서 건너뜁니다.", invalid_times)
            if unreadable:
                LOG.warning("작성자를 확인할 수 없는 댓글 %d개를 이번 조회에서 건너뜁니다.", len(unreadable))
            return sorted(comments, key=lambda item: item["created_at"])
        except PlaywrightError as exc:
            # 원격 오류 전문에는 댓글 본문이 들어갈 수 있어 유형과 실패 단계만 표시한다.
            reason = "화면 요소 또는 연결을 확인하세요."
            if "strict mode violation" in str(exc):
                reason = "화면 요소가 여러 개 일치합니다."
            elif isinstance(exc, PlaywrightTimeoutError):
                reason = "화면 요소가 제한 시간 안에 나타나지 않았습니다."
            raise BandError(f"{stage} 실패 ({type(exc).__name__}): {reason}") from None

    def target_row(self, target, *, check_ambiguity=True):
        # 전송 확인 중 전체 조회/새로고침/더보기를 반복하지 않는다.
        self.ready()
        token = self.targets.get(target["comment_key"])
        if (target.get("ambiguous") or target.get("is_self")
                or is_bot_message(target.get("content"))):
            raise BandError("답글 대상 댓글이 삭제/수정되었거나 구분이 모호합니다.")
        if not token:
            raise BandTargetUnavailable("답글 대상 댓글이 삭제/수정되었거나 구분이 모호합니다.")
        area = self.scope.locator(PHOTO_LIST)
        row = area.locator(f'{ROW}[data-roulette-target="{token}"]:visible')
        identity = self.target_identities[target["comment_key"]]
        timestamp = json.dumps(identity["timestamp"], ensure_ascii=False)
        same_minute = area.locator(VISIBLE_ROW).filter(
            has=self.page.locator(f'time.time[title={timestamp}]'),
        )
        if row.count() == 0:
            # 실제 BAND는 답글을 열 때 목록을 재렌더링한다. 전체 조회 없이
            # 같은 작성자/시각/본문의 유일한 댓글만 다시 연결한다.
            found = []
            for candidate in same_minute.all():
                try:
                    item = self.read_row(candidate)
                except BandError:
                    continue
                if item["comment_key"] == target["comment_key"] and not item["is_self"]:
                    found.append(candidate)
            if len(found) != 1:
                raise BandTargetUnavailable("답글 대상 댓글이 삭제/수정되었거나 구분이 모호합니다.")
            found[0].evaluate('(el, token) => el.setAttribute("data-roulette-target", token)', token)
        if row.count() != 1:
            raise BandError("답글 대상 댓글을 하나로 확인하지 못했습니다.")
        current = self.read_row(row)
        if (current["comment_key"] != target["comment_key"] or current["is_self"]
                or current.get("thread_root") != target.get("thread_root")):
            raise BandTargetUnavailable("답글 대상 댓글이 삭제/수정되었거나 구분이 모호합니다.")
        if check_ambiguity:
            # 새 답글을 쓰기 전에는 같은 이름/시각의 다른 댓글도 검사한다.
            # 이미 보낸 답글을 읽을 때는 이 검사로 전송 확인을 막지 않는다.
            for other in same_minute.all():
                if other.get_attribute("data-roulette-target") == token:
                    continue
                data = other.evaluate(ROW_TEXT)
                if not could_share_slot(data, identity):
                    continue
                if len(data["author"]) == 1:
                    try:
                        other_key, _ = text_keys(
                            self.url, data["author"][0], comment_time(data["timestamp"] or ""),
                            data["content"], is_reply=bool(data.get("is_reply")),
                            occurrence=data.get("occurrence", 0),
                        )
                    except BandError:
                        pass
                    else:
                        if other_key in self.processed:
                            continue
                raise BandError("답글 대상과 작성자·시각이 겹치거나 구분이 모호합니다.")
        return row

    def reply_region(self, row, *, read_only=False):
        # 답글 입력창과 원댓글의 가장 가까운 공통 조상만 허용한다.
        # 목록 전체/사진 전체/피드 입력창으로 범위를 넓히지 않는다.
        depth = row.evaluate("""(row, readOnly) => {
            const layout = row.parentElement?.matches('[data-viewname="DCommentLayoutView"]');
            let node = layout ? row.parentElement : row;
            for (let depth = layout ? 1 : 0; node && node !== document.body; depth++, node = node.parentElement) {
                if (node.matches('._photoCommentListRegion') ||
                    node.querySelector('._footerInfoComment, .commentContainer._writeMain textarea.commentWrite:not(._replyInputRegion textarea)')) return -1;
                // 전송 후 입력창이 숨겨지거나 사라져도 원댓글과 답글의 layout은 읽는다.
                if (readOnly && node.matches('[data-viewname="DCommentLayoutView"]')) return depth;
                const inputs = [...node.querySelectorAll('._replyInputRegion textarea.commentWrite, .commentContainer._writeReply textarea.commentWrite')]
                    .filter(el => readOnly || (el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden'));
                if (inputs.length === 1) return depth;
                if (inputs.length > 1) return -1;
            }
            return -1;
        }""", read_only)
        if depth < 0:
            raise BandError("원댓글에 연결된 답글 영역을 확인하지 못했습니다.")
        return row if depth == 0 else row.locator("xpath=" + "/".join([".."] * depth))

    def open_reply(self, target):
        row = self.target_row(target)
        if self.reply_target == target["comment_key"]:
            try:
                return self.reply_region(row)
            except BandError:
                pass
        # 버튼의 텍스트로 답글쓰기를 찾고, 자식 답글의 버튼은 제외한다.
        buttons = row.get_by_role("button", name=REPLY_BUTTON).or_(
            row.get_by_role("link", name=REPLY_BUTTON))
        handle = row.element_handle()
        buttons = [b for b in buttons.all() if b.is_visible() and b.evaluate(
            '(el, row) => el.closest(\'[data-viewname="DCommentView"]\') === row', handle,
        )]
        if len(buttons) != 1:
            raise BandError("대상 댓글의 답글쓰기 버튼을 하나로 확인하지 못했습니다.")
        self.reply_target = None
        buttons[0].click()
        try:
            region = self.reply_region(self.target_row(target))
        except BandTargetUnavailable:
            # 방금 검증한 답글쓰기 버튼의 클릭으로 대상이 재렌더링될 수 있다.
            # 이때는 단 하나의 열린 답글창과 BAND의 자동 작성자 멘션으로 연결한다.
            textarea = self.scope.locator(REPLY_INPUT)
            textarea.wait_for(state='visible', timeout=10000)
            if textarea.count() != 1 or not has_target_mention(textarea.input_value(), target.get('author')):
                raise BandError('답글쓰기 후 열린 입력창의 자동 멘션을 확인하지 못했습니다.')
            depth = textarea.evaluate('''input => {
                let depth = 0;
                for (let node = input; node && node !== document.body; node = node.parentElement, depth++) {
                    if (node.matches('._photoCommentListRegion') || node.querySelector('._footerInfoComment')) return -1;
                    if (node.matches('[data-viewname="DCommentLayoutView"]') ||
                        node.querySelector('[data-viewname="DCommentView"]')) return depth;
                }
                return -1;
            }''')
            if depth < 0:
                raise BandError('답글쓰기 후 열린 입력창의 댓글 영역을 확인하지 못했습니다.')
            region = textarea if depth == 0 else textarea.locator('xpath=' + '/'.join(['..'] * depth))
        region.locator(REPLY_INPUT).wait_for(state="visible", timeout=10000)
        token = uuid.uuid4().hex
        region.evaluate('(el, token) => el.setAttribute("data-roulette-reply-region", token)', token)
        self.reply_regions[target['comment_key']] = token
        self.reply_target = target["comment_key"]
        return region

    def reply_count(self, target, body):
        """대상 답글 영역에서 같은 본문의 자기 댓글 개수를 센다."""
        try:
            # 답글쓰기에서 연결한 영역을 우선 사용해 대상이 재렌더링되어도 확인한다.
            # 연결한 영역이 없으면 대상 댓글을 다시 검증한다.
            token = self.reply_regions.get(target['comment_key'])
            region = self.scope.locator(f'[data-roulette-reply-region="{token}"]') if token else None
            if region is None or region.count() != 1:
                row = self.target_row(target, check_ambiguity=False)
                region = self.reply_region(row, read_only=True)
            # 자기 답글의 본문/수정 메뉴만 필요하다. 타인의 작성자 파싱 실패로
            # 이미 전송한 결과의 확인이 막히지 않도록 한다.
            matches = [c for c in (row.evaluate(ROW_TEXT) for row in region.locator(ROW).all())
                       if c["is_self"] and matches_reply(c["content"], target, body)]
            for data in matches:
                if len(data["author"]) != 1:
                    continue
                try:
                    created = comment_time(data["timestamp"] or "")
                except BandError:
                    continue
                key, _ = text_keys(self.url, data["author"][0], created, data["content"],
                                   occurrence=data.get("occurrence", 0))
                self.confirmed_reply_keys.add(key)
            return len(matches)
        except PlaywrightError:
            raise BandError("대댓글 전송 결과를 화면에서 확인하지 못했습니다.") from None

    def write_mentioned_reply(self, writer, textarea, target, body):
        author = target.get("author")
        if not isinstance(author, str) or not author or "\n" in author or "\r" in author:
            raise BandError("멘션할 베팅 작성자를 확인하지 못했습니다.")
        marker = writer.locator('.mentions strong', has_text=re.compile(r"^@" + re.escape(author) + r"$"))
        # 답글쓰기 때 BAND가 자동으로 넣은 작성자 멘션을 지우지 않는다.
        prefix = textarea.input_value()
        if prefix.strip() and not has_target_mention(prefix, author):
            raise BandError("답글쓰기 입력창의 자동 멘션이 베팅 작성자와 일치하지 않습니다.")
        had_marker = marker.count() > 0
        # 자동 멘션과 BAND가 넣은 공백을 보존한 채 결과만 덧붙인다.
        textarea.press("Control+End")
        suffix = ('\n' if prefix.strip() else '') + body
        self.page.keyboard.insert_text(suffix)
        textarea.press("End")
        expected = normalized_text(prefix + suffix)
        if ((had_marker and marker.count() == 0)
                or normalized_text(textarea.input_value()) != expected):
            raise BandError("결과 입력 후 베팅 작성자 멘션이 유지되지 않았습니다.")

    def submit(self, target, body):
        stage = "대상 대댓글 입력창 열기"
        click_attempted = False
        try:
            region = self.open_reply(target)
            stage = "대댓글 입력창 확인"
            textarea = region.locator(REPLY_INPUT)
            if textarea.count() != 1:
                raise BandError("결과를 작성할 대댓글 입력창이 하나가 아닙니다.")
            writer = textarea.locator('xpath=ancestor::*[@data-viewname="DCommentInputView"][1]')
            if writer.count() != 1:
                raise BandError("결과를 작성할 대댓글 입력창이 하나가 아닙니다.")
            stage = "비밀 설정 버튼 확인"
            secret = writer.locator('._secretModeButton:visible')
            if secret.count() != 1:
                raise BandError("대댓글의 비밀 설정 버튼을 확인하지 못했습니다.")
            state = secret.get_attribute("aria-checked")
            if state == "false":
                stage = "비밀 설정 버튼 클릭"
                secret.click()
            if state not in {"true", "false"}:
                raise BandError("비밀 대댓글 설정 상태를 확인하지 못했습니다.")
            stage = "비밀 설정 적용 확인"
            # 설정 반영 중 버튼/입력창이 교체되면 ElementHandle은 이전 요소를
            # 계속 가리킨다. Locator로 현재 화면의 체크 상태를 다시 찾는다.
            writer.locator('._secretModeButton[aria-checked="true"]:visible').wait_for(
                state="visible", timeout=5000,
            )
            stage = "베팅 작성자 멘션 및 결과 본문 입력"
            self.write_mentioned_reply(writer, textarea, target, body)
            # 입력 중 설정이 풀리면 공개 전송하지 않는다.
            stage = "전송 직전 비밀 설정 확인"
            if secret.get_attribute("aria-checked") != "true":
                raise BandError("비밀 설정이 유지되지 않아 대댓글을 전송하지 않았습니다.")
            stage = "전송 버튼 클릭"
            # click이 타임아웃이어도 서버에는 전송됐을 수 있으므로 이 뒤에는 재전송하지 않는다.
            click_attempted = True
            writer.locator('button._sendMessageButton').click(timeout=10000)
        except PlaywrightError as exc:
            # Playwright 원문에는 입력한 결과/댓글이 포함될 수 있다.
            reason = "화면 요소 또는 브라우저 연결을 확인하세요."
            if "strict mode violation" in str(exc):
                reason = "화면 요소가 여러 개 일치합니다."
            elif isinstance(exc, PlaywrightTimeoutError):
                reason = "화면 요소가 제한 시간 안에 준비되지 않았습니다."
            if click_attempted:
                raise BandSendUncertain(
                    "전송 버튼 클릭을 시도했습니다. 중복 전송을 막기 위해 화면 확인만 계속합니다."
                ) from None
            raise BandError(f"BAND 비밀 대댓글 {stage} 실패 ({type(exc).__name__}): {reason}") from None
