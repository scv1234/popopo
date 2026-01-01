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
    def validate_execution_price(self, expected_price: float, actual_price: float) -> bool:
        """
        체결가가 예상가(안전 끝단)보다 너무 불리하면 시스템을 즉시 중단합니다.
        """
        slippage = abs(actual_price - expected_price)
        
        if slippage > self.settings.max_allowed_slippage:
            logger.error(
                "🚨 CIRCUIT_BREAKER_TRIGGERED",
                expected=expected_price,
                actual=actual_price,
                slippage=round(slippage, 4),
                limit=self.settings.max_allowed_slippage
            )
            self.is_halted = True  # 시스템 가동 중지 플래그 On
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
    def validate_order(self, side: str, size_shares: float) -> tuple[bool, str]:
        """주문이 나가기 전, 시스템 중단 여부 및 수량 한도를 검사합니다."""
        
        # 1. 시스템 중단 여부 확인
        if self.is_halted:
            return False, "TRADING_HALTED_BY_CIRCUIT_BREAKER"
        
        # 2. 수량 기반 노출 한도 체크 (기존 USD 대신 Shares 기준)
        if side == "BUY":
            if not self.inventory_manager.can_quote_yes(size_shares):
                return False, "MAX_SHARE_EXPOSURE_EXCEEDED"
        else: # SELL 또는 NO BUY 상황
            if not self.inventory_manager.can_quote_no(size_shares):
                return False, "MAX_SHARE_EXPOSURE_EXCEEDED"

        # 3. 인벤토리 상태 확인
        status = self.get_inventory_status()
        if status == "EMERGENCY":
            return False, "INVENTORY_CRITICAL_SKEW"
        
        return True, "OK"

    def reset_halt(self):
        """중단된 봇을 다시 수동으로 재개합니다."""
        self.is_halted = False
        logger.info("system_trading_resumed_by_user")

