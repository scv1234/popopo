# src/main.py
# uvicorn src.api_server:app --reload --port 8000
# uvicorn src.api_server:app --reload --port 8000 --no-access-log
from __future__ import annotations

import asyncio
import signal
import time
import json
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

logger = structlog.get_logger(__name__)

class MarketMakerBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.running = False
        
        # [핵심] 상태 변경 보호를 위한 Lock
        self.state_lock = asyncio.Lock()

        self._reset_in_progress = False

        # 컴포넌트 초기화
        self.inventory_manager = InventoryManager(
            settings.max_exposure_usd,
            settings.min_exposure_usd,
            settings.target_inventory_balance,
        )
        self.risk_manager = RiskManager(settings, self.inventory_manager)
        self.quote_engine = QuoteEngine(settings, self.inventory_manager)
        self.honeypot_service = HoneypotService(settings)
        self.ws_client = PolymarketWebSocketClient(settings)
        self.order_signer = OrderSigner(settings.private_key)
        self.order_executor = OrderExecutor(settings, self.order_signer)
        self.auto_redeem = AutoRedeem(settings)
        
        # 로컬 상태 변수
        self.orderbooks: dict[str, dict[str, Any]] = {}
        self.open_orders: dict[str, dict[str, Any]] = {}
        self.last_quote_time = 0.0
        self.trade_timestamps = []
        self.manual_order_ids: set[str] = set()
        # 마켓 정보
        self.current_market_id = settings.market_id
        self.yes_token_id = ""
        self.no_token_id = ""
        self.spread_cents = 3
        self.min_size = 20.0
        self.current_tick_size = 0.01 # 기본값

    # =========================================================================
    # 1. Lifecycle & Bootstrap (봇의 시작과 종료)
    # =========================================================================

    async def run(self):
        logger.info("bot_starting")
    
        # 1. 인증 및 초기화
        await self.order_executor.initialize()
        self.running = True

        # 2. 초기 마켓 탐색 (초기 1회는 Lock 없이 동기적으로 설정)
        candidates = await self.honeypot_service.scan()
        if candidates:
            # use_lock=False: 봇 시작 전이므로 락 불필요
            await self._apply_market_target(candidates[0], use_lock=False)
        else:
            logger.warning("no_initial_honeypot_found_waiting_for_loop")

        # 3. 핸들러 등록 및 웹소켓 연결
        self.ws_client.register_handler("l2_book", self._handle_orderbook_update)
        self.ws_client.register_handler("user", self._handle_trade_update)
    
        await self.ws_client.connect()
        
        # 유저 전용 채널 구독 (체결 확인용)
        await self.ws_client.subscribe_user(self.order_executor.safe_address)
        
        # 마켓이 설정되었다면 오더북 구독
        if self.current_market_id:
            if self.yes_token_id: await self.ws_client.subscribe_orderbook(self.yes_token_id)
            if self.no_token_id: await self.ws_client.subscribe_orderbook(self.no_token_id)
            await self.update_orderbook()

        # 4. 병렬 루프 실행 (핵심 태스크 분리)
        tasks = [
            asyncio.create_task(self.run_market_discovery_loop()), # 마켓 탐색
            asyncio.create_task(self.run_cancel_replace_cycle()),  # 주문 집행
            asyncio.create_task(self.run_auto_redeem()),           # 수익 실현
            asyncio.create_task(self.ws_client.listen())           # 소켓 수신
        ]
    
        try:
            await asyncio.gather(*tasks)
        finally:
            await self.cleanup()

    async def cleanup(self):
        logger.info("cleaning_up_resources")
        self.running = False
        if self.current_market_id:
            await self.order_executor.cancel_all_orders(self.current_market_id)
        await self.honeypot_service.close()
        await self.ws_client.close()
        await self.order_executor.close()
        await self.auto_redeem.close()

    async def execute_manual_mint(self, amount: float) -> bool:
        """프론트엔드에서 설정한 금액만큼 USDC를 쪼개서(Split) 무위험 재고를 확보합니다."""
        try:
            # 1. 지갑의 실제 USDC 잔고 확인
            balance = await self.order_executor.get_usdc_balance()
            if balance < amount:
                logger.error("insufficient_usdc_balance", available=balance, requested=amount)
                return False

            # 2. 거래소 컨트랙트를 통해 Split 실행
            success = await self.order_executor.split_assets(amount)
            if success:
                # 3. 봇의 인벤토리 메모리에 반영 (Yes +100, No +100 식)
                self.inventory_manager.record_minting(amount)
                logger.info("manual_minting_completed", amount=amount)
                return True
        except Exception as e:
            logger.error("manual_minting_failed", error=str(e))
        return False

    # =========================================================================
    # 2. Market Discovery & State Management (마켓 탐색 및 상태 변경)
    # =========================================================================

    async def run_market_discovery_loop(self):
        """10분마다 시장 스캔 및 봇 타겟 자동 전환"""
        while self.running:
            try:
                # 스캔 자체는 Lock 없이 수행 (오래 걸림)
                candidates = await self.honeypot_service.scan()
                if candidates:
                    best = candidates[0]
                    if self.current_market_id != best['market_id']:
                        logger.info("switching_market_target", 
                                    old=self.current_market_id, 
                                    new=best['market_id'])
                        # 상태 변경 시에는 Lock 사용
                        await self._apply_market_target(best, use_lock=True)
            except Exception as e:
                logger.error("market_discovery_loop_error", error=str(e))
            
            await asyncio.sleep(600)

    async def _apply_market_target(self, market_data: dict[str, Any], use_lock: bool = True):
        """
        새로운 마켓 정보를 적용합니다.
        use_lock=True일 경우, 쿼트 루프가 접근하지 못하도록 Lock을 겁니다.
        """
        async def _critical_section():
            # 1. 이전 마켓 정리
            old_market_id = self.current_market_id
            if old_market_id:
                await self.order_executor.cancel_all_orders(old_market_id)
                self.open_orders.clear()

            # 2. 로컬 상태 변수 업데이트
            self.current_market_id = market_data['market_id']
            self.yes_token_id = market_data['yes_token_id']
            self.no_token_id = market_data['no_token_id']
            self.min_size = market_data['min_size']
            self.spread_cents = market_data.get('spread_cents', 3)
        
            # 상태 초기화
            self.current_orderbook = {}
            self.last_quote_time = 0.0
            self.risk_manager.is_halted = False 

            # 3. 새로운 마켓 구독 및 데이터 동기화
            if self.ws_client.running:
                if self.yes_token_id: await self.ws_client.subscribe_orderbook(self.yes_token_id)
                if self.no_token_id: await self.ws_client.subscribe_orderbook(self.no_token_id)
                await self.update_orderbook()

            # [핵심] 해당 마켓의 틱 사이즈 정보를 동적으로 가져옴
            try:
                # SDK의 get_tick_size는 문자열(예: "0.01")을 직접 반환합니다.
                tick_size_str = self.order_executor.client.get_tick_size(self.yes_token_id)
                self.current_tick_size = float(tick_size_str)
                logger.info("✅ Market Tick Size Updated", tick=self.current_tick_size)
            except Exception as e:
                # 오타('token_info')가 나지 않도록 직접 tick_size_str 사용
                logger.warning(f"⚠️ Failed to fetch tick size ({e}), using default 0.01")
                self.current_tick_size = 0.01 

        if use_lock:
            async with self.state_lock:
                await _critical_section()
        else:
            await _critical_section()

    async def update_orderbook(self):
        """REST API를 통한 강제 업데이트 시에도 구조 유지"""
        session = await self.honeypot_service.get_session()
        for tid in [self.yes_token_id, self.no_token_id]:
            if not tid: continue
            book = await self.honeypot_service.get_orderbook(session, tid)
            if book and "bids" in book:
                self.orderbooks[tid] = book

    # =========================================================================
    # 3. Event Handlers (웹소켓 데이터 수신)
    # =========================================================================

    def _handle_orderbook_update(self, data: dict[str, Any]):
        """웹소켓 오더북 업데이트 핸들러"""
        asset_id = data.get("asset_id") or data.get("token_id")
    
        # 데이터가 'book' 키 안에 들어있는지, 아니면 루트에 있는지 확인
        book_data = data.get("book") if data.get("book") else data
    
        if asset_id and "bids" in book_data:
            # 오더북 리스트만 안전하게 추출하여 저장
            self.orderbooks[asset_id] = {
                "bids": book_data.get("bids", []),
                "asks": book_data.get("asks", [])
            }
            # 실시간 방어 로직 트리거
            asyncio.create_task(self.check_and_defend_orders())

    def _handle_trade_update(self, data: dict[str, Any]):
        """내 주문 체결 정보 수신"""
        order_id = data.get("order_id")
        actual_price = float(data.get("price", 0))
        size = float(data.get("size", 0))
        token_id = data.get("token_id")
        side = data.get("side")

        # [수정] SELL(매도) 체결 시, QuoteEngine에 판매가 기록 (원금 사수용)
        if side == "SELL":
            token_type = "YES" if token_id == self.yes_token_id else "NO"
            self.quote_engine.update_last_sold_price(token_type, actual_price)
            logger.info(f"recorded_sold_price", type=token_type, price=actual_price)
        
        # 1. 인벤토리 즉시 업데이트
        yes_delta = size if token_id == self.yes_token_id and side == "BUY" else (-size if token_id == self.yes_token_id else 0)
        no_delta = size if token_id == self.no_token_id and side == "BUY" else (-size if token_id == self.no_token_id else 0)
        self.inventory_manager.update_inventory(yes_delta, no_delta)

        # 2. 독성 흐름 감지
        now = time.time()
        self.trade_timestamps.append(now)
        self.trade_timestamps = [t for t in self.trade_timestamps if now - t < 10]
        if len(self.trade_timestamps) >= 5: 
            asyncio.create_task(self.handle_emergency("TOXIC_FLOW_DETECTED", exit_position=False))
            return

        # 3. 방어 로직 예약 (복사한 order_info를 직접 전달)
        if order_id:
            asyncio.create_task(self._defend_after_trade(actual_price, order_id, order_info))

        # 4. 관리 목록 정리 (모든 예약이 끝난 후 삭제)
        if order_id:
            self.manual_order_ids.discard(order_id)
            self.open_orders.pop(order_id, None)

    # =========================================================================
    # 4. Defense Logic (방어 및 리스크 관리)
    # =========================================================================

    async def check_and_defend_orders(self):
        """
        오더북 변화에 따른 실시간 방어.
        Lock을 사용하지 않고 빠르게 판단하되, 조치(Action)가 필요할 때만 개입합니다.
        """
        if self.risk_manager.is_halted: return
        active_tokens = {order.get("token_id") for order in self.open_orders.values() if order.get("token_id")}

        # 두 토큰 각각에 대해 방어 로직 수행
        for token_id in active_tokens:
            book = self.orderbooks.get(token_id)
            if not book: continue

            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if not bids or not asks: continue

            best_bid = float(bids[0]['price'])
            best_ask = float(asks[0]['price'])
            market_spread = best_ask - best_bid
            mid_price = (best_bid + best_ask) / 2.0
            
            spread_usd = self.spread_cents / 100.0
            limit_spread = spread_usd * 2.5

            if market_spread > limit_spread:
                logger.warning(f"market_spread_too_wide for {token_id}", current=market_spread)
                await self._reset_local_market_state()
                return

            for order_id, order in list(self.open_orders.items()):
                if order.get("token_id") != token_id: continue
                
                price_diff = abs(mid_price - float(order.get("price", 0)))
                is_risky = price_diff < (spread_usd * 0.3)
                is_invalid = price_diff > spread_usd
                
                if is_risky or is_invalid:
                    logger.info("defending_manual_order", id=order_id, reason="PRICE_RISK")
                    # 전체 리셋 대신 위험한 해당 주문만 취소하거나, 안전을 위해 전체 취소 실행
                    await self.cancel_single_order(order_id)

    async def _defend_after_trade(self, actual_price: float, order_id: str, order_info: dict = None):
        """체결 직후 리스크 점검 (Circuit Breaker, Hedging)"""
        # 1. 인벤토리 상태 점검 (항상 실행)
        if self.risk_manager.get_inventory_status() == "EMERGENCY":
            await self.handle_emergency("INVENTORY_CRITICAL_SKEW", exit_position=True)
            return

        # 2. 가격 이탈 검증 (Circuit Breaker)
        # [핵심] 이제 self.open_orders가 아니라 전달받은 order_info를 사용합니다.
        if order_info:
            expected_price = float(order_info.get("price", 0))
            side = order_info.get("side", "UNKNOWN")

            if not self.risk_manager.validate_execution_price(expected_price, actual_price, side):
                logger.error("circuit_breaker_halted", order_id=order_id, expected=expected_price, actual=actual_price)
                await self._reset_local_market_state()
                return

        # 3. 델타 뉴트럴 헤징
        hedge_needed = self.risk_manager.calculate_hedge_need()
        if abs(hedge_needed) >= 1.0: 
            await self.execute_auto_hedge(hedge_needed)

    async def handle_emergency(self, reason: str, exit_position: bool = False):
        """[통합 비상 대응]"""
        logger.error("🚨 EMERGENCY_TRIGGERED", reason=reason, exit=exit_position)
        
        # 1. 즉시 중단 플래그 설정 (가장 먼저)
        self.risk_manager.is_halted = True

        try:
            # 2. 주문 취소 (가장 중요)
            if self.current_market_id:
                await self.order_executor.cancel_all_orders(self.current_market_id)
                self.open_orders.clear()

            # 3. 포지션 청산 (옵션)
            if exit_position:
                logger.warning("attempting_liquidation")
                await self._liquidate_all_positions()

            # 4. 쿨다운 후 재개
            asyncio.create_task(self._cool_down_and_resume(30))

        except Exception as e:
            logger.error("emergency_handler_failed", error=str(e))

    async def _reset_local_market_state(self):
        """
        현재 마켓의 주문을 모두 취소하고 로컬 상태를 초기화합니다.
        [개선] 중복 호출 방지 로직 추가
        """
        # 1. 이미 리셋이 진행 중이면 즉시 반환 (API 스팸 방지)
        if self._reset_in_progress:
            return

        try:
            self._reset_in_progress = True
            logger.warning("resetting_market_state_start")

            # 2. 거래소 주문 취소 실행
            # 리스크 상황이므로 수동 주문 여부와 상관없이 해당 마켓의 모든 주문을 거두어들입니다.
            if self.current_market_id:
                await self.order_executor.cancel_all_orders(self.current_market_id)
            
            # 3. [핵심 추가] 로컬 메모리 상태 완전 초기화
            # 오픈 주문 목록과 수동 주문 추적 목록을 모두 비웁니다.
            self.open_orders.clear()
            self.manual_order_ids.clear() 
            
            # 마지막 쿼트 시간을 리셋하여 다음 루프에서 즉시 상태를 점검할 수 있게 합니다.
            self.last_quote_time = 0
            
            logger.info("reset_market_state_complete")

        except Exception as e:
            logger.error("reset_market_state_failed", error=str(e))
        finally:
            # 4. 작업이 끝나면 플래그 해제
            self._reset_in_progress = False

    async def _cool_down_and_resume(self, seconds: int):
        await asyncio.sleep(seconds)
        self.risk_manager.reset_halt()
        self.trade_timestamps.clear()
        logger.info(f"🛡️ Safety cool-down ({seconds}s) finished. Resuming...")

    async def _liquidate_all_positions(self):
        inv = self.inventory_manager.inventory
        if inv.yes_shares > 0:
            await self.order_executor.place_market_order(
                self.current_market_id, "SELL", inv.yes_shares, self.yes_token_id)
        if inv.no_shares > 0:
            await self.order_executor.place_market_order(
                self.current_market_id, "SELL", inv.no_shares, self.no_token_id)

    async def execute_auto_hedge(self, amount: float, aggressive: bool = False):
        try:
            target_token = self.yes_token_id if amount > 0 else self.no_token_id
            target_price = 0.99
            
            if not aggressive:
                session = await self.honeypot_service.get_session()
                book = await self.honeypot_service.get_orderbook(session, target_token)
                target_price = float(book.get("best_ask", 0.99))

            await self.order_executor.place_order({
                "market": self.current_market_id, "side": "BUY", "size": str(abs(amount)),
                "price": str(target_price), "token_id": target_token
            })
        except Exception as e:
            logger.error("hedge_failed", error=str(e))

    # =========================================================================
    # 5. Execution Loop (주문 생성 및 관리)
    # =========================================================================

    async def run_cancel_replace_cycle(self):
        """메인 쿼팅 루프"""
        while self.running:
            try:
                # 할트 상태가 아닐 때만 주문 생성 시도
                if not self.risk_manager.is_halted and self.current_market_id:
                    # [핵심] Lock을 획득하여 마켓이 변경되는 도중에는 주문을 내지 않음
                    async with self.state_lock:
                         await self.refresh_quotes()
                
                # 설정된 간격만큼 대기
                await asyncio.sleep(self.settings.cancel_replace_interval_ms / 1000.0)
            except Exception as e:
                logger.error("quote_loop_error", error=str(e))
                await asyncio.sleep(1)

    async def refresh_quotes(self):
        now_ms = time.time() * 1000
        if (now_ms - self.last_quote_time) < self.settings.quote_refresh_rate_ms: return
        self.last_quote_time = now_ms

        yes_book = self.orderbooks.get(self.yes_token_id, {})
        no_book = self.orderbooks.get(self.no_token_id, {})
        if not yes_book or not no_book: 
            await self.update_orderbook()
            return
        # 변동성 각각 추출
        vol_yes = float(yes_book.get("volatility_1h", 0.005))
        vol_no = float(no_book.get("volatility_1h", 0.005))

        y_bb, y_ba = (float(yes_book['bids'][0]['price']) if yes_book.get('bids') else 0.0), \
                     (float(yes_book['asks'][0]['price']) if yes_book.get('asks') else 1.0)
        n_bb, n_ba = (float(no_book['bids'][0]['price']) if no_book.get('bids') else 0.0), \
                     (float(no_book['asks'][0]['price']) if no_book.get('asks') else 1.0)

        yes_q, no_q = self.quote_engine.generate_quotes(
            self.current_market_id, y_bb, y_ba, n_bb, n_ba,
            self.yes_token_id, self.no_token_id, self.spread_cents, self.min_size,
            self.current_tick_size, 
            yes_vol_1h=vol_yes, no_vol_1h=vol_no # 각각 전달
        )
        await self._cancel_stale_orders()
        
        if yes_quote:
            await self._place_quote(yes_quote, "YES")
        if no_quote:
            await self._place_quote(no_quote, "NO")

    async def _place_quote(self, quote: Any, outcome: str, is_manual: bool = False):
        """
        [수정] 인자에 is_manual을 추가하고, order_id 정의 후 사용하도록 변경
        """
        # Risk Manager 검증
        # [수정] 해당 토큰의 오더북을 참조하여 검증
        token_book = self.orderbooks.get(quote.token_id, {})
        valid, reason = self.risk_manager.validate_order(quote.side, quote.size, token_book)
        
        if not valid:
            logger.debug("quote_skipped", reason=reason, outcome=outcome)
            return False

        try:
            order_data = {
                "market": quote.market, "side": quote.side, "size": str(quote.size),
                "price": str(quote.price), "token_id": quote.token_id, "outcome": outcome
            }
            result = await self.order_executor.place_order(order_data)
            if result and "id" in result: 
                order_id = result["id"]
                self.open_orders[order_id] = order_data
                if is_manual: self.manual_order_ids.add(order_id)
                return True
        except Exception as e:
            logger.error("place_quote_failed", error=str(e))
        return False

    async def _cancel_stale_orders(self):
        """현재 열려있는 모든 주문을 취소 (Batch Cancel 권장)"""
        if self.open_orders:
            # 수동 주문 목록에 없는 ID만 골라냄
            stale_ids = [oid for oid in self.open_orders.keys() if oid not in self.manual_order_ids]
        
            if stale_ids:
                # 전체 취소가 아닌 선택적 일괄 취소 사용
                await self.order_executor.batch_cancel_orders(stale_ids)
                for oid in stale_ids:
                    self.open_orders.pop(oid, None)

    # =========================================================================
    # 6. Helper Services (Auto Redeem & Safety Order)
    # =========================================================================

    async def run_auto_redeem(self):
        while self.running:
            if self.settings.auto_redeem_enabled: 
                try:
                    await self.auto_redeem.auto_redeem_all(self.order_signer.get_address())
                except Exception as e:
                    logger.error("auto_redeem_error", error=str(e))
            await asyncio.sleep(300)

    async def execute_manual_safety_order(self, market_id: str, amount_usd: float, yes_id: str = None, no_id: str = None) -> bool:
        """
        [전체 코드] 시장별 수동/일괄 주문 실행 로직.
        속도 최적화를 위해 병렬 API 호출을 사용하며, 데이터 부재 시 DB 캐시를 활용합니다.
        """
        try:
            session = await self.honeypot_service.get_session()

            # 1. 토큰 ID 복구 (ID가 없을 경우 CLOB API에서 즉시 조회)
            if not yes_id or not no_id:
                try:
                    clob_url = f"{self.honeypot_service.CLOB_API}/markets/{market_id}"
                    async with session.get(clob_url, timeout=5) as res:
                        if res.status == 200:
                            data = await res.json()
                            tokens = data.get("tokens", [])
                            if len(tokens) >= 2:
                                yes_id = next((t['token_id'] for t in tokens if t.get('outcome') == 'Yes'), tokens[0]['token_id'])
                                no_id = next((t['token_id'] for t in tokens if t.get('outcome') == 'No'), tokens[1]['token_id'])
                except Exception as e:
                    logger.error("token_id_recovery_failed", error=str(e))

            # 2. 오더북 데이터 확보 (메모리 -> 병렬 API 호출 -> DB 백업 순)
            yes_book = self.orderbooks.get(yes_id, {}) if yes_id else {}
            no_book = self.orderbooks.get(no_id, {}) if no_id else {}

            # 메모리에 데이터가 없으면 API를 통해 실시간 조회 (병렬 처리로 속도 향상)
            if not yes_book or not no_book:
                tasks = [
                    self.honeypot_service.get_orderbook(session, yes_id),
                    self.honeypot_service.get_orderbook(session, no_id)
                ]
                # return_exceptions=True를 사용하여 하나가 실패해도 다른 하나는 진행
                responses = await asyncio.gather(*tasks, return_exceptions=True)
                
                if isinstance(responses[0], dict) and responses[0]: yes_book = responses[0]
                if isinstance(responses[1], dict) and responses[1]: no_book = responses[1]

            # API 호출마저 실패하거나 타임아웃된 경우 DB 캐시(스캔 데이터)에서 복구
            if not yes_book or not no_book:
                logger.info("falling_back_to_db_cache", market=market_id)
                try:
                    import sqlite3
                    import json
                    conn = sqlite3.connect('bot_data.db')
                    cursor = conn.cursor()
                    cursor.execute("SELECT data FROM honeypots WHERE id = ?", (market_id,))
                    row = cursor.fetchone()
                    conn.close()
                    
                    if row:
                        db_m = json.loads(row[0])
                        mid_y = db_m.get('mid_yes', 0.5)
                        mid_n = db_m.get('mid_no', 0.5)
                        # DB 데이터를 바탕으로 가상의 오더북 생성 (주문 중단 방지)
                        if not yes_book: yes_book = {"bids": [[mid_y - 0.001, 10]], "asks": [[mid_y + 0.001, 10]]}
                        if not no_book: no_book = {"bids": [[mid_n - 0.001, 10]], "asks": [[mid_n + 0.001, 10]]}
                except Exception as db_err:
                    logger.error("db_fallback_failed", error=str(db_err))

            # 최종 데이터 검증
            if not yes_book or not no_book:
                logger.error("order_aborted_no_data", reason="All sources failed")
                return False

            # 3. 최우선 호가 및 변동성 추출 헬퍼
            def get_bb_ba(book):
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                # 데이터 형식이 [[price, size], ...] 이든 [{'price': p, ...}, ...] 이든 대응 가능하게 처리
                bb = float(bids[0][0] if bids and isinstance(bids[0], list) else (bids[0].get('price', 0) if bids else 0.0))
                ba = float(asks[0][0] if asks and isinstance(asks[0], list) else (asks[0].get('price', 1) if asks else 1.0))
                return bb, ba

            y_bb, y_ba = get_bb_ba(yes_book)
            n_bb, n_ba = get_bb_ba(no_book)
            vol_yes = float(yes_book.get("volatility_1h", 0.005))
            vol_no = float(no_book.get("volatility_1h", 0.005))

            # 4. 호가 생성 (Quote Engine 호출)
            yes_quote, no_quote = self.quote_engine.generate_quotes(
                market_id=market_id,
                yes_best_bid=y_bb, yes_best_ask=y_ba,
                no_best_bid=n_bb, no_best_ask=n_ba,
                yes_token_id=yes_id, no_token_id=no_id,
                spread_cents=self.spread_cents,
                min_size_shares=self.min_size,
                tick_size=self.current_tick_size,
                yes_vol_1h=vol_yes, no_vol_1h=vol_no,
                user_input_shares=amount_usd
            )

            # 5. 주문 전송 (병렬 처리로 실행 속도 극대화)
            order_tasks = []
            if yes_quote:
                order_tasks.append(self._place_quote(yes_quote, "YES", is_manual=True))
            if no_quote:
                order_tasks.append(self._place_quote(no_quote, "NO", is_manual=True))
            
            if not order_tasks:
                logger.warning("no_quotes_generated", market=market_id)
                return False

            results = await asyncio.gather(*order_tasks)
            
            # 성공 여부 확인
            success_yes = results[0] if len(results) > 0 and yes_quote else False
            success_no = results[1] if len(results) > 1 and no_quote else False

            if success_yes or success_no:
                logger.info("manual_order_success", market=market_id, yes=success_yes, no=success_no)
                
                # [중요] 주문 성공 즉시 해당 토큰들을 웹소켓 실시간 감시 목록에 추가
                if yes_id: await self.ws_client.subscribe_orderbook(yes_id)
                if no_id: await self.ws_client.subscribe_orderbook(no_id)
                
                return True
            
            logger.error("manual_order_all_failed", market=market_id)
            return False

        except Exception as e:
            logger.error("manual_order_exception", error=str(e))
            return False

    async def cancel_single_order(self, order_id: str) -> bool:
        """특정 ID의 주문을 취소하고 관리 목록에서 제거합니다."""
        logger.info("request_cancel_single_order", id=order_id)
        
        # OrderExecutor를 통해 거래소에 취소 요청
        success = await self.order_executor.cancel_order(order_id)
        
        if success:
            # 관리 목록에서 해당 ID 제거
            self.manual_order_ids.discard(order_id)
            self.open_orders.pop(order_id, None)
            return True
        return False

    async def batch_cancel_manual_orders(self) -> bool:
        """모든 수동 주문을 일괄 취소합니다."""
        if not self.manual_order_ids:
            return True
            
        order_ids = list(self.manual_order_ids)
        logger.info("batch_cancelling_manual_orders", count=len(order_ids))
        
        # OrderExecutor의 batch_cancel_orders(일괄 취소) 사용
        success = await self.order_executor.batch_cancel_orders(order_ids)
        if success:
            for oid in order_ids:
                self.manual_order_ids.discard(oid)
                self.open_orders.pop(oid, None)
            return True
        return False        


# =========================================================================
# 7. Bootstrap
# =========================================================================

async def bootstrap(settings: Settings):
    load_dotenv()
    configure_logging(settings.log_level)
    start_metrics_server(settings.metrics_host, settings.metrics_port)
    
    bot = MarketMakerBot(settings)
    
    # Graceful Shutdown 처리
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    
    def _signal_handler():
        logger.info("shutdown_signal_received")
        bot.running = False
        stop_event.set()
        
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass # 윈도우 등에서 지원 안함

    try:
        await bot.run()
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("bot_shutdown_complete")

if __name__ == "__main__":
    settings = get_settings()
    try:
        asyncio.run(bootstrap(settings))
    except KeyboardInterrupt:

        pass
