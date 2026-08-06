from __future__ import annotations

import io
import threading
import unittest

from tools.acm_agent.deepseek import DeepSeekCancelScope
from tools.acm_agent.stress_sources import (
    AllowlistedCrawler,
    SourceSearchError,
    _public_https_url,
)


def public_resolver(host, port, **kwargs):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


class Response(io.BytesIO):
    def __init__(self, url: str, body: str):
        super().__init__(body.encode("utf-8"))
        self._url = url

    def geturl(self):
        return self._url


class MappingTransport:
    def __init__(self, mapping):
        self.mapping = mapping
        self.urls = []

    def __call__(self, request, timeout):
        self.urls.append(request.full_url)
        return Response(request.full_url, self.mapping[request.full_url])


class StressSourceTests(unittest.TestCase):
    def test_cancelled_scope_stops_before_fetch(self) -> None:
        scope = DeepSeekCancelScope()
        scope.cancel()
        transport = MappingTransport({})
        crawler = AllowlistedCrawler(
            transport=transport,
            resolver=public_resolver,
            min_host_interval=0,
            cancel_scope=scope,
        )
        with self.assertRaises(SourceSearchError) as caught:
            crawler.search("codeforces_official", problem_id="CF123A")
        self.assertEqual(caught.exception.code, "request_cancelled")
        self.assertEqual(transport.urls, [])

    def test_cancelled_scope_closes_live_source_response(self) -> None:
        started = threading.Event()
        closed = threading.Event()

        class BlockingResponse:
            def geturl(self):
                return "https://codeforces.com/contest/123"

            def read(self, _size):
                started.set()
                closed.wait(2.0)
                raise OSError("closed")

            def close(self):
                closed.set()

        scope = DeepSeekCancelScope()
        crawler = AllowlistedCrawler(
            transport=lambda request, timeout: BlockingResponse(),
            resolver=public_resolver,
            min_host_interval=0,
            cancel_scope=scope,
        )
        result: list[BaseException] = []

        def fetch() -> None:
            try:
                crawler._read("https://codeforces.com/contest/123", robots=False)
            except BaseException as exc:
                result.append(exc)

        worker = threading.Thread(target=fetch)
        worker.start()
        self.assertTrue(started.wait(1.0))
        scope.cancel()
        worker.join(2.0)
        self.assertFalse(worker.is_alive())
        self.assertTrue(closed.is_set())
        self.assertEqual(getattr(result[0], "code", ""), "request_cancelled")

    def test_rejects_non_allowlisted_and_private_resolution(self) -> None:
        with self.assertRaises(SourceSearchError):
            _public_https_url("http://codeforces.com/blog/entry/1", resolver=public_resolver)
        with self.assertRaises(SourceSearchError):
            _public_https_url("https://example.com/x", resolver=public_resolver)

        def private(host, port, **kwargs):
            return [(2, 1, 6, "", ("127.0.0.1", port))]

        with self.assertRaises(SourceSearchError) as caught:
            _public_https_url("https://codeforces.com/blog/entry/1", resolver=private)
        self.assertEqual(caught.exception.code, "source_url_rejected")

    def test_codeforces_search_only_follows_editorial_and_extracts_complete_cpp(self) -> None:
        mapping = {
            "https://codeforces.com/robots.txt": "User-agent: *\nAllow: /\n",
            "https://codeforces.com/contest/123": (
                '<a href="/blog/entry/9">Codeforces Round 123 Tutorial</a>'
                '<a href="/submission/10">accepted submission</a>'
            ),
            "https://codeforces.com/blog/entry/9": (
                "<title>CF123A Tutorial</title><p>CF123A proof</p>"
                "<pre>#include &lt;bits/stdc++.h&gt;\nint main(){return 0;}</pre>"
            ),
        }
        transport = MappingTransport(mapping)
        crawler = AllowlistedCrawler(
            transport=transport,
            resolver=public_resolver,
            min_host_interval=0,
        )
        found = crawler.search("codeforces_official", problem_id="CF123A")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].complete_cpp)
        self.assertIn("#include", found[0].code or "")
        self.assertFalse(any("submission" in url for url in transport.urls))

    def test_codeforces_editorial_matches_exact_problem_link_without_cf_key_text(self) -> None:
        mapping = {
            "https://codeforces.com/robots.txt": "User-agent: *\nAllow: /\n",
            "https://codeforces.com/contest/123": '<a href="/blog/entry/9">Tutorial</a>',
            "https://codeforces.com/blog/entry/9": (
                '<title>Round tutorial</title><a href="/contest/123/problem/A">A. Example</a>'
                '<pre>#include &lt;iostream&gt;\nint main(){return 0;}</pre>'
            ),
        }
        crawler = AllowlistedCrawler(
            transport=MappingTransport(mapping),
            resolver=public_resolver,
            min_host_interval=0,
        )
        found = crawler.search("codeforces_official", problem_id="CF123A")
        self.assertEqual(len(found), 1)

    def test_robots_denial_and_challenge_fail_closed(self) -> None:
        search_url = "https://www.luogu.com.cn/article?keyword=P1000"
        denied = MappingTransport({
            "https://www.luogu.com.cn/robots.txt": "User-agent: *\nDisallow: /problem/solution/\n",
            search_url: "<title>no results</title>",
        })
        crawler = AllowlistedCrawler(
            transport=denied,
            resolver=public_resolver,
            min_host_interval=0,
        )
        self.assertEqual(crawler.search("luogu_solutions", problem_id="P1000"), [])
        self.assertIn("https://www.luogu.com.cn/robots.txt", denied.urls)

    def test_luogu_article_is_discovered_by_search_only_host(self) -> None:
        search_url = "https://www.luogu.com.cn/article?keyword=P2596"
        article = "https://www.luogu.com.cn/article/fixture123"
        mapping = {
            "https://www.luogu.com.cn/robots.txt": "User-agent: *\nAllow: /\n",
            search_url: (
                f'<a class="result__a" href="{article}">'
                "P2596 题解</a>"
            ),
            article: (
                "<title>P2596 [ZJOI2006] 书架 题解</title>"
                "<pre>#include &lt;bits/stdc++.h&gt;\nint main(){return 0;}</pre>"
            ),
        }
        transport = MappingTransport(mapping)
        crawler = AllowlistedCrawler(
            transport=transport,
            resolver=public_resolver,
            min_host_interval=0,
        )
        found = crawler.search(
            "luogu_solutions", problem_id="P2596", title="ZJOI2006 书架"
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].url, article)
        self.assertTrue(found[0].complete_cpp)
        self.assertFalse(any("duckduckgo.com" in url for url in transport.urls))


if __name__ == "__main__":
    unittest.main()
