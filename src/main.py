# src/main.py
from __future__ import annotations

import asyncio
import signal
import time
import json  # [수정] json 임포트 추가
from typing import Any

import logging
import structlog
from dotenv import load_dotenv

from src.config import Settings, get_settings
from src.execution.order_executor import OrderExecutor
from src.inventory.inventory_manager import InventoryManager
from src.logging_config import configure_logging
from src.market_maker.quote_engine import QuoteEngine
from src.polymarket.order_signer import OrderSigner
from src.polymarket.honeypot_service import HoneypotService
from src.polymarket.websocket_client import PolymarketWebSocketClient
from src.risk.risk_manager import RiskManager
from src.services import AutoRedeem, start_metrics_server

# 로깅 설정
logging.basicConfig(level=logging.INFO)
logger = structlog.get_logger(__name__)


class MarketMakerBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.running = False
        self.honeypot_service = HoneypotService(settings) # 서비스 추가
        self.ws_client = PolymarketWebSocketClient(settings)
        self.order_signer = OrderSigner(settings.private_key)
        self.order_executor = OrderExecutor(settings, self.order_signer)
        
        self.inventory_manager = InventoryManager(
            settings.max_exposure_usd,
            settings.min_exposure_usd,
            settings.target_inventory_balance,
        )
        self.risk_manager = RiskManager(settings, self.inventory_manager)
        self.quote_engine = QuoteEngine(settings, self.inventory_manager)
        
        self.auto_redeem = AutoRedeem(settings)
        
        self.current_orderbook: dict[str, Any] = {}
        self.open_orders: dict[str, dict[str, Any]] = {}
        self.last_quote_time = 0.0

        self.yes_token_id = ""
        self.no_token_id = ""

        self._volatility_cache = {}  # 변동성 캐시
        self._last_discovery_time = 0

    async def get_all_candidates_scored(self) -> list[dict[str, Any]]:
        """기존의 복잡한 로직을 HoneypotService로 대체합니다."""
        return await self.honeypot_service.scan_and_score_markets()

    async def execute_manual_safety_order(self, market_id: str, shares: float) -> bool:
        """대시보드 요청에 따라 특정 마켓에 안전 구역 주문을 실행합니다."""
        try:
            market_details = await self.rest_client.get_market(market_id)
            self.yes_token_id = market_details.get("yes_token_id")
            self.no_token_id = market_details.get("no_token_id")
            
            await self.update_orderbook()
            best_bid = float(self.current_orderbook.get("best_bid", 0))
            best_ask = float(self.current_orderbook.get("best_ask", 1))

            yes_quote, no_quote = self.quote_engine.generate_quotes(
                market_id=market_id,
                best_bid=best_bid,
                best_ask=best_ask,
                yes_token_id=self.yes_token_id,
                no_token_id=self.no_token_id,
                max_spread_cents=float(market_details.get("max_incentive_spread", 3.5)),
                min_size_shares=float(market_details.get("min_incentive_size", 100)),
                user_input_shares=shares
            )

            if yes_quote: await self._place_quote(yes_quote, "YES")
            if no_quote: await self._place_quote(no_quote, "NO")
            return True
        except Exception as e:
            logger.error("manual_order_failed", error=str(e))
            return False

    async def calculate_volatility(self, market_id: str) -> float:
        """최근 가격 이력을 가져와 변동성을 계산합니다."""
        history = await self.rest_client.get_price_history(market_id)
        if not history: return 1.0
        prices = [float(p.get("price", 0.5)) for p in history]
        return max(prices) - min(prices)    

    async def discover_market(self) -> dict[str, Any] | None:
        """최고 점수 마켓을 찾아 봇의 세팅을 업데이트합니다."""
        candidates = await self.get_all_candidates_scored()
        if not candidates:
            logger.warning("no_honeypot_found")
            return None
        
        best = candidates[0]
        # 봇 설정 동기화
        self.settings.market_id = best['market_id']
        self.yes_token_id = best['yes_token_id']
        self.no_token_id = best['no_token_id']
        self.settings.min_size = best['min_size']
        
        logger.info("honey_pot_activated", market=best['market_name'], score=best['score'])
        return best

    async def update_orderbook(self):
        try:
            target_id = self.yes_token_id if self.yes_token_id else self.settings.market_id
            orderbook = await self.rest_client.get_orderbook(target_id)
            self.current_orderbook = orderbook
        except Exception as e:
            logger.error("orderbook_update_failed", error=str(e))

    def _handle_orderbook_update(self, data: dict[str, Any]):
        if data.get("market") == self.settings.market_id:
            self.current_orderbook = data.get("book", self.current_orderbook)
            asyncio.create_task(self.check_and_defend_orders())
            
    def _handle_trade_update(self, data: dict[str, Any]):
        side, size, token_id = data.get("side"), float(data.get("size", 0)), data.get("token_id")
        actual_price, order_id = float(data.get("price", 0)), data.get("order_id")
        
        yes_delta = size if token_id == self.yes_token_id and side == "BUY" else (-size if token_id == self.yes_token_id else 0)
        no_delta = size if token_id == self.no_token_id and side == "BUY" else (-size if token_id == self.no_token_id else 0)
        
        self.inventory_manager.update_inventory(yes_delta, no_delta)
        asyncio.create_task(self._defend_after_trade(actual_price, order_id))

    async def _defend_after_trade(self, actual_price: float, order_id: str | None = None):
        status = self.risk_manager.get_inventory_status()
        if status == "EMERGENCY":
            await self._emergency_market_exit()
            return
        if order_id and order_id in self.open_orders:
            order_data = self.open_orders[order_id]
            if not self.risk_manager.validate_execution_price(float(order_data.get("price", 0)), actual_price):
                logger.warning("⛔ CIRCUIT_BREAKER_HALT")
        hedge_needed = self.risk_manager.calculate_hedge_need()
        if abs(hedge_needed) >= 1.0: await self.execute_auto_hedge(hedge_needed)
        if order_id: self.open_orders.pop(order_id, None)

    async def _emergency_market_exit(self):
        await self.order_executor.cancel_all_orders(self.settings.market_id)
        self.open_orders.clear()
        hedge_needed = self.risk_manager.calculate_hedge_need()
        if abs(hedge_needed) >= 1.0: await self.execute_auto_hedge(hedge_needed, aggressive=True) 

    async def execute_auto_hedge(self, amount: float, aggressive: bool = False):
        target_token_id = self.yes_token_id if amount > 0 else self.no_token_id
        target_book = await self.rest_client.get_orderbook(target_token_id)
        target_price = 0.99 if aggressive else float(target_book.get("best_ask", 0.99))
        hedge_order = {"market": self.settings.market_id, "side": "BUY", "size": str(abs(amount)), "price": str(target_price), "token_id": target_token_id}
        try:
            await self.order_executor.place_order(hedge_order)
            logger.info("✅ HEDGE_ORDER_PLACED", price=target_price, size=abs(amount))
        except Exception as e: logger.error("❌ HEDGE_ORDER_FAILED", error=str(e))                      

    async def check_and_defend_orders(self):
        if not self.current_orderbook: return
        mid_price = (float(self.current_orderbook.get("best_bid", 0)) + float(self.current_orderbook.get("best_ask", 1))) / 2.0
        if any(abs(mid_price - float(o.get("price"))) < 0.001 for o in self.open_orders.values()):
            await self.order_executor.cancel_all_orders(self.settings.market_id)
            self.open_orders.clear()
            self.last_quote_time = 0        

    async def refresh_quotes(self, market_info: dict[str, Any]):
        if (time.time() * 1000 - self.last_quote_time) < self.settings.quote_refresh_rate_ms: return
        self.last_quote_time = time.time() * 1000
        if not self.current_orderbook: await self.update_orderbook()
        yes_quote, no_quote = self.quote_engine.generate_quotes(self.settings.market_id, float(self.current_orderbook.get("best_bid", 0)), float(self.current_orderbook.get("best_ask", 1)), self.yes_token_id, self.no_token_id, market_info.get('max_spread', 0.01), self.settings.min_size)
        await self._cancel_stale_orders()
        if yes_quote: await self._place_quote(yes_quote, "YES")
        if no_quote: await self._place_quote(no_quote, "NO")

    async def _cancel_stale_orders(self):
        try:
            open_orders = await self.rest_client.get_open_orders(self.order_signer.get_address(), self.settings.market_id)
            now = time.time() * 1000
            stale_ids = [o.get("id") for o in open_orders if now - o.get("timestamp", 0) > self.settings.order_lifetime_ms]
            if stale_ids:
                await self.order_executor.batch_cancel_orders(stale_ids)
                for oid in stale_ids: self.open_orders.pop(oid, None)
        except Exception as e: logger.error("stale_cancel_failed", error=str(e))

    async def _place_quote(self, quote: Any, outcome: str):
        is_valid, _ = self.risk_manager.validate_order(quote.side, quote.size)
        if not is_valid: return
        try:
            order = {"market": quote.market, "side": quote.side, "size": str(quote.size), "price": str(quote.price), "token_id": quote.token_id}
            result = await self.order_executor.place_order(order)
            if result and "id" in result: self.open_orders[result["id"]] = order
        except Exception as e: logger.error("quote_failed", error=str(e))

    async def run_cancel_replace_cycle(self, market_info: dict[str, Any]):
        while self.running:
            if not self.risk_manager.is_halted:
                await self.refresh_quotes(market_info)
            await asyncio.sleep(self.settings.cancel_replace_interval_ms / 1000.0)

    async def run_auto_redeem(self):
        while self.running:
            if self.settings.auto_redeem_enabled: await self.auto_redeem.auto_redeem_all(self.order_signer.get_address())
            await asyncio.sleep(300)

    async def run(self):
        self.running = True
        market_info = await self.discover_market()
        if not market_info: return
        self.ws_client.register_handler("l2_book", self._handle_orderbook_update)
        self.ws_client.register_handler("user", self._handle_trade_update)
        await self.update_orderbook()
        if self.settings.market_discovery_enabled:
            await self.ws_client.connect()
            await self.ws_client.subscribe_orderbook(self.settings.market_id)
            await self.ws_client.subscribe_user(self.order_signer.get_address())
        tasks = [self.run_cancel_replace_cycle(market_info), self.run_auto_redeem()]
        if self.ws_client.running: tasks.append(self.ws_client.listen())
        try: await asyncio.gather(*tasks)
        finally: await self.cleanup()

    async def cleanup(self):
        self.running = False
        await self.order_executor.cancel_all_orders(self.settings.market_id)
        await self.rest_client.close()
        await self.ws_client.close()
        await self.order_executor.close()
        await self.auto_redeem.close()

async def bootstrap(settings: Settings):
    load_dotenv()
    configure_logging(settings.log_level)
    start_metrics_server(settings.metrics_host, settings.metrics_port)
    bot = MarketMakerBot(settings)
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()
    def _handle_signal():
        bot.running = False
        stop_event.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError: pass
    try: await bot.run()
    finally: logger.info("bot_shutdown")

def main():
    asyncio.run(bootstrap(get_settings()))

if __name__ == "__main__":
    main()