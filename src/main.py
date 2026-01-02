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

#1. 초기화

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

        self.current_spread_cents = 3

        self.yes_token_id = ""
        self.no_token_id = ""
    
    #2. 마켓 탐색

    async def run_market_discovery_loop(self):
        """[통합 루프] 10분마다 시장 스캔 및 봇 타겟 자동 전환"""
        while self.running:
            try:
                logger.info("🔎 주기적 시장 스캔 및 꿀통 탐색 시작...")
                candidates = await self.honeypot_service.scan()
                
                if candidates:
                    best = candidates[0]
                    if self.settings.market_id != best['market_id']:
                        logger.info(f"🔄 최적 마켓 발견, 전환합니다: {best['title']}")
                        await self._reset_local_market_state() # 기존 마켓 주문 취소 및 초기화
                        
                        # 설정 업데이트
                        self.settings.market_id = best['market_id']
                        self.yes_token_id = best['yes_token_id']
                        self.no_token_id = best['no_token_id']
                        self.settings.min_size = best['min_size']
                        self.spread_cents = best.get('spread_cents', 3) # 단위 통일
                        
                        # 웹소켓 재구독
                        await self.ws_client.subscribe_orderbook(self.settings.market_id)
                
            except Exception as e:
                logger.error(f"🚨 탐색 루프 에러: {e}")
            await asyncio.sleep(600)

    async def discover_market(self) -> dict[str, Any] | None:
        """최고 점수 마켓을 찾아 초기 세팅을 완료합니다."""
        candidates = await self.honeypot_service.scan()
        if not candidates:
            logger.warning("no_honeypot_found")
            return None
        
        best = candidates[0]
        self.settings.market_id = best['market_id']
        self.yes_token_id = best['yes_token_id']
        self.no_token_id = best['no_token_id']
        self.settings.min_size = best['min_size']
        
        logger.info("honey_pot_activated", market=best['title'], score=best['score'])
        return best

    async def update_orderbook(self):
        """HoneypotService를 사용하여 오더북 업데이트"""
        target_token = self.yes_token_id if self.yes_token_id else self.settings.market_id
        session = await self.honeypot_service.get_session()
        self.current_orderbook = await self.honeypot_service.get_orderbook(session, target_token)

    #3. 호가창 및 체결 내역 데이터 정리  

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

    #4. 리스크 관리  

    async def _defend_after_trade(self, actual_price: float, order_id: str | None = None):
        """체결 후 인벤토리 상태를 점검하고 필요한 방어 조치를 수행합니다."""
        # 1. 서킷 브레이커 및 비상 상태 체크
        if self.risk_manager.get_inventory_status() == "EMERGENCY":
            return await self._emergency_market_exit()

        # 2. 가격 이탈 검증 (Circuit Breaker)
        if order_id and order_id in self.open_orders:
            expected_price = float(self.open_orders[order_id].get("price", 0))
            if not self.risk_manager.validate_execution_price(expected_price, actual_price):
                logger.error("circuit_breaker_halted_system", order_id=order_id)
            self.open_orders.pop(order_id, None) # 체결된 주문 제거

        # 3. 델타 뉴트럴 헤징
        hedge_needed = self.risk_manager.calculate_hedge_need()
        if abs(hedge_needed) >= 1.0: 
            await self.execute_auto_hedge(hedge_needed)

    async def _emergency_market_exit(self):
        """비상 상황 시 즉시 모든 포지션을 정리하고 시장에서 철수합니다."""
        logger.critical("🚨 EMERGENCY_EXIT_INITIATED")
        
        # 모든 주문 취소와 상태 초기화를 원자적으로 수행
        await self.order_executor.cancel_all_orders(self.settings.market_id)
        self.open_orders.clear()
        
        # 공격적인 시장가 헤징으로 포지션 0화
        hedge_needed = self.risk_manager.calculate_hedge_need()
        if abs(hedge_needed) >= 1.0:
            await self.execute_auto_hedge(hedge_needed, aggressive=True)

    async def check_and_defend_orders(self):
        """실시간 오더북 변화에 따라 주문의 유효성을 검사하고 방어합니다."""
        if not self.current_orderbook: return

        spread_usd = self.current_spread_cents / 100.0

        mid_price = (float(self.current_orderbook.get("best_bid", 0)) + 
                     float(self.current_orderbook.get("best_ask", 1))) / 2.0           

        for order_id, order in list(self.open_orders.items()):
            price_diff = abs(mid_price - float(order.get("price", 0)))
            
            # 방어 트리거 조건 (체결 위험 OR 리워드 실격)
            is_risky = price_diff < (spread_usd * 0.1) # 10% 지점 이내면 위험
            is_invalid = price_diff > spread_usd     # 범위를 벗어나면 실격
            
            if is_risky or is_invalid:
                logger.info("defensive_action_triggered", 
                            reason="RISKY" if is_risky else "INVALID", 
                            diff=round(price_diff, 4),
                            limit_usd=round(spread_usd, 4))
                await self._reset_local_market_state()
                break

    async def _reset_local_market_state(self):
        """현재 마켓의 주문을 모두 취소하고 로컬 상태를 초기화합니다."""
        await self.order_executor.cancel_all_orders(self.settings.market_id)
        self.open_orders.clear()
        self.last_quote_time = 0

    #5. 주문 생성 및 실행   

    async def execute_auto_hedge(self, amount: float, aggressive: bool = False):
        """인벤토리 불균형 해소를 위한 헤징 주문"""
        try:
            target_token = self.yes_token_id if amount > 0 else self.no_token_id
            if aggressive:
                target_price = 0.99
            else:
                book = await self.honeypot_service.get_orderbook(target_token)
                target_price = float(book.get("best_ask", 0.99))

            hedge_order = {
                "market": self.settings.market_id, "side": "BUY", "size": str(abs(amount)),
                "price": str(target_price), "token_id": target_token
            }
            await self.order_executor.place_order(hedge_order)
        except Exception as e:
            logger.error("hedge_failed", error=str(e))

    async def refresh_quotes(self, market_info: dict[str, Any]):
        """최신 가격에 맞춰 MM 주문 갱신"""
        now_ms = time.time() * 1000
        if (now_ms - self.last_quote_time) < self.settings.quote_refresh_rate_ms:
            return
        self.last_quote_time = now_ms

        if not self.current_orderbook: await self.update_orderbook()
        
        self.spread_cents = market_info.get('spread_cents', 3)
        
        yes_q, no_q = self.quote_engine.generate_quotes(
            self.settings.market_id, 
            float(self.current_orderbook.get("best_bid", 0)),
            float(self.current_orderbook.get("best_ask", 1)),
            self.yes_token_id, 
            self.no_token_id,
            self.spread_cents, # 정수형 센트 전달
            self.settings.min_size
        )

        await self._cancel_stale_orders()
        for quote, side in [(yes_q, "YES"), (no_q, "NO")]:
            if quote: await self._place_quote(quote, side)

    async def _place_quote(self, quote: Any, outcome: str):
        """리스크 매니저 승인 후 주문 제출"""
        valid, reason = self.risk_manager.validate_order(quote.side, quote.size)
        if not valid: return

        try:
            order_data = {
                "market": quote.market, "side": quote.side, "size": str(quote.size),
                "price": str(quote.price), "token_id": quote.token_id
            }
            result = await self.order_executor.place_order(order_data)
            if result and "id" in result: self.open_orders[result["id"]] = order_data
        except Exception as e:
            logger.error("placement_failed", error=str(e))

    async def _cancel_stale_orders(self):
        if self.open_orders:
            await self.order_executor.cancel_all_orders(self.settings.market_id)
            self.open_orders.clear()

    async def execute_manual_safety_order(self, market_id: str, shares: float) -> bool:
        """
        대시보드 수동 주문 로직: 
        투자 금액($)을 입력받아 YES/NO 양방향에 안전 유동성을 공급합니다.
        """
        try:
            # 1. 세션 및 마켓 기본 정보(Gamma) 가져오기
            session = await self.honeypot_service.get_session()
            market_details = await self.honeypot_service.get_market(session, market_id)
            
            condition_id = market_details.get("conditionId")
            clob_token_ids = json.loads(market_details.get("clobTokenIds", "[]"))
            
            if not condition_id or len(clob_token_ids) < 2:
                logger.error("invalid_market_data", market_id=market_id)
                return False

            y_id, n_id = clob_token_ids[0], clob_token_ids[1]

            # 2. 리워드 설정 및 오더북 정보 병렬 조회
            tasks = [
                session.get(f"{self.honeypot_service.CLOB_API}/rewards/markets/{condition_id}"),
                self.honeypot_service.get_orderbook(session, y_id)
            ]
            responses = await asyncio.gather(*tasks)
            
            reward_res = responses[0]
            orderbook = responses[1]

            # 3. 리워드 파라미터 추출
            reward_data = {}
            if reward_res.status == 200:
                reward_json = await reward_res.json()
                if reward_json.get("data"):
                    reward_data = reward_json["data"][0]

            local_spread_cents = int(float(reward_data.get("rewards_max_spread", 3)))
            min_size = float(reward_data.get("rewards_min_size", 20))

            # 4. 투자 금액($)을 수량(Shares)으로 매핑
            # 델타 뉴트럴 전략에서 $1000 투자는 YES 1000주 + NO 1000주 공급을 의미합니다.
            target_shares = shares 

            # 5. QuoteEngine을 통한 안전 호가 생성
            yes_quote, no_quote = self.quote_engine.generate_quotes(
                market_id=market_id,
                best_bid=float(orderbook.get("best_bid", 0)),
                best_ask=float(orderbook.get("best_ask", 1)),
                yes_token_id=y_id, 
                no_token_id=n_id,
                spread_cents=local_spread_cents,
                min_size_shares=min_size,
                user_input_shares=shares
            )

            # 6. 최종 주문 실행
            if yes_quote: 
                await self._place_quote(yes_quote, "YES")
            if no_quote: 
                await self._place_quote(no_quote, "NO")

            logger.info("manual_safety_order_executed", market=market_id, amount=amount_usd)
            return True

        except Exception as e:
            logger.error("manual_order_failed", error=str(e))
            return False

    async def run_auto_redeem(self):
        while self.running:
            if self.settings.auto_redeem_enabled: await self.auto_redeem.auto_redeem_all(self.order_signer.get_address())
            await asyncio.sleep(300)        

    #6. 메인루프

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

        # 모든 루프를 병렬 실행
        tasks = [
            asyncio.create_task(self.run_market_discovery_loop()),
            asyncio.create_task(self.run_cancel_replace_cycle(market_info)),
            asyncio.create_task(self.run_auto_redeem())
        ]
        if self.ws_client.running: tasks.append(self.ws_client.listen())
        
        try:
            await asyncio.gather(*tasks)
        finally:
            await self.cleanup()

    async def run_cancel_replace_cycle(self, market_info: dict[str, Any]):
        while self.running:
            try:
                if not self.risk_manager.is_halted:
                    await self.refresh_quotes(market_info)
                await asyncio.sleep(self.settings.cancel_replace_interval_ms / 1000.0)
            except Exception as e:
                logger.error("loop_error", error=str(e))
                await asyncio.sleep(1)

    async def cleanup(self):
        self.running = False
        if self.settings.market_id:
            await self.order_executor.cancel_all_orders(self.settings.market_id)
        await self.honeypot_service.close()
        await self.ws_client.close()
        await self.order_executor.close()
        await self.auto_redeem.close()

#7. 부트스트랩

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

if __name__ == "__main__":
    # [수정] main() 대신 asyncio.run과 bootstrap 호출
    settings = get_settings()
    try:
        asyncio.run(bootstrap(settings))
    except KeyboardInterrupt:
        pass