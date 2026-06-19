import asyncio
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from db_manager import db_manager


class RpaDeliveryError(Exception):
    """Base error for RPA delivery failures."""


class RpaDeliverySkipped(RpaDeliveryError):
    """Raised when a delivery candidate is intentionally skipped."""


class RpaDeliverySendUncertain(RpaDeliveryError):
    """Raised when the browser may have sent the message but verification failed."""


@dataclass
class RpaDeliveryConfig:
    enabled: bool = False
    boot_delay_seconds: int = 45
    interval_seconds: int = 120
    max_orders_per_cycle: int = 3
    max_order_age_minutes: int = 1440
    profile_dir: str = "/app/browser_data/rpa_chrome"
    headless: bool = False
    display: str = ":99"
    im_url: str = "https://www.goofish.com/im"
    screenshot_dir: str = "/app/logs/rpa_delivery_screenshots"
    send_timeout_seconds: int = 30
    confirmation_timeout_seconds: int = 8
    only_when_ws_unready: bool = True
    require_buyer_nick: bool = True
    open_browser_on_start: bool = True

    @classmethod
    def from_mapping(cls, raw: Dict[str, Any] = None, *, bool_coercer=None) -> "RpaDeliveryConfig":
        raw = raw or {}

        def as_bool(key: str, default: bool) -> bool:
            value = raw.get(key, default)
            if bool_coercer:
                return bool_coercer(value, default)
            if isinstance(value, bool):
                return value
            if value is None:
                return default
            return str(value).strip().lower() in {"1", "true", "yes", "on"}

        def as_int(key: str, default: int, minimum: int, maximum: int = None) -> int:
            try:
                value = int(raw.get(key, default) or default)
            except (TypeError, ValueError):
                value = default
            value = max(minimum, value)
            if maximum is not None:
                value = min(maximum, value)
            return value

        return cls(
            enabled=as_bool("enabled", False),
            boot_delay_seconds=as_int("boot_delay_seconds", 45, 0, 3600),
            interval_seconds=as_int("interval_seconds", 120, 30, 3600),
            max_orders_per_cycle=as_int("max_orders_per_cycle", 3, 1, 20),
            max_order_age_minutes=as_int("max_order_age_minutes", 1440, 1, 10080),
            profile_dir=str(raw.get("profile_dir") or "/app/browser_data/rpa_chrome"),
            headless=as_bool("headless", False),
            display=str(raw.get("display") or ":99"),
            im_url=str(raw.get("im_url") or "https://www.goofish.com/im"),
            screenshot_dir=str(raw.get("screenshot_dir") or "/app/logs/rpa_delivery_screenshots"),
            send_timeout_seconds=as_int("send_timeout_seconds", 30, 5, 180),
            confirmation_timeout_seconds=as_int("confirmation_timeout_seconds", 8, 2, 60),
            only_when_ws_unready=as_bool("only_when_ws_unready", True),
            require_buyer_nick=as_bool("require_buyer_nick", True),
            open_browser_on_start=as_bool("open_browser_on_start", True),
        )


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_chat_id(value: Any) -> str:
    text = str(value or "").strip()
    if text.endswith("@goofish"):
        return text.split("@", 1)[0].strip()
    return text


def parse_quantity(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 1
    match = re.search(r"\d+", text)
    if not match:
        return 1
    try:
        return max(1, int(match.group(0)))
    except ValueError:
        return 1


def is_order_too_old(order: Dict[str, Any], max_age_minutes: int) -> bool:
    anchor = order.get("platform_paid_at") or order.get("platform_created_at") or order.get("created_at")
    if not anchor:
        return False

    text = str(anchor).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(text[:19], fmt).replace(tzinfo=timezone.utc)
            age_seconds = max(0, (datetime.now(timezone.utc) - dt).total_seconds())
            return age_seconds > max_age_minutes * 60
        except ValueError:
            continue
    return False


def summarize_delivery_rule_meta(delivery_result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "rule_id": delivery_result.get("rule_id"),
        "rule_keyword": delivery_result.get("rule_keyword"),
        "card_type": delivery_result.get("card_type"),
        "match_mode": delivery_result.get("match_mode"),
        "order_spec_mode": delivery_result.get("order_spec_mode"),
        "rule_spec_mode": delivery_result.get("rule_spec_mode"),
        "item_config_mode": delivery_result.get("item_config_mode"),
        "card_id": delivery_result.get("card_id"),
        "card_description": delivery_result.get("card_description"),
        "data_card_pending_consume": delivery_result.get("data_card_pending_consume"),
        "data_line": delivery_result.get("data_line"),
        "data_reservation_id": delivery_result.get("data_reservation_id"),
        "data_reservation_status": delivery_result.get("data_reservation_status"),
        "redeem_code_id": delivery_result.get("redeem_code_id"),
        "redeem_code_batch_id": delivery_result.get("redeem_code_batch_id"),
        "redeem_code_status": delivery_result.get("redeem_code_status"),
        "delivery_unit_index": delivery_result.get("delivery_unit_index") or 1,
    }


def is_supported_text_delivery_steps(delivery_steps: List[Dict[str, Any]]) -> bool:
    if not delivery_steps:
        return False
    for step in delivery_steps:
        if (step or {}).get("type") == "image":
            return False
        if not normalize_text((step or {}).get("content")):
            return False
    return True


class GoofishRpaDeliverySender:
    """Headful browser sender for logged-in Goofish IM pages."""

    INPUT_SELECTORS = (
        "[contenteditable='true'][role='textbox']",
        "[contenteditable='true']",
        "textarea",
        ".ql-editor",
    )
    SEND_SELECTORS = (
        "button:has-text('发送')",
        "button:has-text('Send')",
        "[role='button']:has-text('发送')",
    )
    LOGIN_TEXT_MARKERS = ("登录", "扫码", "密码")

    def __init__(self, cookie_id: str, config: RpaDeliveryConfig):
        self.cookie_id = cookie_id
        self.config = config
        self.playwright = None
        self.context = None
        self.page = None

    async def ensure_started(self):
        if self.page and not self.page.is_closed():
            return

        from playwright.async_api import async_playwright

        if self.config.display and not self.config.headless:
            os.environ.setdefault("DISPLAY", self.config.display)

        Path(self.config.profile_dir).mkdir(parents=True, exist_ok=True)
        Path(self.config.screenshot_dir).mkdir(parents=True, exist_ok=True)

        self.playwright = await async_playwright().start()
        self.context = await self.playwright.chromium.launch_persistent_context(
            self.config.profile_dir,
            headless=self.config.headless,
            viewport={"width": 1600, "height": 950},
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1600,950",
                "--window-position=20,20",
            ],
        )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.page.set_default_timeout(self.config.send_timeout_seconds * 1000)
        await self.page.goto(self.config.im_url, wait_until="domcontentloaded")
        await self.page.wait_for_load_state("networkidle", timeout=15000)
        logger.info(f"【{self.cookie_id}】RPA浏览器已打开闲鱼IM页面")

    async def close(self):
        try:
            if self.context:
                await self.context.close()
        finally:
            self.context = None
            self.page = None
            if self.playwright:
                await self.playwright.stop()
                self.playwright = None

    async def screenshot(self, order_id: str, label: str) -> Optional[str]:
        if not self.page or self.page.is_closed():
            return None
        safe_order = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(order_id or "unknown"))[:80]
        safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(label or "state"))[:40]
        path = Path(self.config.screenshot_dir) / f"{int(time.time())}_{safe_order}_{safe_label}.png"
        try:
            await self.page.screenshot(path=str(path), full_page=False)
            return str(path)
        except Exception as exc:
            logger.debug(f"【{self.cookie_id}】RPA截图失败: {exc}")
            return None

    async def _assert_logged_in(self):
        page_text = normalize_text(await self.page.locator("body").inner_text(timeout=8000))
        lowered = page_text.lower()
        if "goofish" not in lowered and "闲鱼" not in page_text and "消息" not in page_text:
            raise RpaDeliverySkipped("RPA页面不像闲鱼IM，拒绝发送")
        if any(marker in page_text for marker in self.LOGIN_TEXT_MARKERS) and "消息" not in page_text:
            raise RpaDeliverySkipped("RPA浏览器未保持登录态，拒绝发送")

    async def _focus_conversation(self, order: Dict[str, Any]) -> bool:
        buyer_nick = normalize_text(order.get("buyer_nick"))
        buyer_id = normalize_chat_id(order.get("buyer_id"))
        chat_id = normalize_chat_id(order.get("sid"))
        candidates = [value for value in (buyer_nick, buyer_id, chat_id) if value]

        if not candidates:
            return False

        body_text = normalize_text(await self.page.locator("body").inner_text(timeout=8000))
        if any(candidate and candidate in body_text for candidate in candidates):
            for candidate in candidates:
                try:
                    target = self.page.get_by_text(candidate, exact=False).first
                    if await target.count():
                        await target.click(timeout=3000)
                        await self.page.wait_for_timeout(800)
                        return True
                except Exception:
                    continue
            return True

        search_selectors = (
            "input[placeholder*='搜索']",
            "input[placeholder*='search' i]",
            "input[type='search']",
        )
        for selector in search_selectors:
            locator = self.page.locator(selector).first
            try:
                if not await locator.count():
                    continue
                for candidate in candidates:
                    await locator.fill(candidate)
                    await self.page.keyboard.press("Enter")
                    await self.page.wait_for_timeout(1200)
                    updated_text = normalize_text(await self.page.locator("body").inner_text(timeout=8000))
                    if candidate in updated_text:
                        try:
                            await self.page.get_by_text(candidate, exact=False).first.click(timeout=3000)
                        except Exception:
                            pass
                        await self.page.wait_for_timeout(800)
                        return True
            except Exception:
                continue

        return False

    async def _conversation_matches_order(self, order: Dict[str, Any]) -> bool:
        buyer_nick = normalize_text(order.get("buyer_nick"))
        buyer_id = normalize_chat_id(order.get("buyer_id"))
        item_id = normalize_text(order.get("item_id"))
        page_text = normalize_text(await self.page.locator("body").inner_text(timeout=8000))

        if self.config.require_buyer_nick and buyer_nick and buyer_nick in page_text:
            return True
        if buyer_id and buyer_id in page_text:
            return True
        if item_id and item_id in page_text and (buyer_nick or buyer_id):
            return True
        if not self.config.require_buyer_nick and (buyer_nick or buyer_id or item_id):
            return any(value and value in page_text for value in (buyer_nick, buyer_id, item_id))
        return False

    async def _find_input(self):
        for selector in self.INPUT_SELECTORS:
            locator = self.page.locator(selector).last
            try:
                if await locator.count() and await locator.is_visible(timeout=2000):
                    return locator
            except Exception:
                continue
        raise RpaDeliverySkipped("RPA未找到聊天输入框")

    async def _click_send_or_enter(self):
        for selector in self.SEND_SELECTORS:
            locator = self.page.locator(selector).last
            try:
                if await locator.count() and await locator.is_visible(timeout=1000):
                    await locator.click(timeout=3000)
                    return
            except Exception:
                continue
        await self.page.keyboard.press("Enter")

    async def _fill_input(self, input_locator, content: str):
        await input_locator.click(timeout=5000)
        try:
            await input_locator.fill("")
            await input_locator.fill(content)
            return
        except Exception:
            pass

        await self.page.keyboard.press("Control+A")
        await self.page.keyboard.press("Backspace")
        await self.page.keyboard.insert_text(content)

    async def _verify_sent_text_visible(self, content: str) -> bool:
        expected = normalize_text(content)
        if not expected:
            return False
        deadline = time.time() + self.config.confirmation_timeout_seconds
        while time.time() < deadline:
            try:
                body_text = normalize_text(await self.page.locator("body").inner_text(timeout=3000))
                if expected in body_text:
                    return True
                if len(expected) >= 24 and any(part and part in body_text for part in (expected[:24], expected[-24:])):
                    return True
            except Exception:
                pass
            await self.page.wait_for_timeout(500)
        return False

    async def send_text_steps(self, order: Dict[str, Any], delivery_steps: List[Dict[str, Any]]) -> Dict[str, Any]:
        order_id = str(order.get("order_id") or "")
        input_locator = await self.ensure_order_page_ready(order)
        sent_count = 0
        for index, step in enumerate(delivery_steps, start=1):
            content = normalize_text(step.get("content"))
            if not content:
                raise RpaDeliverySkipped(f"RPA发货步骤{index}内容为空")

            await self._fill_input(input_locator, content)
            before_path = await self.screenshot(order_id, f"before_send_{index}")
            await self._click_send_or_enter()
            await self.page.wait_for_timeout(1000)

            if not await self._verify_sent_text_visible(content):
                after_path = await self.screenshot(order_id, f"send_uncertain_{index}")
                raise RpaDeliverySendUncertain(
                    f"RPA已触发发送但页面未能确认文本出现，step={index}, "
                    f"before={before_path or ''}, after={after_path or ''}"
                )
            sent_count += 1
            await self.screenshot(order_id, f"sent_{index}")

        return {
            "success": True,
            "sent_steps": sent_count,
            "channel": "rpa",
        }

    async def ensure_order_page_ready(self, order: Dict[str, Any]):
        """Verify the logged-in browser can safely send to this order's conversation."""
        await self.ensure_started()
        await self._assert_logged_in()

        order_id = str(order.get("order_id") or "")
        if not await self._focus_conversation(order):
            await self.screenshot(order_id, "conversation_not_found")
            raise RpaDeliverySkipped("RPA未能定位到订单买家的会话")

        if not await self._conversation_matches_order(order):
            await self.screenshot(order_id, "conversation_mismatch")
            raise RpaDeliverySkipped("RPA当前会话无法确认属于该订单买家，拒绝发送")

        return await self._find_input()


class RpaDeliveryWorker:
    def __init__(self, live, config: RpaDeliveryConfig):
        self.live = live
        self.config = config
        self.sender: Optional[GoofishRpaDeliverySender] = None

    def _record_log(self, order: Dict[str, Any], status: str, reason: str, rule_meta: Dict[str, Any] = None):
        self.live._record_delivery_log(
            order_id=order.get("order_id"),
            item_id=order.get("item_id"),
            buyer_id=order.get("buyer_id"),
            buyer_nick=order.get("buyer_nick"),
            status=status,
            reason=reason,
            channel="rpa",
            rule_meta=rule_meta,
        )

    def _candidate_orders(self) -> List[Dict[str, Any]]:
        orders = db_manager.get_orders_by_cookie(self.live.cookie_id, limit=200)
        candidates = []
        for order in orders:
            order_id = str(order.get("order_id") or "").strip()
            item_id = str(order.get("item_id") or "").strip()
            buyer_id = str(order.get("buyer_id") or "").strip()
            chat_id = str(order.get("sid") or "").strip()
            status = str(order.get("order_status") or "").strip()

            if status not in {"pending_ship", "pending_delivery", "partial_success", "partial_pending_finalize"}:
                continue
            if not order_id or not item_id or not buyer_id or not chat_id:
                continue
            if is_order_too_old(order, self.config.max_order_age_minutes):
                continue
            if parse_quantity(order.get("quantity")) != 1:
                continue
            if self.live.is_lock_held(order_id) or not self.live.can_auto_delivery(order_id):
                continue
            progress = db_manager.get_delivery_progress_summary(order_id, expected_quantity=1)
            if progress.get("aggregate_status") == "shipped":
                continue
            if progress.get("pending_finalize_unit_indexes") or progress.get("uncertain_unit_indexes"):
                continue
            candidates.append(order)
            if len(candidates) >= self.config.max_orders_per_cycle:
                break
        return candidates

    async def run_once(self) -> Dict[str, int]:
        stats = {"pending": 0, "delivered": 0, "skipped": 0, "failed": 0, "uncertain": 0}

        if self.config.only_when_ws_unready and self.live._is_websocket_ready_for_delivery():
            logger.debug(f"【{self.live.cookie_id}】RPA发货跳过：WebSocket当前可用")
            return stats

        candidates = self._candidate_orders()
        stats["pending"] = len(candidates)
        if not candidates:
            return stats

        if self.sender is None:
            self.sender = GoofishRpaDeliverySender(self.live.cookie_id, self.config)

        for order in candidates:
            result = await self._process_order(order)
            stats[result] = stats.get(result, 0) + 1

        return stats

    async def warmup_browser(self):
        if self.sender is None:
            self.sender = GoofishRpaDeliverySender(self.live.cookie_id, self.config)
        await self.sender.ensure_started()

    async def _process_order(self, order: Dict[str, Any]) -> str:
        order_id = str(order.get("order_id") or "").strip()
        item_id = str(order.get("item_id") or "").strip()
        buyer_id = str(order.get("buyer_id") or "").strip()
        chat_id = str(order.get("sid") or "").strip()
        lock_key = order_id
        order_lock = self.live._order_locks[lock_key]

        async with order_lock:
            if self.live.is_lock_held(lock_key) or not self.live.can_auto_delivery(order_id):
                self._record_log(order, "skipped", "RPA获取锁后发现订单已处理，跳过")
                return "skipped"

            if self.live._check_buyer_blacklist_for_action(
                buyer_id=buyer_id,
                item_id=item_id,
                order_id=order_id,
                buyer_nick=order.get("buyer_nick"),
                action="RPA自动发货",
                channel="rpa",
                log_delivery=True,
            ):
                return "skipped"

            pending_finalize_meta = self.live._get_pending_delivery_finalization_meta(order_id, 1)
            if pending_finalize_meta:
                return await self._finalize_previously_sent(order, pending_finalize_meta)

            try:
                if self.sender is None:
                    self.sender = GoofishRpaDeliverySender(self.live.cookie_id, self.config)
                await self.sender.ensure_order_page_ready(order)

                delivery_result = await self.live._auto_delivery(
                    item_id=item_id,
                    item_title=None,
                    order_id=order_id,
                    send_user_id=buyer_id,
                    chat_id=chat_id,
                    send_user_name=order.get("buyer_nick"),
                    include_meta=True,
                )
                if not isinstance(delivery_result, dict) or not delivery_result.get("success"):
                    reason = (delivery_result or {}).get("error") if isinstance(delivery_result, dict) else "未找到匹配发货内容"
                    self._record_log(order, "skipped", reason or "未找到匹配发货内容")
                    return "skipped"

                delivery_steps = delivery_result.get("delivery_steps") or []
                if not delivery_steps and delivery_result.get("content"):
                    delivery_steps = self.live._build_delivery_steps(
                        delivery_result.get("content"),
                        delivery_result.get("card_description") or "",
                    )
                if not is_supported_text_delivery_steps(delivery_steps):
                    self.live._release_data_reservation_if_needed(delivery_result, error="RPA首版仅支持文本发货步骤")
                    self._record_log(order, "skipped", "RPA首版仅支持文本发货步骤，已跳过", summarize_delivery_rule_meta(delivery_result))
                    return "skipped"

                send_result = await self.sender.send_text_steps(order, delivery_steps)
                if not send_result.get("success"):
                    self.live._release_data_reservation_if_needed(delivery_result, error="RPA发送未成功")
                    self._record_log(order, "failed", "RPA发送未成功", summarize_delivery_rule_meta(delivery_result))
                    return "failed"

                return await self._finalize_sent_order(order, delivery_result)
            except RpaDeliverySkipped as exc:
                self.live._release_data_reservation_if_needed(locals().get("delivery_result"), error=str(exc))
                self._record_log(order, "skipped", str(exc), summarize_delivery_rule_meta(locals().get("delivery_result") or {}))
                return "skipped"
            except RpaDeliverySendUncertain as exc:
                delivery_meta = locals().get("delivery_result") if isinstance(locals().get("delivery_result"), dict) else {}
                self.live._persist_delivery_send_uncertain_state(
                    order_id=order_id,
                    item_id=item_id,
                    buyer_id=buyer_id,
                    delivery_meta=delivery_meta,
                    channel="rpa",
                    last_error=str(exc),
                )
                self.live._activate_delivery_lock(lock_key, delay_minutes=24 * 60)
                self._record_log(order, "failed", str(exc), summarize_delivery_rule_meta(delivery_meta))
                return "uncertain"
            except Exception as exc:
                delivery_meta = locals().get("delivery_result") if isinstance(locals().get("delivery_result"), dict) else {}
                self.live._release_data_reservation_if_needed(delivery_meta, error=str(exc))
                self._record_log(order, "failed", f"RPA自动发货异常: {self.live._safe_str(exc)}", summarize_delivery_rule_meta(delivery_meta))
                logger.error(f"【{self.live.cookie_id}】RPA自动发货异常: order_id={order_id}, error={self.live._safe_str(exc)}")
                return "failed"

    async def _finalize_previously_sent(self, order: Dict[str, Any], delivery_meta: Dict[str, Any]) -> str:
        order_id = str(order.get("order_id") or "")
        item_id = str(order.get("item_id") or "")
        buyer_id = str(order.get("buyer_id") or "")
        finalize_result = await self.live._finalize_delivery_after_send(
            delivery_meta=delivery_meta,
            order_id=order_id,
            item_id=item_id,
        )
        if not finalize_result.get("success"):
            self.live._persist_delivery_finalization_state(
                order_id=order_id,
                item_id=item_id,
                buyer_id=buyer_id,
                delivery_meta=delivery_meta,
                channel="rpa",
                status="sent",
                last_error=finalize_result.get("error") or "RPA补完成finalize失败",
            )
            self._record_log(order, "failed", finalize_result.get("error") or "RPA补完成finalize失败", delivery_meta)
            return "failed"

        self.live._persist_delivery_finalization_state(
            order_id=order_id,
            item_id=item_id,
            buyer_id=buyer_id,
            delivery_meta=delivery_meta,
            channel="rpa",
            status="finalized",
        )
        self.live._sync_order_delivery_progress(order_id, self.live.cookie_id, expected_quantity=1, context="RPA补完成收尾成功")
        self.live._activate_delivery_lock(order_id, delay_minutes=10)
        self._record_log(order, "success", "RPA检测到发货消息已发送，本次补完成收尾成功", delivery_meta)
        return "delivered"

    async def _finalize_sent_order(self, order: Dict[str, Any], delivery_result: Dict[str, Any]) -> str:
        order_id = str(order.get("order_id") or "")
        item_id = str(order.get("item_id") or "")
        buyer_id = str(order.get("buyer_id") or "")

        if not self.live._mark_data_reservation_sent_if_needed(delivery_result):
            self.live._release_data_reservation_if_needed(delivery_result, error="RPA发送成功后标记预占已发送失败")
            raise RuntimeError("RPA发送成功后标记预占已发送失败")

        self.live._persist_delivery_finalization_state(
            order_id=order_id,
            item_id=item_id,
            buyer_id=buyer_id,
            delivery_meta=delivery_result,
            channel="rpa",
            status="sent",
        )

        finalize_result = await self.live._finalize_delivery_after_send(
            delivery_meta=delivery_result,
            order_id=order_id,
            item_id=item_id,
        )
        rule_meta = summarize_delivery_rule_meta(delivery_result)
        if not finalize_result.get("success"):
            self.live._persist_delivery_finalization_state(
                order_id=order_id,
                item_id=item_id,
                buyer_id=buyer_id,
                delivery_meta=delivery_result,
                channel="rpa",
                status="sent",
                last_error=finalize_result.get("error") or "RPA发送成功但提交发货副作用失败",
            )
            self._record_log(order, "failed", finalize_result.get("error") or "RPA发送成功但提交发货副作用失败", rule_meta)
            return "failed"

        self.live._persist_delivery_finalization_state(
            order_id=order_id,
            item_id=item_id,
            buyer_id=buyer_id,
            delivery_meta=delivery_result,
            channel="rpa",
            status="finalized",
        )
        self.live._sync_order_delivery_progress(order_id, self.live.cookie_id, expected_quantity=1, context="RPA自动发货发送成功")
        self.live._activate_delivery_lock(order_id, delay_minutes=10)
        self._record_log(order, "success", "RPA自动发货文本步骤发送成功", rule_meta)
        return "delivered"

    async def close(self):
        if self.sender:
            await self.sender.close()
            self.sender = None
