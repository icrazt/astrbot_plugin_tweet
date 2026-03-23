from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import aiohttp

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, MessageEventResult, filter
from astrbot.api.star import Context, Star, register


@register(
    "astrbot_plugin_tweet",
    "crazt",
    "基于 RSSHub 的推文转发插件，支持内置 LLM 翻译与 BOOTH 商品信息抓取。",
    "1.0.0",
    "https://github.com/icrazt/astrbot_plugin_tweet",
)
class TweetPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.twitter_link_pattern = re.compile(
            r"https?://(?:x\.com|twitter\.com)/([A-Za-z0-9_]+)/status/(\d+)",
            re.IGNORECASE,
        )
        self.status_link_pattern = re.compile(
            r"(?:x|twitter)\.com/([A-Za-z0-9_]+)/status/(\d+)",
            re.IGNORECASE,
        )
        self.booth_link_pattern = re.compile(
            r"https://(?:[A-Za-z0-9-]+\.)?booth\.pm/(?:[a-z\-]+/)?items/(\d+)",
            re.IGNORECASE,
        )
        self.image_url_pattern = re.compile(
            r"https://pbs\.twimg\.com/(?:media|amplify_video_thumb|ext_tw_video_thumb)/[^\"'<>\\s]+",
            re.IGNORECASE,
        )
        self.video_url_pattern = re.compile(
            r"https://video\.twimg\.com/[^\"'<>\\s]+",
            re.IGNORECASE,
        )

    @filter.event_message_type(filter.EventMessageType.ALL, priority=5)
    async def on_all_message(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()
        if not text:
            return

        tweet_results = await self._handle_tweet_link(event, text)
        if tweet_results is not None:
            event.should_call_llm(False)
            for result in tweet_results:
                result.stop_event()
                yield result
            return

        booth_results = await self._handle_booth_link(event, text)
        if booth_results is not None:
            event.should_call_llm(False)
            for result in booth_results:
                result.stop_event()
                yield result

    async def _handle_tweet_link(
        self,
        event: AstrMessageEvent,
        text: str,
    ) -> Optional[list[MessageEventResult]]:
        command: Optional[str] = None
        content = text
        lowered = text.lower()

        if lowered.startswith("c ") or lowered.startswith("content "):
            command = "content"
            content = text.split(" ", 1)[1] if " " in text else ""
        elif lowered.startswith("o ") or lowered.startswith("origin "):
            command = "origin"
            content = text.split(" ", 1)[1] if " " in text else ""

        match = self.twitter_link_pattern.search(content)
        if not match:
            return None

        user_name = match.group(1)
        tweet_id = match.group(2)
        original_link = match.group(0)

        if user_name.lower() == "i":
            resolved = await self._resolve_twitter_link(tweet_id)
            if not resolved:
                return [event.plain_result("未能解析推文链接。")]
            user_name, original_link = resolved

        rsshub_base_url = self._cfg_str(
            "rsshub_base_url",
            "https://rsshub.app/twitter/user/",
        )
        if not rsshub_base_url:
            return [event.plain_result("插件尚未配置 RSSHub 地址，请联系管理员。")]

        query = self._cfg_str("rsshub_query_param", "")
        if query and not query.startswith("?"):
            query = f"?{query}"
        rss_url = f"{rsshub_base_url.rstrip('/')}/{user_name}/status/{tweet_id}{query}"

        tweet_data = await self._fetch_tweet_data(rss_url, original_link)
        if not tweet_data:
            return [event.plain_result("未能获取该推文。")]

        if command == "content":
            chain = self._build_tweet_content_only(tweet_data)
        else:
            chain = self._build_tweet_original(tweet_data, user_name)

        video_urls = [
            u for u in tweet_data.get("videos", [])
            if self._is_valid_twitter_video_url(u)
        ]
        if video_urls:
            asyncio.create_task(
                self._send_videos_followup(
                    umo=event.unified_msg_origin,
                    video_urls=video_urls,
                )
            )

        if not chain:
            if video_urls:
                chain = [Comp.Plain("检测到视频，正在发送视频消息...")]
            else:
                chain = [Comp.Plain("该推文没有可发送的文本、图片或视频。")]

        if command is None:
            source_text = str(tweet_data.get("text") or "").strip()
            if source_text:
                asyncio.create_task(
                    self._send_translation_followup(
                        umo=event.unified_msg_origin,
                        source_text=source_text,
                    )
                )

        return [event.chain_result(chain)]

    async def _handle_booth_link(
        self,
        event: AstrMessageEvent,
        text: str,
    ) -> Optional[list[MessageEventResult]]:
        match = self.booth_link_pattern.search(text)
        if not match:
            return None

        item_id = match.group(1)
        booth_data = await self._fetch_booth_data(item_id)
        if not booth_data:
            return [event.plain_result("未能获取该 BOOTH 商品信息。")]

        name = str(booth_data.get("name") or "").strip()
        images = self._extract_booth_images(booth_data)

        if not name and not images:
            return [event.plain_result("该 BOOTH 商品没有可发送的信息。")]

        booth_chain = []
        if name:
            booth_chain.append(Comp.Plain(f"{name}\n"))
        for image_url in images:
            try:
                booth_chain.append(Comp.Image.fromURL(image_url))
            except Exception as exc:
                logger.warning(f"tweet plugin: BOOTH 图片构建失败 {image_url} - {exc}")

        if not booth_chain and name:
            return [event.plain_result(name)]
        if not booth_chain:
            return [event.plain_result("该 BOOTH 商品没有可发送的信息。")]

        platform_name = (event.get_platform_name() or "").lower()
        is_onebot_like = ("aiocqhttp" in platform_name) or ("onebot" in platform_name)
        is_group_message = bool(event.get_group_id())

        if len(images) >= 5 and is_group_message and is_onebot_like:
            try:
                node = Comp.Node(
                    uin=event.get_self_id() or "0",
                    name="BOOTH",
                    content=booth_chain,
                )
                return [event.chain_result([node])]
            except Exception as exc:
                logger.warning(f"tweet plugin: BOOTH 合并转发构建失败，降级普通发送: {exc}")

        return [event.chain_result(booth_chain)]

    async def _fetch_tweet_data(
        self,
        rss_url: str,
        original_link: str,
    ) -> Optional[dict[str, Any]]:
        logger.debug(f"tweet plugin: fetching RSS data from {rss_url}")
        timeout = aiohttp.ClientTimeout(total=self._cfg_int("request_timeout_sec", 20))
        headers = {"User-Agent": "astrbot-plugin-tweet/1.0.0"}

        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(rss_url) as response:
                    if response.status >= 400:
                        logger.warning(f"tweet plugin: RSS HTTP {response.status} for {rss_url}")
                        return None
                    xml_text = await response.text()
        except Exception as exc:
            logger.warning(f"tweet plugin: failed to fetch RSS feed: {exc}")
            return None

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            logger.warning(f"tweet plugin: failed to parse RSS XML: {exc}")
            return None

        items = root.findall(".//item")
        if not items:
            return None

        match = self.status_link_pattern.search(original_link)
        if not match:
            return None
        original_user, original_tweet_id = match.groups()

        for item in reversed(items):
            guid = (item.findtext("guid") or "").strip()
            link = (item.findtext("link") or "").strip()
            link_blob = f"{guid} {link}"

            guid_match = self.status_link_pattern.search(link_blob)
            if not guid_match:
                continue

            guid_user, guid_tweet_id = guid_match.groups()
            if (
                guid_user.lower() != original_user.lower()
                or guid_tweet_id != original_tweet_id
            ):
                continue

            raw_content = item.findtext("description") or ""
            text, image_urls, video_urls = self._extract_text_images_videos(raw_content)
            return {
                "text": text,
                "images": image_urls,
                "videos": video_urls,
                "pub_date": item.findtext("pubDate") or "",
                "author": item.findtext("author") or "",
            }
        return None

    async def _resolve_twitter_link(self, tweet_id: str) -> Optional[tuple[str, str]]:
        api_url = f"https://api.vxtwitter.com/i/status/{tweet_id}"
        timeout = aiohttp.ClientTimeout(total=self._cfg_int("request_timeout_sec", 20))
        headers = {"User-Agent": "astrbot-plugin-tweet/1.0.0"}

        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(api_url) as response:
                    if response.status >= 400:
                        logger.warning(
                            f"tweet plugin: failed to resolve i/status link HTTP {response.status}",
                        )
                        return None
                    data = await response.json(content_type=None)
        except Exception as exc:
            logger.warning(f"tweet plugin: failed to resolve tweet link: {exc}")
            return None

        tweet_url = data.get("tweetURL")
        screen_name = data.get("user_screen_name")
        if not tweet_url or not screen_name:
            return None
        return str(screen_name), str(tweet_url)

    async def _fetch_booth_data(self, item_id: str) -> Optional[dict[str, Any]]:
        booth_locale = self._cfg_str("booth_locale", "zh-cn")
        api_url = f"https://booth.pm/{booth_locale}/items/{item_id}.json"
        timeout = aiohttp.ClientTimeout(total=self._cfg_int("request_timeout_sec", 20))
        headers = {"User-Agent": "astrbot-plugin-tweet/1.0.0"}

        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(api_url) as response:
                    if response.status >= 400:
                        logger.warning(
                            f"tweet plugin: BOOTH API HTTP {response.status} for item {item_id}",
                        )
                        return None
                    data = await response.json(content_type=None)
        except Exception as exc:
            logger.warning(f"tweet plugin: failed to fetch BOOTH item: {exc}")
            return None
        if isinstance(data, dict):
            return data
        return None

    async def _send_translation_followup(self, umo: str, source_text: str) -> None:
        try:
            translated_text = await self._translate_text(umo=umo, text=source_text)
            if not translated_text:
                return
            message_chain = MessageChain().message(translated_text)
            await self.context.send_message(umo, message_chain)
        except Exception as exc:
            logger.warning(f"tweet plugin: 发送翻译消息失败: {exc}")

    async def _send_videos_followup(self, umo: str, video_urls: list[str]) -> None:
        for video_url in video_urls:
            try:
                ok = await self.context.send_message(
                    umo,
                    MessageChain(chain=[Comp.Video.fromURL(video_url)]),
                )
                if not ok:
                    logger.warning(f"tweet plugin: 无法发送视频消息，找不到会话: {umo}")
            except Exception as exc:
                logger.warning(f"tweet plugin: 发送视频消息失败 {video_url} - {exc}")
            await asyncio.sleep(0.8)

    async def _translate_text(
        self,
        umo: str,
        text: Optional[str],
    ) -> Optional[str]:
        if not text:
            return None
        if not self._cfg_bool("translate_enabled", True):
            return None

        target_language = self._cfg_str("translate_target_language", "zh-Hans")
        if not target_language:
            return None

        provider_id = self._cfg_str("translate_provider_id", "")
        if not provider_id:
            try:
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            except Exception as exc:
                logger.warning(
                    f"tweet plugin: failed to resolve chat provider for translation: {exc}",
                )
                return None

        raw_text = text.strip()
        if not raw_text:
            return None

        if self._cfg_bool("detect_language_before_translate", False):
            detected_language = await self._detect_language(provider_id, raw_text)
            if detected_language and self._language_matches_target(
                detected_language,
                target_language,
            ):
                return None

        translated_text = await self._request_translation(
            provider_id=provider_id,
            target_language=target_language,
            text=raw_text,
            system_prompt="你是翻译助手。只输出翻译结果，不要解释。",
        )

        if not translated_text or translated_text == raw_text:
            translated_text = await self._request_translation(
                provider_id=provider_id,
                target_language=target_language,
                text=raw_text,
                system_prompt="Translate the given text and output translation only.",
            )

        if not translated_text:
            return None

        return f"{translated_text}"

    async def _request_translation(
        self,
        provider_id: str,
        target_language: str,
        text: str,
        system_prompt: str,
    ) -> Optional[str]:
        prompt = (
            f"请将以下文本翻译为 {target_language}。\n"
            "仅输出翻译结果，不要解释：\n\n"
            f"{text}"
        )
        try:
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                system_prompt=system_prompt,
                prompt=prompt,
            )
            translated_text = (llm_resp.completion_text or "").strip()
            return translated_text or None
        except Exception as exc:
            logger.warning(f"tweet plugin: translation request failed: {exc}")
            return None

    async def _detect_language(
        self,
        provider_id: str,
        text: str,
    ) -> Optional[str]:
        try:
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                system_prompt=(
                    "你是语言识别助手。只返回语言代码，如 en、zh-Hans、ja。"
                ),
                prompt=text,
            )
            response_text = (llm_resp.completion_text or "").strip()
        except Exception as exc:
            logger.warning(f"tweet plugin: language detection failed: {exc}")
            return None

        code_match = re.search(
            r"\b([A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*)\b",
            response_text,
        )
        if not code_match:
            return None
        return code_match.group(1)

    def _build_tweet_original(
        self,
        tweet_data: dict[str, Any],
        user_name: str,
    ) -> list:
        chain = []
        formatted_date = self._format_pub_date(tweet_data.get("pub_date", ""))
        author = str(tweet_data.get("author") or "").strip()
        if formatted_date and author:
            chain.append(Comp.Plain(f"{author}@{user_name} {formatted_date}\n"))

        text = str(tweet_data.get("text") or "").strip()
        if text:
            chain.append(Comp.Plain(f"{text}\n"))

        for image_url in tweet_data.get("images", []):
            try:
                chain.append(Comp.Image.fromURL(image_url))
            except Exception as exc:
                logger.warning(f"tweet plugin: failed to build image component {image_url} - {exc}")
        return chain

    def _build_tweet_content_only(self, tweet_data: dict[str, Any]) -> list:
        chain = []
        for image_url in tweet_data.get("images", []):
            try:
                chain.append(Comp.Image.fromURL(image_url))
            except Exception as exc:
                logger.warning(f"tweet plugin: failed to build image component {image_url} - {exc}")
        return chain

    def _extract_text_images_videos(
        self,
        content: str,
    ) -> tuple[str, list[str], list[str]]:
        content = html.unescape(content or "")

        image_urls = [
            u for u in self._dedup_urls(self.image_url_pattern.findall(content))
            if self._is_valid_twitter_media_url(u)
        ]
        video_urls = [
            u for u in self._dedup_urls(self.video_url_pattern.findall(content))
            if self._is_valid_twitter_video_url(u)
        ]

        cleaned = self.image_url_pattern.sub(" ", content)
        cleaned = self.video_url_pattern.sub(" ", cleaned)
        cleaned = re.sub(r"<video[^>]*>.*?</video>", " ", cleaned, flags=re.IGNORECASE | re.DOTALL)
        cleaned = re.sub(r"<[^>]+>", "\n", cleaned)

        lines = []
        for line in cleaned.splitlines():
            line = re.sub(r"\s+", " ", line).strip()
            if line:
                lines.append(line)
        text = "\n".join(lines)

        return text, image_urls, video_urls

    def _extract_booth_images(self, booth_data: dict[str, Any]) -> list[str]:
        images = booth_data.get("images", [])
        if not isinstance(images, list):
            return []
        urls: list[str] = []
        for image in images:
            if not isinstance(image, dict):
                continue
            original_url = image.get("original")
            if isinstance(original_url, str) and original_url.strip():
                urls.append(original_url.strip())
        return self._dedup_urls(urls)

    def _format_pub_date(self, pub_date: str) -> str:
        if not pub_date:
            return ""
        parsed: Optional[datetime] = None
        for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
            try:
                parsed = datetime.strptime(pub_date, fmt)
                break
            except ValueError:
                continue
        if not parsed:
            return ""
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        east_asia = timezone(timedelta(hours=8))
        return parsed.astimezone(east_asia).strftime("%y-%m-%d %H:%M")

    def _language_matches_target(self, detected: str, target: str) -> bool:
        detected_norm = detected.strip().lower().replace("_", "-")
        target_norm = target.strip().lower().replace("_", "-")
        if not detected_norm or not target_norm:
            return False
        if detected_norm == target_norm:
            return True
        if detected_norm.startswith(target_norm) or target_norm.startswith(detected_norm):
            return True
        if detected_norm.startswith("zh") and target_norm.startswith("zh"):
            return True
        return False

    def _is_valid_twitter_media_url(self, url: str) -> bool:
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        if parsed.scheme not in {"http", "https"}:
            return False
        if parsed.netloc.lower() != "pbs.twimg.com":
            return False
        file_name = parsed.path.rsplit("/", 1)[-1]
        # 截断 URL（如 .../media/HEA）会导致 onebot rich media 上传失败
        if len(file_name) < 6:
            return False
        # 大多数可用媒体链接会带 format 参数或显式扩展名
        if "format=" not in parsed.query and "." not in file_name:
            return False
        return True

    def _is_valid_twitter_video_url(self, url: str) -> bool:
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        if parsed.scheme not in {"http", "https"}:
            return False
        return parsed.netloc.lower() == "video.twimg.com" and bool(parsed.path.strip("/"))

    def _dedup_urls(self, urls: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for url in urls:
            normalized = url.strip().rstrip(".,;!?\"'" )
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result

    def _chunked(self, data: list[str], chunk_size: int) -> list[list[str]]:
        if chunk_size <= 0:
            return [data]
        return [data[i : i + chunk_size] for i in range(0, len(data), chunk_size)]

    def _cfg_str(self, key: str, default: str = "") -> str:
        value = self.config.get(key, default)
        if value is None:
            return default
        return str(value).strip()

    def _cfg_int(self, key: str, default: int) -> int:
        value = self.config.get(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        return bool(value)
