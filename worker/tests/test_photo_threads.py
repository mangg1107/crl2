import unittest

from playwright.sync_api import sync_playwright

from band_browser import BandBrowser, BandTargetUnavailable
from integrated_worker import parse_thread_command


URL = "https://www.band.us/band/101/media/202"
OLD = "2026년 11월 15일 오후 1:00"
RECENT = "2026년 11월 15일 오후 2:00"


def comment(author, body, *, own=False, stamp=RECENT):
    edit = '<button data-uiselector="modifyButton">댓글 수정</button>' if own else ""
    return f'''
      <div data-viewname="DCommentView">
        <span class="commentName">{author}</span>
        <p class="_commentContent" style="white-space:pre-wrap">{body}</p>
        <time class="time" title="{stamp}">작성 시각</time>
        {edit}
      </div>'''


def thread(name, root, reply, *, collapsed=False):
    toggle = '''
      <button onclick="this.nextElementSibling.hidden = false; this.remove()">답글 1개 모두 보기</button>
    ''' if collapsed else ""
    hidden = "hidden" if collapsed else ""
    return f'''
      <section id="{name}" data-viewname="DCommentLayoutView">
        {root}
        <div class="_replyRegion">{toggle}<div {hidden}>{reply}</div></div>
      </section>'''


class PhotoThreadsTest(unittest.TestCase):
    def test_only_alea_threads_route_replies_and_open_collapsed_conversion(self):
        exchange = thread("conversion", comment("**ALEA**", "환전\n\n양식 안내", own=True, stamp=OLD),
                          comment("가", "빨강 / 5, green"), collapsed=True)
        unrelated = thread("unrelated", comment("ALEA", "환전", stamp=OLD),
                           comment("나", "레드, 6, 그린"), collapsed=True)
        roulette = thread("roulette", comment("ALEA", "룰렛", own=True, stamp=OLD),
                          comment("다", "@ALEA @**ALEA** 블루 10 / 레드"))
        baccarat = thread("baccarat", comment("ALEA", "바카라", own=True, stamp=OLD),
                          comment("라", "히트"))
        wrong = thread("wrong", comment("ALEA", "블랙잭", own=True, stamp=OLD),
                       comment("마", "@**ALEA** 바카라 10 / 뱅커"))
        html = f'''
          <main><a class="_footerInfoComment">댓글</a>
            <div class="_photoCommentListRegion">{exchange}{unrelated}{roulette}{baccarat}{wrong}</div>
            <div class="commentContainer _writeMain"><textarea class="commentWrite"></textarea></div>
          </main>'''
        with sync_playwright() as playwright:
            edge = playwright.chromium.launch(channel="msedge", headless=True, timeout=8000)
            try:
                page = edge.new_page()
                page.set_default_timeout(5000)
                page.route("**/*", lambda route: route.fulfill(body=html, content_type="text/html; charset=utf-8"))
                page.goto(URL, wait_until="domcontentloaded")
                browser = BandBrowser(page, URL)
                browser.ignore_comments_through = "2026-11-15T13:30:00"
                replies = {row["author"]: row for row in browser.fetch_comments(refresh=False)}
                self.assertTrue(page.locator("#unrelated button").is_visible())
                self.assertEqual(set(replies), {"가", "다", "라", "마"})
                self.assertEqual(parse_thread_command(replies["가"]), {
                    "action": "convert", "amount": 5, "fromColor": "red", "toColor": "green"
                })
                roulette_command = parse_thread_command(replies["다"])
                self.assertEqual(roulette_command["game"], "roulette")
                self.assertEqual(roulette_command["color"], "blue")
                self.assertEqual(parse_thread_command(replies["라"]), {"action": "hit", "game": "baccarat"})
                with self.assertRaisesRegex(ValueError, "선택한 종목"):
                    parse_thread_command(replies["마"])
                page.locator("#conversion p").first.evaluate("(el) => el.textContent = '룰렛'")
                with self.assertRaises(BandTargetUnavailable):
                    browser.target_row(replies["가"])
            finally:
                edge.close()


class ThreadCommandTest(unittest.TestCase):
    def parse(self, topic, content, *, allow_short=True):
        return parse_thread_command({
            "is_reply": True, "content": content,
            "thread_root": {"author": ["ALEA"], "is_self": True, "content": f"{topic}\n양식"},
        }, allow_short=allow_short)

    def test_mixed_separators_keep_the_wager_color_separate_from_the_target(self):
        for text in ("10,1 5 / 8,10", "10 / 1, 5, 8, 10", "10 1 5 8 10", "10／1、5，8/10"):
            with self.subTest(text=text):
                self.assertEqual(self.parse("룰렛", text), {
                    "game": "roulette", "amount": 10, "numbers": [1, 5, 8, 10],
                })
        self.assertEqual(self.parse("룰렛", "Blue 10 레드")["color"], "blue")
        self.assertNotIn("color", self.parse("룰렛", "10 레드"))
        self.assertEqual(self.parse("룰렛", "1,000 / 17")["amount"], 1000)
        self.assertEqual(self.parse("바카라", "노랑, 10 / BANKER"), {
            "game": "baccarat", "amount": 10, "color": "yellow", "side": "BANKER",
        })
        self.assertEqual(self.parse("환전", "빨강 5 / 그린"), {
            "action": "convert", "amount": 5, "fromColor": "red", "toColor": "green",
        })
        self.assertEqual(self.parse("환전", "RED, 1,000, 흰색")["amount"], 1000)
        self.assertEqual(self.parse("블랙잭", "0"), {"game": "blackjack", "amount": 0})

    def test_chip_aliases_and_old_commands(self):
        aliases = {
            "red": ("red", "레드", "빨강"), "blue": ("Blue", "블루", "파랑"),
            "green": ("green", "그린", "초록"), "yellow": ("yellow", "옐로", "옐로우", "노랑"),
            "white": ("white", "화이트", "흰색"),
        }
        for color, labels in aliases.items():
            for label in labels:
                with self.subTest(label=label):
                    self.assertEqual(self.parse("블랙잭", f"{label} / 10")["color"], color)
                    self.assertEqual(self.parse("환전", f"{label} / 5, 레드")["fromColor"], color)
        self.assertIsNone(self.parse("블랙잭", "10", allow_short=False))
        self.assertIsNone(self.parse("환전", "빨강 5 그린", allow_short=False))
        self.assertEqual(self.parse("룰렛", "@ALEA 룰렛 10 / 레드", allow_short=False)["amount"], 10)
        self.assertEqual(self.parse("환전", "레드, 5, 그린", allow_short=False)["amount"], 5)
        self.assertEqual(self.parse("환전", "확인"), {"action": "verify"})
