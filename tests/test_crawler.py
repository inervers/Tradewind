from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from app.config import (
    VISION_PROVIDERS, get_vision_config, get_vision_failover_configs,
    get_vision_provider_config,
)
from app.crawler.lead_hunter import MAX_HTML_BYTES, _fetch, _valid_email
from app.crawler.maps_hunter import (
    _is_candidate_photo_url, _is_google_internal_host, _place_key, _safe_photo_dir_name,
)
from app.crawler.progress import report, use_progress_sink
from app.crawler.result_utils import matches_targets, target_reached
from app.crawler.vision_analyzer import (
    _ask_vision, _cache, _candidate_image_check, _image_media_type, _image_suffix,
    _hash_distance, _normalize_items, _perceptual_hash, analyze_photos,
)
from app.crawler.webs_hunter import (
    _clean_results, _crawl_site, _extract_phone, _extract_whatsapp, _industry_relevance,
    _is_hong_kong_site, _resolve_bing_url, _search, hunt_websites,
)


class _FakeResponse:
    def __init__(self, status: int, text: str = "", content_type: str = "text/html") -> None:
        self.status_code = status
        self.text = text
        self.content = text.encode()
        self.headers = {"content-type": content_type}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = responses
        self.calls = 0

    def get(self, _url: str, timeout: int) -> _FakeResponse:
        del timeout
        response = self.responses[self.calls]
        self.calls += 1
        return response


class FetchTests(unittest.TestCase):
    @patch("app.crawler.lead_hunter.time.sleep", return_value=None)
    def test_fetch_retries_temporary_status(self, _sleep) -> None:
        client = _FakeClient([
            _FakeResponse(503),
            _FakeResponse(200, "<html>ok</html>"),
        ])

        self.assertEqual(_fetch("https://example.test", "", client=client), "<html>ok</html>")
        self.assertEqual(client.calls, 2)

    def test_fetch_rejects_non_html_and_oversized_content(self) -> None:
        image_client = _FakeClient([_FakeResponse(200, "image", "image/png")])
        self.assertIsNone(_fetch("https://example.test/a.png", "", client=image_client))

        large = _FakeResponse(200, "x" * (MAX_HTML_BYTES + 1))
        self.assertIsNone(_fetch("https://example.test/large", "", client=_FakeClient([large])))


class ParserTests(unittest.TestCase):
    def test_maps_place_key_prefers_stable_entity_id(self) -> None:
        href = "https://www.google.com/maps/place/Glow+Clinic/data=!4m7!3m6!1s0xabc:0x123!8m2"
        self.assertEqual(_place_key(href), "entity:0xabc:0x123")

    def test_maps_photo_directory_name_is_windows_safe(self) -> None:
        self.assertEqual(_safe_photo_dir_name('美容（A）/:*?"<>|'), "美容（A）________")
        self.assertEqual(_safe_photo_dir_name("CON"), "CON_")
        self.assertEqual(_safe_photo_dir_name("... "), "unknown")

    def test_maps_rejects_google_internal_links_as_merchant_website(self) -> None:
        self.assertTrue(_is_google_internal_host("www.google.cn"))
        self.assertTrue(_is_google_internal_host("maps.google.co.uk"))
        self.assertTrue(_is_google_internal_host("lh3.googleusercontent.com"))
        self.assertFalse(_is_google_internal_host("medskinplus.com"))

    def test_contact_parsers_and_email_filter(self) -> None:
        html = '<a href="tel:2345 6789">Call</a><a href="https://wa.me/85291234567">WA</a>'
        self.assertEqual(_extract_phone(html), "+85223456789")
        self.assertEqual(_extract_whatsapp(html), "+85291234567")
        self.assertTrue(_valid_email("sales@clinic.hk"))
        self.assertFalse(_valid_email("privacy@clinic.hk"))

    def test_bing_redirect_is_decoded(self) -> None:
        href = "https://www.bing.com/ck/a?u=a1aHR0cHM6Ly9leGFtcGxlLmhrLw=="
        self.assertEqual(_resolve_bing_url(href), "https://example.hk/")

    def test_hong_kong_site_accepts_com_domain_with_local_signals(self) -> None:
        self.assertTrue(_is_hong_kong_site("clinic.hk", ""))
        self.assertTrue(_is_hong_kong_site("clinic.com", "Central, Hong Kong +852 2345 6789"))
        self.assertFalse(_is_hong_kong_site("clinic.com", "London +44 20 1234 5678"))

    def test_search_results_filter_non_customer_domains_and_duplicate_hosts(self) -> None:
        urls = _clean_results([
            "https://hk01.com/article/123",
            "https://clinic.com/treatments",
            "https://www.clinic.com/contact",
            "https://en.wikipedia.org/wiki/Medical_spa",
        ])

        self.assertEqual(urls, ["https://clinic.com/treatments"])

    def test_industry_relevance_accepts_clinic_and_rejects_generic_news(self) -> None:
        self.assertGreaterEqual(
            _industry_relevance("glow.com", "Glow 醫學美容中心", "香港 激光脫毛療程"), 3)
        self.assertGreaterEqual(
            _industry_relevance("skinbeam.hk", "Korean Medical Aesthetics Clinic Hong Kong", ""), 3)
        self.assertLess(
            _industry_relevance("example.com", "香港即時新聞", "美容市場新聞報道"), 3)

    @patch("app.crawler.webs_hunter.DuckDuckGoSearch")
    @patch("app.crawler.webs_hunter._search_bing", return_value=["https://bing-clinic.hk"])
    @patch("app.crawler.webs_hunter._search_google", return_value=["https://google-noise.example"])
    def test_auto_search_merges_engines_even_when_google_has_results(self, _google, _bing, ddg) -> None:
        ddg.return_value.search.return_value = []

        self.assertEqual(
            _search("香港 醫學美容", "auto", ""),
            ["https://google-noise.example", "https://bing-clinic.hk"],
        )

    @patch("app.crawler.webs_hunter._search_bing")
    @patch("app.crawler.webs_hunter._search_google")
    @patch("app.crawler.webs_hunter.DuckDuckGoSearch")
    def test_auto_search_prefers_sufficient_ddg_candidates(self, ddg, google, bing) -> None:
        ddg.return_value.search.return_value = [
            MagicMock(url="https://clinic-one.hk"),
            MagicMock(url="https://clinic-two.com"),
            MagicMock(url="https://clinic-three.hk"),
        ]

        urls = _search("medical aesthetic clinic Hong Kong", "auto", "")

        self.assertEqual(len(urls), 3)
        google.assert_not_called()
        bing.assert_not_called()

    @patch("app.crawler.webs_hunter._render_site_pages")
    @patch("app.crawler.webs_hunter._fetch", return_value="<html><title>Clinic</title><div id='app'></div></html>")
    @patch("app.crawler.webs_hunter._new_http_client")
    def test_dynamic_render_fills_contact_missing_from_static_html(self, new_client, _fetch_page, render) -> None:
        client_context = MagicMock()
        client_context.__enter__.return_value = MagicMock()
        new_client.return_value = client_context
        render.return_value = {
            "rendered:/": "<footer>Central, Hong Kong sales@clinic.com +852 2345 6789</footer>"
        }

        result = _crawl_site("clinic.com", "")

        self.assertEqual(result["emails"], ["sales@clinic.com"])
        self.assertEqual(result["phone"], "+85223456789")
        self.assertTrue(result["is_hong_kong"])
        render.assert_called_once()

    @patch("app.crawler.webs_hunter._crawl_site")
    @patch("app.crawler.webs_hunter._search", return_value=["https://old.hk", "https://new.hk"])
    def test_webs_skips_excluded_domain_before_deep_crawl(self, _search_sites, crawl) -> None:
        crawl.return_value = {
            "title": "New Clinic", "emails": ["hello@new.hk"], "phone": "",
            "whatsapp": "", "socials": {}, "instruments": [], "is_hong_kong": True,
            "relevance_score": 4,
        }

        results = hunt_websites(["clinic"], max_customers=1, excluded_domains={"old.hk"})

        self.assertEqual([item["website"] for item in results], ["https://new.hk"])
        crawl.assert_called_once()
        self.assertEqual(crawl.call_args.args[0], "new.hk")


class VisionTests(unittest.TestCase):
    def setUp(self) -> None:
        _cache.clear()

    def test_image_media_type_uses_file_signature(self) -> None:
        self.assertEqual(_image_media_type(b"\x89PNG\r\n\x1a\nrest"), "image/png")
        self.assertEqual(_image_media_type(b"RIFF1234WEBPrest"), "image/webp")
        self.assertEqual(_image_media_type(b"\xff\xd8\xffrest"), "image/jpeg")

    def test_image_suffix_uses_magic_bytes(self) -> None:
        self.assertEqual(_image_suffix(b"\xff\xd8\xffrest"), ".jpg")
        self.assertEqual(_image_suffix(b"\x89PNGrest"), ".png")
        self.assertEqual(_image_suffix(b"RIFF1234WEBPrest"), ".webp")
        self.assertEqual(_image_suffix(b"GIF89arest"), ".gif")
        self.assertEqual(_image_suffix(b"unknown"), ".jpg")

    def test_maps_photo_url_filter_drops_avatar_and_streetview(self) -> None:
        self.assertFalse(_is_candidate_photo_url("https://lh3.googleusercontent.com/a-/avatar=s64"))
        self.assertFalse(_is_candidate_photo_url("https://streetviewpixels-pa.googleapis.com/v1/thumbnail?panoid=abc"))
        self.assertTrue(_is_candidate_photo_url("https://lh5.googleusercontent.com/p/AF1Qip-photo=w408-h306-k-no"))

    def test_local_candidate_filter_drops_tiny_and_flat_avatar(self) -> None:
        from io import BytesIO
        from PIL import Image as PILImage

        def png(width: int, height: int, color: tuple[int, int, int]) -> bytes:
            output = BytesIO()
            PILImage.new("RGB", (width, height), color).save(output, "PNG")
            return output.getvalue()

        self.assertFalse(_candidate_image_check(png(64, 64, (128, 70, 190)))[0])
        self.assertFalse(_candidate_image_check(png(800, 800, (128, 70, 190)))[0])
        self.assertFalse(_candidate_image_check(png(556, 290, (90, 110, 130)))[0])

    def test_perceptual_hash_matches_recompressed_photo(self) -> None:
        from io import BytesIO
        from PIL import Image as PILImage, ImageDraw

        image = PILImage.new("RGB", (640, 480), "#e8e2d7")
        draw = ImageDraw.Draw(image)
        draw.rectangle((70, 80, 560, 390), fill="#4d241d")
        draw.ellipse((220, 130, 420, 330), fill="#d88945")
        versions = []
        for quality in (95, 68):
            output = BytesIO()
            image.save(output, "JPEG", quality=quality)
            versions.append(output.getvalue())
        hashes = [_perceptual_hash(data) for data in versions]

        self.assertIsNotNone(hashes[0])
        self.assertIsNotNone(hashes[1])
        self.assertLessEqual(_hash_distance(hashes[0] or 0, hashes[1] or 0), 10)

    @patch("app.crawler.vision_analyzer._ask_vision")
    def test_vision_saves_downloaded_photos_without_extra_downloads(self, ask) -> None:
        urls = [
            "https://img.test/a", "https://img.test/b",
            "https://img.test/a", "https://img.test/c", "https://img.test/d",
        ]
        photos = {
            urls[0]: b"\xff\xd8\xffjpeg",
            urls[1]: b"\x89PNGpng",
            urls[3]: b"RIFF1234WEBPwebp",
            urls[4]: b"unknown",
        }
        downloaded: list[str] = []
        expected_result = [{"device": "射频仪", "brand": "", "purpose": "紧肤", "confidence": 0.9}]
        ask.return_value = (expected_result, None)

        def downloader(url: str) -> bytes:
            downloaded.append(url)
            return photos[url]

        with tempfile.TemporaryDirectory() as temp_dir:
            result = analyze_photos(
                urls, "key", model="glm-4.6v-flash",
                base_url="https://open.bigmodel.cn/api/paas/v4", provider="glm",
                downloader=downloader, save_dir=temp_dir, enable_failover=False,
            )
            saved = sorted(path.name for path in Path(temp_dir).iterdir())

        clean_urls = [urls[0], urls[1], urls[3], urls[4]]
        expected_files = [
            f"01_{hashlib.sha256(clean_urls[0].encode()).hexdigest()[:6]}.jpg",
            f"02_{hashlib.sha256(clean_urls[1].encode()).hexdigest()[:6]}.png",
            f"03_{hashlib.sha256(clean_urls[2].encode()).hexdigest()[:6]}.webp",
            f"04_{hashlib.sha256(clean_urls[3].encode()).hexdigest()[:6]}.jpg",
        ]
        self.assertEqual(downloaded, clean_urls)
        self.assertEqual(saved, sorted(expected_files))
        self.assertEqual(result, expected_result)
        ask.assert_called_once_with(
            [photos[url] for url in clean_urls], "key", "glm-4.6v-flash", "",
            "https://open.bigmodel.cn/api/paas/v4", verbose=False, provider="glm",
        )

    @patch("app.crawler.vision_analyzer._ask_vision", return_value=([], None))
    def test_vision_write_failure_does_not_interrupt_analysis(self, ask) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            invalid_dir = Path(temp_dir) / "not-a-directory"
            invalid_dir.write_text("occupied", encoding="utf-8")
            result = analyze_photos(
                ["https://img.test/a"], "key",
                downloader=lambda _url: b"\xff\xd8\xffphoto",
                save_dir=str(invalid_dir),
                model="glm-4.6v-flash", base_url="https://open.bigmodel.cn/api/paas/v4",
                provider="glm", enable_failover=False,
            )

        self.assertEqual(result, [])
        ask.assert_called_once()

    def test_vision_items_drop_low_confidence_and_duplicates(self) -> None:
        items = _normalize_items([
            {"device": "皮肤检测仪", "brand": None, "purpose": "检测", "confidence": 0.91},
            {"device": "皮肤检测仪", "brand": "null", "purpose": "重复", "confidence": 0.88},
            {"device": "疑似仪器", "brand": None, "purpose": "不确定", "confidence": 0.3},
            {"device": "射频仪", "brand": "Thermage", "purpose": "紧肤", "confidence": "0.82"},
        ])

        self.assertEqual([item["device"] for item in items], ["皮肤检测仪", "射频仪"])
        self.assertEqual(items[0]["brand"], "")

    @patch("app.crawler.vision_analyzer._ask_vision", return_value=([], None))
    def test_vision_cache_isolated_by_model_and_caches_valid_empty_result(self, ask) -> None:
        downloader = lambda _url: b"\xff\xd8\xffphoto"
        kwargs = {"base_url": "https://open.bigmodel.cn/api/paas/v4", "provider": "glm", "enable_failover": False}
        analyze_photos(["https://img.test/a"], "key", model="glm-4.6v-flash", downloader=downloader, **kwargs)
        analyze_photos(["https://img.test/a"], "key", model="glm-4.6v-flash", downloader=downloader, **kwargs)
        analyze_photos(["https://img.test/a"], "key", model="glm-4.6v", downloader=downloader, **kwargs)

        self.assertEqual(ask.call_count, 2)

    @patch("app.crawler.vision_analyzer._ask_vision", return_value=(None, "timeout"))
    def test_vision_does_not_cache_failed_request(self, ask) -> None:
        downloader = lambda _url: b"\xff\xd8\xffphoto"
        kwargs = {"model": "glm-4.6v-flash", "base_url": "https://open.bigmodel.cn/api/paas/v4", "provider": "glm", "enable_failover": False}
        analyze_photos(["https://img.test/failure"], "key", downloader=downloader, **kwargs)
        analyze_photos(["https://img.test/failure"], "key", downloader=downloader, **kwargs)

        self.assertEqual(ask.call_count, 2)

    @patch("app.crawler.vision_analyzer.time.sleep", return_value=None)
    @patch("app.crawler.vision_analyzer.random.uniform", return_value=0)
    @patch("app.crawler.vision_analyzer.httpx.Client")
    def test_vision_uses_exponential_backoff_for_rate_limit(self, client_class, _random, sleep) -> None:
        limited = MagicMock(status_code=429, headers={}, text="busy")
        success = MagicMock(status_code=200, headers={}, text="ok")
        success.json.return_value = {
            "choices": [{"message": {"content": '{"items": []}'}}]
        }
        post = client_class.return_value.__enter__.return_value.post
        post.side_effect = [limited, limited, success]

        result, error = _ask_vision(
            [b"\xff\xd8\xffphoto"], "key", "glm-4.6v-flash",
            base_url="https://api.test", provider="glm",
        )

        self.assertEqual(result, [])
        self.assertIsNone(error)
        self.assertEqual(post.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])

    @patch("app.crawler.vision_analyzer.httpx.Client")
    def test_qwen_disables_thinking_and_does_not_retry_read_timeout(self, client_class) -> None:
        post = client_class.return_value.__enter__.return_value.post
        post.side_effect = httpx.ReadTimeout("slow response")

        result, error = _ask_vision(
            [b"\xff\xd8\xffphoto"], "key", "qwen3-vl-plus",
            base_url="https://dashscope.test", provider="qwen",
        )

        self.assertIsNone(result)
        self.assertIn("处理超过 90 秒", error or "")
        self.assertEqual(post.call_count, 1)
        self.assertFalse(post.call_args.kwargs["json"]["enable_thinking"])

    @patch("app.crawler.vision_analyzer._ask_vision")
    @patch("app.crawler.vision_analyzer.get_vision_failover_configs")
    @patch("app.crawler.vision_analyzer.get_vision_config")
    def test_vision_fails_over_only_to_configured_provider(self, current, failovers, ask) -> None:
        glm = {"provider": "glm", "api_key": "bad", "model": "glm-4.6v-flash", "base_url": "https://glm.test", "needs_proxy": False}
        qwen = {"provider": "qwen", "api_key": "good", "model": "qwen3-vl-plus", "base_url": "https://qwen.test", "needs_proxy": False}
        current.return_value = glm
        failovers.return_value = [glm, qwen]
        expected = [{"device": "皮肤检测仪", "brand": "", "purpose": "检测", "confidence": 0.9}]
        ask.side_effect = [(None, "429"), (expected, None)]

        result = analyze_photos(
            ["https://img.test/a"], downloader=lambda _url: b"\xff\xd8\xffphoto",
        )

        self.assertEqual(result, expected)
        self.assertEqual([call.kwargs["provider"] for call in ask.call_args_list], ["glm", "qwen"])


class VisionConfigTests(unittest.TestCase):
    @patch("app.config._get", return_value="")
    @patch("app.config.load_config_file", return_value={"vision_provider": "openai", "glm_api_key": "local-key"})
    def test_legacy_openai_config_falls_back_to_glm(self, _load, _env) -> None:
        config = get_vision_config()
        self.assertEqual(config["provider"], "glm")
        self.assertEqual(config["model"], "glm-4.6v-flash")
        self.assertEqual(config["api_key"], "local-key")

    @patch("app.config._get", return_value="")
    @patch("app.config.load_config_file", return_value={
        "vision_provider": "volc",
        "volc_api_key": "volc-key",
        "volc_vision_model": "doubao-seed-1-6-vision-250815",
    })
    def test_retired_volc_model_moves_to_seed_2_lite(self, _load, _env) -> None:
        config = get_vision_config()
        self.assertEqual(config["model"], "doubao-seed-2-0-lite-260428")
        self.assertEqual(config["api_key"], "volc-key")

    @patch("app.config._get", return_value="")
    @patch("app.config.load_config_file", return_value={
        "volc_api_key": "volc-key",
        "volc_vision_model": "doubao-seed-2-0-lite-future-version",
    })
    def test_volc_keeps_arbitrary_manual_model_id(self, _load, _env) -> None:
        config = get_vision_provider_config("volc")
        self.assertEqual(config["model"], "doubao-seed-2-0-lite-future-version")
        self.assertIn("doubao-seed-2-0-mini-260428", VISION_PROVIDERS["volc"]["models"])

    @patch("app.config._get", return_value="")
    @patch("app.config.load_config_file", return_value={
        "vision_provider": "volc",
        "volc_api_key": 'copied "bad key" value',
        "glm_api_key": "valid-glm-key",
    })
    def test_malformed_volc_key_is_skipped_but_valid_fallback_remains(self, _load, _env) -> None:
        self.assertEqual(get_vision_config()["api_key"], "")
        candidates = get_vision_failover_configs("volc")
        self.assertEqual([item["provider"] for item in candidates], ["glm"])


class ResultTargetTests(unittest.TestCase):
    def test_matches_selected_contact_channel(self) -> None:
        email = {"email": "sales@example.hk", "phone": ""}
        phone = {"email": "（WhatsApp 跟进）", "phone": "+85223456789"}

        self.assertTrue(matches_targets(email, ["email"]))
        self.assertFalse(matches_targets(phone, ["email"]))
        self.assertTrue(matches_targets(phone, ["whatsapp"]))
        self.assertTrue(matches_targets(email, ["all"]))

    def test_target_counts_matching_results_not_processed_candidates(self) -> None:
        results = [
            {"email": "", "phone": "+85211111111"},
            {"email": "sales@clinic.hk", "phone": ""},
            {"email": "", "phone": "+85222222222"},
        ]
        email_only = lambda item: matches_targets(item, ["email"])

        self.assertFalse(target_reached(results, 2, email_only))
        self.assertTrue(target_reached(results, 1, email_only))
        self.assertTrue(target_reached(results, 3))


class ProgressTests(unittest.TestCase):
    def test_progress_sink_collects_only_current_context(self) -> None:
        lines: list[str] = []
        with use_progress_sink(lines.append):
            report("first")
            report("second\nthird")

        self.assertEqual(lines, ["first", "second", "third"])


if __name__ == "__main__":
    unittest.main()
