"""BAND 사진 대댓글을 로컬 CRL2 서버로 전달한다."""

import hashlib
import json
import logging
import math
import msvcrt
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from threading import Event, Thread
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from band_browser import BandBrowser, BandError, BandSendUncertain, normalize_url
from bot_messages import COMMAND_MENTION, THREAD_TOPICS, bet_body_candidates, bet_command_text, is_bot_message, thread_topic
from roulette import covered_numbers, parse_standard_bet

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("crl2")
ACTION = {"확인": "verify", "상태": "status", "히트": "hit", "hit": "hit",
          "스탠드": "stand", "stand": "stand"}
SIDE = {"플레이어": "PLAYER", "뱅커": "BANKER", "타이": "TIE", "무승부": "TIE",
        "player": "PLAYER", "banker": "BANKER", "tie": "TIE"}
AMOUNT = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)\s*(?:칩|c)?"
CHIP_COLORS = {
    label: color for color, labels in {
        "red": ("레드", "빨강", "빨간", "빨간색", "적색"),
        "blue": ("블루", "파랑", "파란", "파란색", "청색"),
        "green": ("그린", "초록", "초록색", "녹색"),
        "yellow": ("옐로", "옐로우", "노랑", "노란", "노란색", "황색"),
        "white": ("화이트", "하양", "하얀", "하얀색", "흰색", "백색"),
    }.items() for label in (color, *labels)
}
SEPARATOR = r"[\s,/]+"
TRANSLATE_SEPARATORS = str.maketrans("，／、", ",/,")
COLOR_NAME = "|".join(CHIP_COLORS)
TOPIC_PREFIX = re.compile(rf"({'|'.join(THREAD_TOPICS)}){SEPARATOR}")
WAGER = re.compile(
    rf"(?:(?P<color>{COLOR_NAME}){SEPARATOR})?(?P<amount>{AMOUNT})(?:{SEPARATOR}(?P<target>.+))?",
    re.IGNORECASE | re.DOTALL,
)
CONVERSION = re.compile(
    rf"(?P<source>{COLOR_NAME}){SEPARATOR}(?P<amount>-?{AMOUNT}){SEPARATOR}(?P<destination>{COLOR_NAME})",
    re.IGNORECASE,
)
LEGACY_COLOR = "레드|블루|그린|옐로|옐로우|화이트|red|blue|green|yellow|white"
LEGACY_CONVERSION = re.compile(
    rf"(?:(?:@\*\*ALEA\*\*|\*\*@ALEA\*\*|@ALEA)\s+)*(?P<source>{LEGACY_COLOR})\s*,\s*"
    rf"(?P<amount>-?[0-9]+)\s*,\s*(?P<destination>{LEGACY_COLOR})", re.IGNORECASE,
)


class RejectRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(ProxyHandler({}), RejectRedirect())


def parse_command(text):
    body, fallback = bet_body_candidates(text)
    for candidate in (body, fallback):
        if not candidate:
            continue
        action = ACTION.get(candidate.lower())
        if action:
            return {"action": action}
        match = re.fullmatch(r"룰렛\s+(.+)", candidate, re.DOTALL)
        if match:
            bet = parse_standard_bet(f"{COMMAND_MENTION} {match[1]}")
            if not bet:
                raise ValueError("룰렛 10 / 레드처럼 입력해 주세요.")
            return {"game": "roulette", "amount": bet["chip_count"], "numbers": covered_numbers(bet)}
        match = re.fullmatch(rf"바카라\s+({AMOUNT})\s*/\s*(\S+)", candidate, re.IGNORECASE)
        if match:
            side = SIDE.get(match[2].lower())
            if not side:
                raise ValueError("플레이어, 뱅커, 타이 중 하나를 선택해 주세요.")
            return {"game": "baccarat", "amount": parse_amount(match[1]), "side": side}
        match = re.fullmatch(rf"블랙잭\s+({AMOUNT})", candidate, re.IGNORECASE)
        if match:
            return {"game": "blackjack", "amount": parse_amount(match[1])}
    raise ValueError("확인, 룰렛 10 / 레드, 바카라 10 / 플레이어, 블랙잭 10, 히트, 스탠드, 상태 중 하나를 입력해 주세요.")


def parse_amount(value):
    return int(re.match(r"-?[0-9,]+", value)[0].replace(",", ""))


def conversion_command(match, topic):
    if topic != "convert":
        raise ValueError("환전 원댓글 아래에 입력해 주세요.")
    return {"action": "convert", "amount": parse_amount(match["amount"]),
            "fromColor": CHIP_COLORS[match["source"].lower()],
            "toColor": CHIP_COLORS[match["destination"].lower()]}


def parse_short_command(body, topic):
    body = body.translate(TRANSLATE_SEPARATORS).strip().rstrip(".。．").rstrip()
    heading = TOPIC_PREFIX.match(body)
    if heading:
        if THREAD_TOPICS[heading[1]] != topic:
            raise ValueError("선택한 종목의 원댓글 아래에 입력해 주세요.")
        body = body[heading.end():]
    action = ACTION.get(body.lower())
    if action:
        return {"action": action}
    if topic == "convert":
        conversion = CONVERSION.fullmatch(body)
        return conversion_command(conversion, topic) if conversion else None
    wager = WAGER.fullmatch(body)
    if not wager:
        return None
    target = wager["target"]
    command = {"game": topic, "amount": parse_amount(wager["amount"])}
    if wager["color"]:
        command["color"] = CHIP_COLORS[wager["color"].lower()]
    if topic == "blackjack":
        return command if target is None else None
    if target is None:
        return None
    if topic == "baccarat":
        side = SIDE.get(target.lower())
        if not side:
            raise ValueError("플레이어, 뱅커, 타이 중 하나를 선택해 주세요.")
        return {**command, "side": side}
    target = re.sub(SEPARATOR, " ", target).strip()
    if CHIP_COLORS.get(target.lower()) == "red":
        target = "red"
    bet = parse_standard_bet(f"{COMMAND_MENTION} {wager['amount']} / {target}")
    if not bet:
        raise ValueError("10 / 레드 또는 10 / 1, 5, 8처럼 입력해 주세요.")
    return {**command, "numbers": covered_numbers(bet)}


def parse_thread_command(target, *, allow_short=True):
    if not target["is_reply"]:
        return None
    topic = thread_topic(target.get("thread_root"))
    if topic is None:
        return None
    content = target["content"]
    text = bet_command_text(content, is_reply=True)
    command = None
    if allow_short:
        bodies = bet_body_candidates(text) if text is not None else (content,)
        for body in bodies:
            if body and (command := parse_short_command(body, topic)) is not None:
                break
    else:
        conversion = LEGACY_CONVERSION.fullmatch(content.strip())
        if conversion:
            command = conversion_command(conversion, topic)
    if command is None:
        if text is None:
            return None
        command = parse_command(text)
    if "game" in command and command["game"] != topic:
        raise ValueError("선택한 종목의 원댓글 아래에 입력해 주세요.")
    if command.get("action") in {"hit", "stand"}:
        if topic not in {"baccarat", "blackjack"}:
            raise ValueError("진행 중인 카드 게임의 원댓글 아래에 입력해 주세요.")
        command["game"] = topic
    return command


def write_state(path, state):
    temp = path.with_suffix(".json.tmp")
    with temp.open("w", encoding="utf-8") as output:
        json.dump(state, output, ensure_ascii=False)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp, path)


def load_state(path):
    minute = datetime.now().replace(second=0, microsecond=0).isoformat()
    if not path.exists():
        state = {"cutoff": minute, "conversion_cutoff": minute, "short_cutoff": minute, "processed": []}
        write_state(path, state)
        return state
    state = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(state, dict) or not isinstance(state.get("processed"), list)
            or any(not isinstance(key, str) for key in state["processed"])
            or not isinstance(state.get("cutoff"), str)):
        raise ValueError("BAND 처리 이력 형식을 읽을 수 없습니다.")
    datetime.strptime(state["cutoff"], "%Y-%m-%dT%H:%M:00")
    cutoffs = ("conversion_cutoff", "short_cutoff")
    missing = {key: minute for key in cutoffs if key not in state}
    state.update(missing)
    for key in cutoffs:
        if not isinstance(state[key], str):
            raise ValueError("BAND 입력 양식의 활성화 시각을 읽을 수 없습니다.")
        datetime.strptime(state[key], "%Y-%m-%dT%H:%M:00")
    if missing:
        write_state(path, state)
    return state


def request_reply(target, command):
    packet = {"key": target["slot_key"].split(":", 1)[1], "author": target["author"], "command": command}
    request = Request(
        os.environ["CRL_ENDPOINT"],
        data=json.dumps(packet, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + os.environ["CRL_SECRET"], "Content-Type": "application/json"},
    )
    with OPENER.open(request, timeout=35) as response:
        reply = json.load(response)["reply"]
    if not isinstance(reply, str) or not reply:
        raise ValueError("통합 서버가 답글 내용을 반환하지 않았습니다.")
    return reply


def send_reply(browser, target, body):
    try:
        before = browser.reply_count(target, body)
        try:
            browser.submit(target, body)
        except BandSendUncertain:
            pass
        for attempt in range(2):
            if browser.reply_count(target, body) > before:
                LOG.info("비밀 답글 전송 확인")
                return
            if attempt == 0:
                browser.page.wait_for_timeout(1000)
    except BandError as error:
        LOG.warning("비밀 답글 확인 보류 (%s)", error)
        return
    LOG.warning("비밀 답글이 화면에서 확인되지 않았습니다. 참가자는 상태 명령으로 게임을 다시 확인할 수 있습니다.")


def process_targets(browser, targets, path, state, stopped, diagnostics):
    for target in targets:
        if stopped.is_set():
            return
        key, slot = target["comment_key"], target["slot_key"]
        if not target["is_reply"] or target["is_self"]:
            continue
        if target["ambiguous"]:
            if (key, "ambiguous") not in diagnostics:
                LOG.warning("새 대댓글을 감지했지만 작성자·시각이 겹쳐 안전 확인을 보류했습니다.")
                diagnostics.add((key, "ambiguous"))
            continue
        if key in browser.processed or slot in browser.processed or is_bot_message(target["content"]):
            continue
        try:
            command = parse_thread_command(target, allow_short=target["created_at"] > state["short_cutoff"])
        except ValueError as error:
            command = None
            body = f"입력 오류: {error}"
            keys = (key,)
        else:
            if command is None:
                if (key, "unrecognized") not in diagnostics:
                    topic = thread_topic(target.get("thread_root")) or "unrecognized"
                    called = bet_command_text(target["content"], is_reply=True) is not None
                    LOG.warning("새 대댓글을 감지했지만 명령을 해석하지 못했습니다 (종목=%s, 봇 호출=%s).",
                                topic, "yes" if called else "no")
                    diagnostics.add((key, "unrecognized"))
                continue
            if command.get("action") == "convert" and target["created_at"] <= state["conversion_cutoff"]:
                continue
        try:
            browser.target_row(target)
        except BandError as error:
            if (key, "target") not in diagnostics:
                LOG.warning("명령은 읽었지만 원댓글을 다시 확인하지 못했습니다 (%s).", error)
                diagnostics.add((key, "target"))
            continue
        if command is not None:
            try:
                body = request_reply(target, command)
            except (URLError, OSError, ValueError, KeyError):
                LOG.warning("정산 응답 미확인. 다음 조회에서 해당 요청을 다시 확인합니다.")
                # 연속된 히트/스탠드의 순서를 유지한다.
                return
            keys = (key, slot)
        # Sheets가 정산을 완료한 뒤에만 처리 이력을 남긴다. 답글은 한 번만 시도한다.
        browser.processed.update(keys)
        state["processed"] = sorted(browser.processed)
        write_state(path, state)
        send_reply(browser, target, body)


def await_stop(stopped):
    try:
        os.read(sys.stdin.fileno(), 1)
    finally:
        stopped.set()


def main():
    url = normalize_url(os.environ["BAND_PAGE_URL"])
    poll = float(os.environ.get("POLL_INTERVAL", "20"))
    channel = os.environ.get("BROWSER_CHANNEL", "msedge").strip()
    if not math.isfinite(poll) or poll <= 0 or channel not in {"msedge", "chrome", "chromium"}:
        raise ValueError("POLL_INTERVAL과 BROWSER_CHANNEL 설정을 확인해 주세요.")
    url_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    path = ROOT / f"processed-{url_id}.json"
    stopped = Event()
    if "--managed" in sys.argv:
        Thread(target=await_stop, args=(stopped,), daemon=True).start()
    with (ROOT / "processed.lock").open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"\0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise ValueError("이미 실행 중인 CRL2 봇이 있습니다.") from None
        state = load_state(path)
        with sync_playwright() as playwright:
            context = None
            try:
                ready = False
                last_error = None
                last_scan_summary = None
                diagnostics = set()
                while not stopped.is_set():
                    if context is None:
                        try:
                            context = playwright.chromium.launch_persistent_context(
                                str(ROOT / "browser_profile"), channel=channel,
                                headless=False, locale="ko-KR",
                            )
                            browser = BandBrowser(
                                context.pages[0] if context.pages else context.new_page(), url,
                            )
                            browser.processed = set(state["processed"])
                            browser.ignore_comments_through = state["cutoff"]
                            ready = False
                            last_scan_summary = None
                            diagnostics.clear()
                        except PlaywrightError as error:
                            LOG.warning("BAND 브라우저를 열지 못했습니다 (%s). 다시 시도합니다.",
                                        type(error).__name__)
                            stopped.wait(poll)
                            continue
                    try:
                        targets = browser.fetch_comments()
                    except (BandError, ValueError, PlaywrightError) as error:
                        if str(error) != last_error:
                            LOG.warning("BAND 사진 조회 대기 (%s)", error)
                        last_error = str(error)
                        # 브라우저 창을 닫았거나 Edge가 종료되면 컨텍스트를 다시 띄운다.
                        if ("BAND 브라우저가 닫혔습니다" in str(error)
                                or "TargetClosedError" in str(error)):
                            try:
                                context.close()
                            except PlaywrightError:
                                pass
                            context = None
                            last_error = None
                    else:
                        last_error = None
                        summary = (
                            len(targets),
                            sum(1 for target in targets if target["is_reply"]),
                            sum(1 for target in targets
                                if target["is_reply"] and not target["is_self"]),
                            sum(1 for target in targets if target.get("ambiguous")),
                        )
                        if summary != last_scan_summary:
                            LOG.info("BAND 댓글 목록 확인 (전체=%d, 대댓글=%d, 처리 후보=%d, 모호=%d)",
                                     *summary)
                        last_scan_summary = summary
                        if not ready:
                            print("CRL_READY", file=sys.stderr, flush=True)
                            LOG.info("BAND 대댓글 감시를 시작했습니다.")
                            ready = True
                        process_targets(browser, targets, path, state, stopped, diagnostics)
                    stopped.wait(poll)
            finally:
                if context is not None:
                    context.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        main()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, PlaywrightError) as error:
        LOG.error("봇 실행 실패: %s", error)
        raise SystemExit(1)
