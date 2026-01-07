# src/main.py
# uvicorn src.api_server:app --reload --port 8000
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
logging.basicConfig(level=logging.INFO)
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
        self.current_orderbook: dict[str, Any] = {}
        self.open_orders: dict[str, dict[str, Any]] = {}
        self.last_quote_time = 0.0
        self.trade_timestamps = []

        # 마켓 정보
        self.current_market_id = settings.market_id
        self.yes_token_id = ""
        self.no_token_id = ""
        self.spread_cents = 3
        self.min_size = 20.0

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
        await self.ws_client.subscribe_user(self.order_signer.get_address())
        
        # 마켓이 설정되었다면 오더북 구독
        if self.current_market_id:
            await self.ws_client.subscribe_orderbook(self.current_market_id)
            await self.update_orderbook() # 초기 데이터 로드

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
                # 이전 마켓 주문 취소
                await self.order_executor.cancel_all_orders(old_market_id)
                self.open_orders.clear()
                # (선택 사항) 웹소켓 구독 해제 로직이 있다면 여기서 호출

            # 2. 로컬 상태 변수 업데이트 (Atomic에 가깝게)
            self.current_market_id = market_data['market_id']
            self.yes_token_id = market_data['yes_token_id']
            self.no_token_id = market_data['no_token_id']
            self.min_size = market_data['min_size']
            self.spread_cents = market_data.get('spread_cents', 3)
            
            # 상태 초기화
            self.current_orderbook = {}
            self.last_quote_time = 0.0
            self.risk_manager.is_halted = False # 새 마켓에서는 리셋

            # 3. 새로운 마켓 구독
            if self.ws_client.running: # 연결된 상태라면
                await self.ws_client.subscribe_orderbook(self.current_market_id)
                await self.update_orderbook() # 스냅샷 즉시 로드 시도

        if use_lock:
            # Lock을 획득하여 다른 루프(cancel_replace)가 끼어들지 못하게 함
            async with self.state_lock:
                await _critical_section()
        else:
            await _critical_section()

    async def update_orderbook(self):
        """REST API를 통해 오더북 스냅샷을 가져옵니다 (초기화/리프레시용)"""
        target_token = self.yes_token_id or self.current_market_id
        if not target_token: return
        
        try:
            session = await self.honeypot_service.get_session()
            self.current_orderbook = await self.honeypot_service.get_orderbook(session, target_token)
        except Exception as e:
            logger.error("update_orderbook_failed", error=str(e))

    # =========================================================================
    # 3. Event Handlers (웹소켓 데이터 수신)
    # =========================================================================

    def _handle_orderbook_update(self, data: dict[str, Any]):
        """실시간 오더북 업데이트 수신"""
        # 현재 마켓 데이터가 아니면 무시 (Race Condition 방지)
        if data.get("market") != self.current_market_id:
            return

        self.current_orderbook = data.get("book", self.current_orderbook)
        
        # 방어 로직은 메인 루프를 막지 않기 위해 비동기 Task로 실행
        asyncio.create_task(self.check_and_defend_orders())

    def _handle_trade_update(self, data: dict[str, Any]):
        """내 주문 체결 정보 수신"""
        side, size = data.get("side"), float(data.get("size", 0))
        token_id = data.get("token_id")
        actual_price = float(data.get("price", 0))
        order_id = data.get("order_id")
        
        # 1. 인벤토리 즉시 업데이트
        yes_delta = size if token_id == self.yes_token_id and side == "BUY" else (-size if token_id == self.yes_token_id else 0)
        no_delta = size if token_id == self.no_token_id and side == "BUY" else (-size if token_id == self.no_token_id else 0)
        self.inventory_manager.update_inventory(yes_delta, no_delta)

        # 2. 독성 흐름(Toxic Flow) 감지
        now = time.time()
        self.trade_timestamps.append(now)
        # 10초 이내 체결만 유지
        self.trade_timestamps = [t for t in self.trade_timestamps if now - t < 10]
        
        if len(self.trade_timestamps) >= 5: 
            # 긴급 방어 트리거
            asyncio.create_task(self.handle_emergency("TOXIC_FLOW_DETECTED", exit_position=False))
            return

        # 3. 사후 방어 로직 (Slippage 체크 및 Auto-Hedge)
        asyncio.create_task(self._defend_after_trade(actual_price, order_id))

    # =========================================================================
    # 4. Defense Logic (방어 및 리스크 관리)
    # =========================================================================

    async def check_and_defend_orders(self):
        """
        오더북 변화에 따른 실시간 방어.
        Lock을 사용하지 않고 빠르게 판단하되, 조치(Action)가 필요할 때만 개입합니다.
        """
        if self.risk_manager.is_halted: return
        if not self.current_orderbook: return

        # 1. 마켓 스프레드 계산
        bids = self.current_orderbook.get("bids", [])
        asks = self.current_orderbook.get("asks", [])
        
        if not bids or not asks: return

        best_bid = float(bids[0]['price'])
        best_ask = float(asks[0]['price'])
        market_spread = best_ask - best_bid
        mid_price = (best_bid + best_ask) / 2.0
        
        spread_usd = self.spread_cents / 100.0
        limit_spread = spread_usd * 2.5 # 허용 한계

        # 2. 시장 스프레드 과다 이격 방어
        if market_spread > limit_spread:
            logger.warning("market_spread_too_wide", current=market_spread, limit=limit_spread)
            await self._reset_local_market_state()
            return

        # 3. 개별 주문 위치 방어
        for order_id, order in list(self.open_orders.items()):
            price_diff = abs(mid_price - float(order.get("price", 0)))
            
            # 위험 구간: 스프레드 내 10% 안쪽으로 들어왔거나, 아예 밖으로 밀려난 경우
            is_risky = price_diff < (spread_usd * 0.1)
            is_invalid = price_diff > spread_usd
            
            if is_risky or is_invalid:
                logger.info("defensive_action", reason="RISKY" if is_risky else "INVALID")
                await self._reset_local_market_state()
                break

    async def _defend_after_trade(self, actual_price: float, order_id: str | None = None):
        """체결 직후 리스크 점검 (Circuit Breaker, Hedging)"""
        # 1. 인벤토리 상태 점검
        if self.risk_manager.get_inventory_status() == "EMERGENCY":
            await self.handle_emergency("INVENTORY_CRITICAL_SKEW", exit_position=True)
            return

        # 2. 가격 이탈 검증 (Circuit Breaker)
        if order_id and order_id in self.open_orders:
            order_info = self.open_orders[order_id]
            expected_price = float(order_info.get("price", 0))
            side = order_info.get("side", "UNKNOWN")  # side 정보 추출

            # [수정] side 인자 추가 전달
            if not self.risk_manager.validate_execution_price(expected_price, actual_price, side):
                logger.error("circuit_breaker_halted_system", order_id=order_id)
                # 즉시 모든 주문 취소 및 상태 리셋
                await self._reset_local_market_state()
        
            self.open_orders.pop(order_id, None)

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

            # 2. 주문 취소 실행
            if self.current_market_id:
                await self.order_executor.cancel_all_orders(self.settings.market_id)
            
            # 3. 메모리 상의 주문 정보 클리어
            self.open_orders.clear()
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

            hedge_order = {
                "market": self.current_market_id, "side": "BUY", "size": str(abs(amount)),
                "price": str(target_price), "token_id": target_token
            }
            await self.order_executor.place_order(hedge_order)
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
        # 1. 갱신 주기 확인
        now_ms = time.time() * 1000
        if (now_ms - self.last_quote_time) < self.settings.quote_refresh_rate_ms:
            return
        self.last_quote_time = now_ms

        # 2. 데이터 최신화 확인
        if not self.current_orderbook:
            await self.update_orderbook()

        # 3. 쿼트 계산
        vol_1h = float(self.current_orderbook.get("volatility_1h", 0.005))
        
        # QuoteEngine 호출
        yes_q, no_q = self.quote_engine.generate_quotes(
            market_id=self.current_market_id, 
            best_bid=float(self.current_orderbook.get("best_bid", 0)),
            best_ask=float(self.current_orderbook.get("best_ask", 1)),
            yes_token_id=self.yes_token_id, 
            no_token_id=self.no_token_id,
            spread_cents=self.spread_cents,
            min_size_shares=self.min_size,
            volatility_1h=vol_1h
        )

        # 4. 기존 주문 정리 (Cancel)
        await self._cancel_stale_orders()

        # 5. 신규 주문 제출 (Place)
        # Cancel과 Place 사이의 간격을 최소화하기 위해 같은 함수 내에서 처리
        if yes_q: await self._place_quote(yes_q, "YES")
        if no_q: await self._place_quote(no_q, "NO")

    async def _place_quote(self, quote: Any, outcome: str):
        # Risk Manager 검증 (수정된 인자 반영)
        valid, reason = self.risk_manager.validate_order(
            quote.side, quote.size, self.current_orderbook
        )
        
        if not valid:
            logger.debug("quote_skipped", reason=reason, outcome=outcome)
            return False

        try:
            order_data = {
                "market": quote.market, "side": quote.side, "size": str(quote.size),
                "price": str(quote.price), "token_id": quote.token_id
            }
            result = await self.order_executor.place_order(order_data)
            if result and "id" in result: 
                self.open_orders[result["id"]] = order_data
                return True
        except Exception as e:
            logger.error("place_quote_failed", error=str(e))
        return False

    async def _cancel_stale_orders(self):
        """현재 열려있는 모든 주문을 취소 (Batch Cancel 권장)"""
        if self.open_orders:
            # open_orders의 ID 목록만 추출
            order_ids = list(self.open_orders.keys())
            if order_ids:
                # OrderExecutor에 batch_cancel_orders 구현이 있다면 그것을 사용 권장
                await self.order_executor.cancel_all_orders(self.current_market_id)
            self.open_orders.clear()

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