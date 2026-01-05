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

        self.current_market_id = settings.market_id
        self.yes_token_id = ""
        self.no_token_id = ""
        self.spread_cents = 3
        self.min_size = 20.0
    
    #2. 마켓 탐색

    async def _apply_market_target(self, market_data: dict[str, Any]):
        # 1. 먼저 새로운 마켓 정보를 인스턴스 변수에 저장합니다.
        old_market_id = self.current_market_id
        self.current_market_id = market_data['market_id']
        self.yes_token_id = market_data['yes_token_id']
        self.no_token_id = market_data['no_token_id']
        self.min_size = market_data['min_size']
        self.spread_cents = market_data.get('spread_cents', 3)

        # 2. 이전 마켓이 있었다면 해당 마켓의 주문만 취소합니다.
        if old_market_id:
            await self.order_executor.cancel_all_orders(old_market_id)
            self.open_orders.clear()

        # 3. 새로운 마켓 구독
        await self.ws_client.subscribe_orderbook(self.current_market_id)

    async def run_market_discovery_loop(self):
        """10분마다 시장 스캔 및 봇 타겟 자동 전환"""
        while self.running:
            try:
                candidates = await self.honeypot_service.scan()
                if candidates:
                    best = candidates[0]
                    if self.current_market_id != best['market_id']:
                        await self._apply_market_target(best)
            except Exception as e:
                logger.error("market_discovery_loop_error", error=str(e))
            await asyncio.sleep(600)

    async def update_orderbook(self):
        """HoneypotService를 사용하여 오더북 업데이트"""
        target_token = self.yes_token_id or self.current_market_id
        if not target_token: return
        
        # [수정] 세션을 안전하게 가져와서 전달
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

        # 1. 기준값 설정
        spread_usd = self.spread_cents / 100.0

        # 2. 현재 시장 데이터 계산
        # [수정] 오더북에서 최우선 호가를 더 안전하게 추출
        bids = self.current_orderbook.get("bids", [])
        asks = self.current_orderbook.get("asks", [])
        
        if not bids or not asks:
            return

        best_bid = float(bids[0]['price'])
        best_ask = float(asks[0]['price'])
        market_spread = best_ask - best_bid  # 시장 스프레드
        
        mid_price = (best_bid + best_ask) / 2.0

        # [수정] 🚨 시장 스프레드 과다 이격 방어 (HoneypotService와 동기화)
        # 빈집 마켓 공략을 위해 리워드 스프레드의 2.5배까지 허용합니다.
        limit_spread = spread_usd * 2.5
        
        if market_spread > limit_spread:
            logger.warning("market_spread_too_wide_defense", 
                           current_spread=round(market_spread, 4), 
                           limit=round(limit_spread, 4),
                           message="Spread exceeds 2.5x of reward spread. Retreating...")
            
            # 모든 주문 취소 및 관망
            await self._reset_local_market_state()
            return

        # 3. 개별 주문 위치 방어 (기존 로직)
        for order_id, order in list(self.open_orders.items()):
            price_diff = abs(mid_price - float(order.get("price", 0)))

            safe_buffer = max(spread_usd * 0.1, 0.002)
            # 방어 트리거 조건 (체결 위험 OR 리워드 실격)
            is_risky = price_diff < safe_buffer # 10% 지점 이내면 위험
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
            target_price = 0.99
            
            if not aggressive:
                # [수정] 세션 인자 추가 전달
                session = await self.honeypot_service.get_session()
                book = await self.honeypot_service.get_orderbook(session, target_token)
                target_price = float(book.get("best_ask", 0.99))

            hedge_order = {
                "market": self.current_market_id, "side": "BUY", "size": str(abs(amount)),
                "price": str(target_price), "token_id": target_token
            }
            await self.order_executor.place_order(hedge_order)
        except Exception as e:
            logger.error("hedge_failed", error=str(e))

    async def refresh_quotes(self):
        if not self.current_market_id: return
        
        now_ms = time.time() * 1000
        if (now_ms - self.last_quote_time) < self.settings.quote_refresh_rate_ms:
            return
        self.last_quote_time = now_ms

        await self.update_orderbook()

        vol_1h = float(self.current_orderbook.get("volatility_1h", 0.005))
        
        yes_q, no_q = self.quote_engine.generate_quotes(
            market_id=self.current_market_id, 
            best_bid=float(self.current_orderbook.get("best_bid", 0)),
            best_ask=float(self.current_orderbook.get("best_ask", 1)),
            yes_token_id=self.yes_token_id, 
            no_token_id=self.no_token_id,
            spread_cents=self.spread_cents,
            min_size_shares=self.min_size,
            volatility_1h=vol_1h     # 전달
        )

        await self._cancel_stale_orders()
        for quote, side in [(yes_q, "YES"), (no_q, "NO")]:
            if quote: await self._place_quote(quote, side)

    async def _place_quote(self, quote: Any, outcome: str):
        """리스크 매니저 승인 후 주문 제출"""
        valid, reason = self.risk_manager.validate_order(quote.side, quote.size)
        if not valid:
            # [추가] 거절 사유를 로그에 남겨서 확인 가능하게 함
            logger.warning("order_rejected_by_risk_manager", 
                           outcome=outcome, 
                           reason=reason, 
                           size=quote.size)
            return False

        try:
            order_data = {
                "market": quote.market, "side": quote.side, "size": str(quote.size),
                "price": str(quote.price), "token_id": quote.token_id
            }
            # [디버깅 로그 추가]
            logger.info("attempting_to_place_order", side=quote.side, price=quote.price)
            result = await self.order_executor.place_order(order_data)
            if result and "id" in result: 
                self.open_orders[result["id"]] = order_data
                return True
        except Exception as e:
            logger.error("placement_failed", error=str(e))
        return False    

    async def _cancel_stale_orders(self):
        if self.open_orders:
            await self.order_executor.cancel_all_orders(self.settings.market_id)
            self.open_orders.clear()

    async def execute_manual_safety_order(self, market_id: str, amount_usd: float, yes_id: str = None, no_id: str = None) -> bool:
        """
        [수정됨] session.get을 올바르게 처리하여 리워드 정보와 호가창을 가져옵니다.
        """
        try:
            session = await self.honeypot_service.get_session()
            
            # 1. Token ID 누락 시 Fallback (CLOB API 조회)
            if not yes_id or not no_id:
                logger.info("fetching_missing_token_ids", market_id=market_id)
                clob_url = f"{self.honeypot_service.CLOB_API}/markets/{market_id}"
                async with session.get(clob_url) as res:
                    if res.status != 200:
                        logger.error("market_not_found_clob", status=res.status)
                        return False
                    data = await res.json()
                    tokens = data.get("tokens", [])
                    if len(tokens) >= 2:
                        yes_id = next((t['token_id'] for t in tokens if t.get('outcome') == 'Yes'), tokens[0]['token_id'])
                        no_id = next((t['token_id'] for t in tokens if t.get('outcome') == 'No'), tokens[1]['token_id'])
                    else:
                        logger.error("tokens_empty")
                        return False

            # [핵심 수정] session.get을 안전하게 수행하는 내부 헬퍼 함수 정의
            async def fetch_json(url):
                try:
                    async with session.get(url) as res:
                        if res.status == 200:
                            return await res.json()
                except Exception:
                    pass
                return {}

            # 2. 병렬 데이터 조회 (수정된 fetch_json 사용)
            # - 리워드 정보: Condition ID 사용
            # - 호가창: YES Token ID 사용
            tasks = [
                fetch_json(f"{self.honeypot_service.CLOB_API}/rewards/markets/{market_id}"),
                self.honeypot_service.get_orderbook(session, yes_id)
            ]
            
            responses = await asyncio.gather(*tasks)
            reward_json = responses[0]
            orderbook = responses[1]

            # [핵심 수정] 리스트에서 직접 Best Bid/Ask 추출
            # CLOB API 결과는 {"bids": [{"price": "0.5", "size": "100"}, ...], "asks": [...]} 형태임
            bids = orderbook.get("bids", [])
            asks = orderbook.get("asks", [])
            
            # 1. 명시적 정렬: API 응답이 정렬되어 있지 않을 경우를 대비함
            # 매수(Bids): 가장 높은 가격이 위로 (내림차순)
            if bids:
                bids.sort(key=lambda x: float(x['price']), reverse=True)
            # 매도(Asks): 가장 낮은 가격이 위로 (오름차순)
            if asks:
                asks.sort(key=lambda x: float(x['price']))

            # 2. 최우선 호가(Best Bid/Ask) 추출
            # 정렬된 리스트의 0번째가 항상 가장 유리한 가격입니다.
            best_bid = float(bids[0]['price']) if bids else 0.0
            best_ask = float(asks[0]['price']) if asks else 1.0

            # 3. 리워드 파라미터 추출
            local_spread_cents = 3
            min_size = 20.0
            
            if reward_json and reward_json.get("data"):
                r_data = reward_json["data"][0]
                local_spread_cents = int(float(r_data.get("rewards_max_spread", 3)))
                min_size = float(r_data.get("rewards_min_size", 20))

            # 4. 호가 생성
            yes_quote, no_quote = self.quote_engine.generate_quotes(
                market_id=market_id,
                best_bid=best_bid,
                best_ask=best_ask,
                yes_token_id=yes_id, 
                no_token_id=no_id,
                spread_cents=local_spread_cents,
                min_size_shares=min_size,
                user_input_shares=amount_usd
            )

            # 5. 주문 전송
            success_yes = False
            success_no = False

            if yes_quote: 
                success_yes = await self._place_quote(yes_quote, "YES")
            else:
                # 호가 생성 실패 시 원인 로그 (디버깅용)
                logger.warning("quote_gen_failed_yes", bid=best_bid, ask=best_ask, mid=(best_bid+best_ask)/2)

            if no_quote: 
                success_no = await self._place_quote(no_quote, "NO")

            if success_yes or success_no:
                logger.info("manual_safety_order_executed", market=market_id)
                return True
            else:
                logger.error("manual_order_all_failed", market=market_id)
                return False

        except Exception as e:
            logger.error("manual_order_exception", error=str(e))
            return False

    async def run_auto_redeem(self):
        while self.running:
            if self.settings.auto_redeem_enabled: await self.auto_redeem.auto_redeem_all(self.order_signer.get_address())
            await asyncio.sleep(300)        

    #6. 메인루프

    async def run(self):
        logger.info("bot_starting")
    
        # 1. 인증 및 초기화
        await self.order_executor.initialize()
        self.running = True

        # 2. 초기 마켓 탐색 (초기 1회만 동기식으로 실행하여 타겟 설정)
        candidates = await self.honeypot_service.scan()
        if candidates:
            await self._apply_market_target(candidates[0])
        else:
            logger.warning("no_initial_honeypot_found_waiting_for_loop")

        # 3. 핸들러 등록 및 웹소켓 연결
        self.ws_client.register_handler("l2_book", self._handle_orderbook_update)
        self.ws_client.register_handler("user", self._handle_trade_update)
    
        await self.ws_client.connect()
        await self.ws_client.subscribe_user(self.order_signer.get_address())
        # 초기 마켓이 설정되었다면 구독 시도
        if self.current_market_id:
            await self.ws_client.subscribe_orderbook(self.current_market_id)

        # 4. 병렬 루프 실행
        tasks = [
            asyncio.create_task(self.run_market_discovery_loop()),
            asyncio.create_task(self.run_cancel_replace_cycle()),
            asyncio.create_task(self.run_auto_redeem())
        ]
        if self.ws_client.running:
            tasks.append(self.ws_client.listen())
    
        try:
            await asyncio.gather(*tasks)
        finally:
            await self.cleanup()

    async def run_cancel_replace_cycle(self):
        while self.running:
            try:
                if not self.risk_manager.is_halted:
                    await self.refresh_quotes()
                await asyncio.sleep(self.settings.cancel_replace_interval_ms / 1000.0)
            except Exception as e:
                logger.error("quote_loop_error", error=str(e))
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


