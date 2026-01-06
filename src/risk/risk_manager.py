from __future__ import annotations

from typing import Any

import structlog

from src.config import Settings
from src.inventory.inventory_manager import InventoryManager

logger = structlog.get_logger(__name__)


class RiskManager:
    def __init__(self, settings: Settings, inventory_manager: InventoryManager):
        self.settings = settings
        self.inventory_manager = inventory_manager
        self.is_halted = False  # Circuit Breaker 작동 여부

    def validate_obi(self, orderbook: dict) -> tuple[bool, str]:
        """[추가] OBI(Order Book Imbalance)를 분석하여 가격 급변동 전조를 감지합니다."""
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        
        if not bids or not asks: return True, "OK"

        # 최상단 호가 물량 합산
        bid_vol = sum(float(b['size']) for b in bids[:3])
        ask_vol = sum(float(a['size']) for a in asks[:3])
        
        if (bid_vol + ask_vol) == 0: return True, "OK"
        
        obi = (bid_vol - ask_vol) / (bid_vol + ask_vol)
        
        # OBI가 극단적(0.8 이상)이면 주문을 일시 차단하여 보호합니다.
        if obi > 0.8: return False, "OBI_HIGH_UPWARD_RISK"
        if obi < -0.8: return False, "OBI_HIGH_DOWNWARD_RISK"
        
        return True, "OK"

    # --- 1단 방어: Auto-Hedge (델타 뉴트럴 계산) ---
    def calculate_hedge_need(self) -> float:
        """
        1:1 수량을 맞추기 위해 필요한 헤징 수량을 계산합니다.
        결과가 양수면 YES가 부족, 음수면 NO가 부족한 상태입니다.
        """
        # net_exposure_shares = YES 수량 - NO 수량
        # 0을 만들기 위해 필요한 차이값을 반환
        return -self.inventory_manager.inventory.net_exposure_shares

    # --- 2단 방어: Slippage Circuit Breaker (가격 이탈 차단) ---
    def validate_execution_price(self, expected_price: float, actual_price: float, side: str) -> bool:
        """
        체결가가 예상가보다 '불리한 방향'으로 허용치를 초과해 벗어났는지 검사합니다.
        - BUY: 체결가 > 예상가 + 허용치 (너무 비싸게 삼 -> 위험)
        - SELL: 체결가 < 예상가 - 허용치 (너무 싸게 팜 -> 위험)
        """
        allowed_slippage = self.settings.max_allowed_slippage
        is_bad_execution = False
        diff = 0.0

        if side == "BUY":
            # 매수인데 예상보다 비싸게 체결된 경우
            if actual_price > (expected_price + allowed_slippage):
                is_bad_execution = True
                diff = actual_price - expected_price
        elif side == "SELL":
            # 매도인데 예상보다 싸게 체결된 경우
            if actual_price < (expected_price - allowed_slippage):
                is_bad_execution = True
                diff = expected_price - actual_price

        if is_bad_execution:
            logger.error(
                "🚨 CIRCUIT_BREAKER_TRIGGERED",
                side=side,
                expected=expected_price,
                actual=actual_price,
                diff=round(diff, 4),
                limit=allowed_slippage
            )
            self.is_halted = True
            return False
    
        return True

    # --- 3단 방어: Inventory Hard-Limit (인벤토리 쏠림 감지) ---
    def get_inventory_status(self) -> str:
        """
        인벤토리 불균형(Skew)을 진단하여 비상 상황 여부를 판단합니다.
        """
        skew = self.inventory_manager.inventory.get_skew()
        
        if skew >= self.settings.emergency_skew_limit:
            logger.error("🚨 EMERGENCY_SKEW_DETECTION", skew=skew, limit=self.settings.emergency_skew_limit)
            return "EMERGENCY"  # 즉시 시장가 청산이 필요한 상태
        
        elif skew >= 0.3:  # 주의 단계
            return "WARNING"
            
        return "HEALTHY"

    # --- 통합 유효성 검사 (주문 실행 전 호출) ---
    def validate_order(self, side: str, size_shares: float, orderbook: dict[str, Any]) -> tuple[bool, str]:
        """주문이 나가기 전, 시스템 중단 여부 및 수량 한도를 검사합니다."""
        
        # 1. 시스템 중단 여부 확인
        if self.is_halted:
            return False, "TRADING_HALTED_BY_CIRCUIT_BREAKER"

        # 2. 외부 시장 위험 (OBI 체크) - [추가 및 순서 조정]
        # 내 상태와 상관없이 시장 자체가 비정상적(한쪽으로 쏠림)이라면 주문을 내지 않는 것이 상책입니다.
        obi_valid, obi_reason = self.validate_obi(orderbook)
        if not obi_valid:
            return False, obi_reason
        
        # 3. 수량 기반 노출 한도 체크 (기존 USD 대신 Shares 기준)
        if side == "BUY":
            if not self.inventory_manager.can_quote_yes(size_shares):
                return False, "MAX_SHARE_EXPOSURE_EXCEEDED"
        else: # SELL 또는 NO BUY 상황
            if not self.inventory_manager.can_quote_no(size_shares):
                return False, "MAX_SHARE_EXPOSURE_EXCEEDED"

        # 4. 인벤토리 상태 확인
        status = self.get_inventory_status()
        if status == "EMERGENCY":
            return False, "INVENTORY_CRITICAL_SKEW"
        
        return True, "OK"

    def reset_halt(self):
        """중단된 봇을 다시 수동으로 재개합니다."""
        self.is_halted = False
        logger.info("system_trading_resumed_by_user")

