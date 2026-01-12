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
        self.auto_redeem = AutoRedeem(settings)

        self.order_executor = OrderExecutor(settings, self.order_signer)
        self.executor = self.order_executor  # [추가] API 서버 호환성을 위한 별명 설정
        
        # 로컬 상태 변수
        self.orderbooks: dict[str, dict[str, Any]] = {} # 복수형으로 통일
        self.open_orders: dict[str, dict[str, Any]] = {}
        self.last_quote_time = 0.0
        self.trade_timestamps = []
        self.manual_order_ids: set[str] = set()
        # 마켓 정보
        self.current_market_id = settings.market_id
        self.current_condition_id = ""
        self.yes_token_id = ""
        self.no_token_id = ""
        self.spread_cents = 3
        self.min_size = 20.0
        self.current_tick_size = 0.01 # 기본값
        self.current_num_outcomes = 2

    # =========================================================================
    # 1. Lifecycle & Bootstrap (봇의 시작과 종료)
    # =========================================================================

    async def sync_positions_from_chain(self):
        """온체인 잔고를 가져와 인벤토리에 강제 동기화합니다."""
        if not self.yes_token_id or not self.no_token_id:
            return
        
        # OrderExecutor를 통해 실제 잔고 조회
        yes_bal = await self.order_executor.get_token_balance(self.yes_token_id)
        no_bal = await self.order_executor.get_token_balance(self.no_token_id)
        
        # 인벤토리 매니저에 저장
        self.inventory_manager.sync_inventory(yes_bal, no_bal)

    async def run_position_sync_loop(self):
        """지갑 잔고를 주기적으로 감지하여 인벤토리를 최신화합니다."""
        while self.running:
            try:
                await self.sync_positions_from_chain()
            except Exception as e:
                logger.error("position_sync_loop_error", error=str(e))
            await asyncio.sleep(10) # 10초마다 지갑 확인
    
    async def run(self):
        logger.info("bot_starting")
    
        # 1. 인증 및 초기화
        await self.order_executor.initialize()
        self.running = True

        # 2. 초기 마켓 탐색: DB 데이터 로드
        logger.info("loading_initial_market_from_db")
        candidates = await self.honeypot_service.get_cached_candidates() #
        
        if candidates:
            await self._apply_market_target(candidates[0], use_lock=False)
            # [추가] 시작 직후 잔고 즉시 동기화
            await self.sync_positions_from_chain()
        else:
            logger.warning("no_db_records_found_waiting_for_first_scan")

        # 3. 핸들러 등록 및 웹소켓 연결
        self.ws_client.register_handler("l2_book", self._handle_orderbook_update)
        self.ws_client.register_handler("user", self._handle_trade_update)
        await self.ws_client.connect()
        await self.ws_client.subscribe_user(self.order_executor.safe_address)
        
        if self.current_market_id:
            if self.yes_token_id: await self.ws_client.subscribe_orderbook(self.yes_token_id)
            if self.no_token_id: await self.ws_client.subscribe_orderbook(self.no_token_id)
            await self.update_orderbook()

        # 4. 병렬 루프 실행
        tasks = [
            asyncio.create_task(self.run_market_discovery_loop()),
            asyncio.create_task(self.run_position_sync_loop()),    # [수정] 포지션 동기화 루프 추가
            asyncio.create_task(self.run_cancel_replace_cycle()),  
            asyncio.create_task(self.run_auto_redeem()),           
            asyncio.create_task(self.ws_client.listen())           
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
        """저장된 condition_id를 사용하여 자산을 분할(Split)합니다."""
        try:
            if not self.current_condition_id:
                logger.error("minting_failed_no_condition_id")
                return False

            # 1. 지갑의 실제 USDC 잔고 확인
            balance = await self.order_executor.get_usdc_balance()
            if balance < amount:
                logger.error("insufficient_usdc_balance", available=balance, requested=amount)
                return False

            # 2. 거래소 컨트랙트를 통해 Split 실행 (condition_id 전달)
            success = await self.order_executor.split_assets(amount, self.current_condition_id) # [수정]
            if success:
                # 3. 봇의 인벤토리 메모리에 반영
                self.inventory_manager.record_minting(amount)
                logger.info("manual_minting_completed", amount=amount, condition=self.current_condition_id)
                return True
        except Exception as e:
            logger.error("manual_minting_failed", error=str(e))
        return False

    async def execute_manual_merge(self, amount: float) -> bool:
        """Proxy를 통해 Merge를 실행하고 인벤토리를 업데이트함"""
        try:
            if not self.current_condition_id:
                logger.error("merge_failed_no_condition_id")
                return False

            logger.info("starting_proxy_merge", amount=amount, condition=self.current_condition_id)
            success = await self.order_executor.merge_assets(amount, self.current_condition_id)
            
            if success:
                # 인벤토리 차감
                self.inventory_manager.update_inventory(-amount, -amount)
                logger.info("manual_merge_completed", amount=amount)
                return True
            return False
        except Exception as e:
            logger.error("manual_merge_failed", error=str(e))
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
        """새로운 마켓 정보를 적용합니다."""
        async def _critical_section():
            # 1. 이전 마켓 정리 (기존 코드 유지)
            old_market_id = self.current_market_id
            if old_market_id:
                await self.order_executor.cancel_all_orders(old_market_id)
                self.open_orders.clear()

            self.inventory_manager.reset()

            # 2. 로컬 상태 변수 업데이트 (기존 코드 유지)
            self.current_market_id = str(market_data['market_id'])
            self.current_condition_id = market_data.get('condition_id', "") 
            self.current_num_outcomes = market_data.get('num_outcomes', 2)
            self.yes_token_id = market_data['yes_token_id']
            self.no_token_id = market_data['no_token_id']
            self.min_size = market_data.get('min_size', 1.0)
            
            logger.info("🎯 market_target_updated", 
                        title=market_data.get('title'), 
                        market_id=self.current_market_id)
        
            self.orderbooks = {}
            self.last_quote_time = 0.0
            self.risk_manager.is_halted = False 
            self.current_tick_size = 0.01

            # 3. 새로운 마켓 구독 및 데이터 동기화 (기존 코드 유지)
            if self.ws_client.running:
                if self.yes_token_id: await self.ws_client.subscribe_orderbook(self.yes_token_id)
                if self.no_token_id: await self.ws_client.subscribe_orderbook(self.no_token_id)
                await self.update_orderbook()

            try:
                tick_size_str = self.order_executor.client.get_tick_size(self.yes_token_id)
                self.current_tick_size = float(tick_size_str)
                logger.info("✅ Market Tick Size Updated", tick=self.current_tick_size)
            except Exception as e:
                logger.warning(f"⚠️ Failed to fetch tick size ({e}), using default 0.01")
                self.current_tick_size = 0.01 

            # [핵심 수정] 마켓 설정이 완료된 직후 즉시 지갑 잔고를 확인하여 인벤토리에 반영
            await self.sync_positions_from_chain()

        if use_lock:
            async with self.state_lock:
                await _critical_section()
        else:
            await _critical_section()

    def _sort_book(self, orders: list, reverse: bool = False):
        """리스트 형태([p, s])와 딕셔너리 형태({'price': p}) 모두 대응 가능한 정렬 함수"""
        if not orders: return []
        return sorted(
            orders,
            key=lambda x: float(x[0] if isinstance(x, list) else x.get('price', 0)),
            reverse=reverse
        )

    async def update_orderbook(self):
        """REST API를 통한 강제 업데이트 시에도 구조 유지"""
        session = await self.honeypot_service.get_session()
        for tid in [self.yes_token_id, self.no_token_id]:
            if not tid: continue
            book = await self.honeypot_service.get_orderbook(session, tid)
            if book and "bids" in book:
                # 데이터를 저장할 때 정렬하여 저장
                self.orderbooks[tid] = {
                    "bids": self._sort_book(book.get("bids", []), reverse=True),
                    "asks": self._sort_book(book.get("asks", []))
                }

    # =========================================================================
    # 3. Event Handlers (웹소켓 데이터 수신)
    # =========================================================================

    async def _handle_orderbook_update(self, data: dict[str, Any]):
        """안전한 가격 추출 방식을 적용하여 KeyError 방지"""
        asset_id = data.get("asset_id") or data.get("token_id")
        book_data = data.get("book") if data.get("book") else data
    
        if asset_id and "bids" in book_data:
            self.orderbooks[asset_id] = {
                "bids": self._sort_book(book_data.get("bids", []), reverse=True),
                "asks": self._sort_book(book_data.get("asks", []))
            }
            
            # [수정] 인덱스 [0][0] 직접 접근 대신 안전한 함수 사용
            if self.orderbooks[asset_id]["bids"]:
                best_bid = self._extract_price(self.orderbooks[asset_id]["bids"][0])
                self.risk_manager.check_market_danger(best_bid)
            
            asyncio.create_task(self.check_and_defend_orders())

    async def _handle_trade_update(self, data: dict[str, Any]):
        """내 주문 체결 정보 수신 및 전략 상태 업데이트"""
        order_id = data.get("order_id")
        actual_price = float(data.get("price", 0))
        size = float(data.get("size", 0))
        token_id = data.get("token_id")
        side = data.get("side") # "BUY" 또는 "SELL"

        if not token_id or size <= 0:
            return

        # 1. 인벤토리 수량 업데이트 (SELL이면 감소, BUY면 증가)
        # 분할 후 매도 전략에서는 주로 SELL 체결이 발생합니다.
        is_yes = (token_id == self.yes_token_id)
        is_no = (token_id == self.no_token_id)
        
        # 체결 방향에 따른 수량 변화 계산
        multiplier = 1 if side == "BUY" else -1
        yes_delta = size * multiplier if is_yes else 0
        no_delta = size * multiplier if is_no else 0
        
        self.inventory_manager.update_inventory(yes_delta, no_delta)

        # 2. 매도(SELL) 체결 시 'Leg Risk' 방어 데이터 기록
        # 한쪽이 팔리는 순간, 남은 반대쪽은 반드시 (1.0 - 팔린가격) 이상으로 팔아야 원금이 보존됩니다.
        if side == "SELL":
            token_type = "YES" if is_yes else "NO"
            
            # RiskManager에게 한쪽이 팔렸음을 알리고 복구 목표가 설정
            self.risk_manager.set_recovery_target(actual_price)
            
            # QuoteEngine에게 판매가를 전달하여 남은 쪽의 매도 마지노선을 계산하게 함
            self.quote_engine.update_last_sold_price(token_type, actual_price)
            
            logger.info("TRADE_EXECUTED_LEG_SOLD", 
                        token=token_type, 
                        price=actual_price, 
                        recovery_min=round(1.0 - actual_price, 4))

        # 3. 관리 목록 정리
        if order_id:
            self.manual_order_ids.discard(order_id)
            self.open_orders.pop(order_id, None)
            
        # 4. 체결 후 즉시 방어 로직 가동 (남은 주문의 가격이 적절한지 체크)
        asyncio.create_task(self.check_and_defend_orders())

    # =========================================================================
    # 4. Defense Logic (방어 및 리스크 관리)
    # =========================================================================

    async def check_and_defend_orders(self):
        """
        오더북 변화에 따른 실시간 방어.
        파밍 전략에 맞춰 '중간값에 가깝다'는 이유로 주문을 취소하지 않고, 
        '보상 범위를 벗어났거나 본전 사수가 불가능할 때'만 개입합니다.
        """
        if self.risk_manager.is_halted: return
        
        # 현재 열려있는 주문들의 토큰 ID 목록 추출
        active_tokens = {order.get("token_id") for order in self.open_orders.values() if order.get("token_id")}

        for token_id in active_tokens:
            book = self.orderbooks.get(token_id)
            if not book: continue

            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if not bids or not asks: continue

            # 폴리마켓 L2 데이터 구조 [price, size] 대응
            best_bid = self._extract_price(bids[0])
            best_ask = self._extract_price(asks[0])
            mid_price = (best_bid + best_ask) / 2.0
            
            # 리워드를 받을 수 있는 최대 스프레드 범위 (예: 2~3센트)
            reward_spread_usd = self.spread_cents / 100.0

            for order_id, order in list(self.open_orders.items()):
                if order.get("token_id") != token_id: continue
                
                order_price = float(order.get("price", 0))
                price_diff = abs(mid_price - order_price)

                # --- 방어 로직 1: 보상 범위 이탈 (is_invalid) ---
                # 주문이 중간값에서 너무 멀어져 리워드 지급 범위를 벗어났다면 재배치를 위해 취소
                is_out_of_reward_range = price_diff > reward_spread_usd

                # --- 방어 로직 2: 본전 사수 불가능 (Min Recovery Check) ---
                # 만약 한쪽이 팔린 상태(Leg Risk)인데, 시장가가 내 본전 회수 가격보다 낮아졌다면 방어
                is_below_recovery = False
                if self.risk_manager.is_leg_risk_active:
                    # 현재 주문 가격이 본전 마지노선보다 낮다면 즉시 취소
                    if order_price < self.risk_manager.min_recovery_price:
                        is_below_recovery = True

                if is_out_of_reward_range or is_below_recovery:
                    reason = "OUT_OF_REWARD_RANGE" if is_out_of_reward_range else "BELOW_RECOVERY_PRICE"
                    logger.info("defending_order", id=order_id, reason=reason)
                    await self.cancel_single_order(order_id)

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

    def _extract_price(self, order_item):
        if isinstance(order_item, list):
            return float(order_item[0])
        return float(order_item.get('price', 0))            

    async def refresh_quotes(self):
        now_ms = time.time() * 1000
        if (now_ms - self.last_quote_time) < self.settings.quote_refresh_rate_ms: return
        self.last_quote_time = now_ms

        yes_book = self.orderbooks.get(self.yes_token_id, {})
        no_book = self.orderbooks.get(self.no_token_id, {})
        if not yes_book or not no_book: 
            await self.update_orderbook()
            return
        # 최우선 호가 추출
        def get_top_price(book_side, default_price):
            if not book_side: return default_price
            return self._extract_price(book_side[0])

        y_bb = get_top_price(yes_book.get('bids'), 0.5)
        y_ba = get_top_price(yes_book.get('asks'), 0.5)
        n_bb = get_top_price(no_book.get('bids'), 0.5)
        n_ba = get_top_price(no_book.get('asks'), 0.5)
        # [수정] QuoteEngine 인자값 최적화 (불필요한 vol, size 제거)
        yes_quote, no_quote = self.quote_engine.generate_quotes(
            self.current_market_id, 
            y_bb, y_ba, n_bb, n_ba,
            self.yes_token_id, self.no_token_id,
            tick_size=self.current_tick_size
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
        valid, reason = self.risk_manager.validate_order(quote.side, quote.price, token_book)
        
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

    async def execute_optimizer_order(self, market_id: str, amount_usd: float) -> bool:
        """직접 Split 하지 않고, 지갑의 YES/NO 잔고를 감지하여 매도 전략을 실행합니다."""
        try:
            logger.info("🚀 포지션 감지 및 매도 시작", market_id=market_id)
            
            # 1. 마켓 정보 조회 및 타겟 설정
            session = await self.honeypot_service.get_session()
            url = f"{self.honeypot_service.GAMMA_API}?conditionId={market_id}" if market_id.startswith("0x") else f"{self.honeypot_service.GAMMA_API}?id={market_id}" 
            async with session.get(url) as res:
                data = await res.json()
            
            m = next((item for item in data if str(item.get("conditionId", "")).lower() == market_id.lower() or str(item.get("id", "")) == market_id), None)
            if not m: return False

            raw_ids = m.get("clobTokenIds", "[]")
            token_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
            
            await self._apply_market_target({
                'market_id': str(m.get('id')),
                'condition_id': m.get('conditionId'),
                'yes_token_id': token_ids[0],
                'no_token_id': token_ids[1],
                'num_outcomes': len(token_ids),
                'min_size': float(m.get('min_size', 1.0)),
                'title': m.get('question')
            })

            # 2. [중요] 지갑의 실제 잔고 확인 (Sync)
            yes_bal = await self.order_executor.get_token_balance(self.yes_token_id)
            no_bal = await self.order_executor.get_token_balance(self.no_token_id)

            if yes_bal <= 0 and no_bal <= 0:
                logger.warning("⚠️ 감지된 잔고가 없습니다. 웹에서 Split을 먼저 하셨나요?")
                return False

            # 3. 인벤토리에 동기화하여 화면에 표시되게 함
            self.inventory_manager.sync_inventory(yes_bal, no_bal)

            # 4. 즉시 매도 쿼트 생성
            async with self.state_lock:
                await asyncio.sleep(1) # 오더북 대기
                await self.refresh_quotes()
            
            return True
        except Exception as e:
            logger.error("❌ 실행 오류", error=str(e))
            return False

    def _extract_best(self, book):
        """호가 데이터에서 최우선 호가를 안전하게 추출"""
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        bb = self._extract_price(bids[0]) if bids else 0.49
        ba = self._extract_price(asks[0]) if asks else 0.51
        return bb, ba

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