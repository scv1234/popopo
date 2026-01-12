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
        self.is_halted = False
        
        # [추가] 원금 회수를 위한 마지노선 가격 관리
        self.min_recovery_price = 0.0
        self.is_leg_risk_active = False # 한쪽만 남은 상태인지 여부

    def set_recovery_target(self, sold_price: float):
        """한쪽이 체결되었을 때, 남은 쪽이 팔아야 할 본전 가격을 설정합니다."""
        # 원금 1.0 - 판 가격 = 남은 쪽의 최소 판매가
        self.min_recovery_price = max(1.0 - sold_price, 0.01)
        self.is_leg_risk_active = True
        logger.info("🎯 RECOVERY_TARGET_SET", min_price=self.min_recovery_price)

    def validate_obi(self, orderbook: dict) -> tuple[bool, str]:
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        
        if not bids or not asks: return False, "EMPTY_ORDERBOOK"

        # [수정] 리스트([p, s])와 딕셔너리({'size': s}) 구조 모두 대응
        def get_size(item):
            if isinstance(item, list) and len(item) > 1: return float(item[1])
            return float(item.get('size', 0))

        bid_vol = sum(get_size(b) for b in bids[:3])
        ask_vol = sum(get_size(a) for a in asks[:3])
        
        if (bid_vol + ask_vol) == 0: return False, "NO_LIQUIDITY"
        
        obi = (bid_vol - ask_vol) / (bid_vol + ask_vol)
        if obi > 0.8: return False, "HIGH_BUY_PRESSURE" # 기준 완화(0.7 -> 0.8)
        return True, "OK"

    def validate_order(self, side: str, price: float, orderbook: dict[str, Any]) -> tuple[bool, str]:
        """주문 실행 전 최종 승인 로직"""
        # 1. 시스템 중단 확인
        if self.is_halted:
            return False, "TRADING_HALTED"

        # 2. 매수(BUY) 금지 로직 (민팅 전략이므로 시장가 매수는 금지)
        if side == "BUY":
            return False, "BUY_ORDERS_PROHIBITED_IN_MINT_STRATEGY"

        # 3. 본전 사수 검증 (가장 중요)
        if self.is_leg_risk_active:
            if price < self.min_recovery_price:
                # 본전보다 낮은 가격에 팔려고 하면 차단
                return False, f"BELOW_RECOVERY_PRICE_LIMIT({self.min_recovery_price})"

        # 4. 시장 위험도(OBI) 체크
        obi_valid, obi_reason = self.validate_obi(orderbook)
        if not obi_valid:
            return False, obi_reason

        return True, "OK"

    def check_market_danger(self, current_best_bid: float):
        """
        시장의 매수 희망가(Bid)가 내 본전보다 너무 낮아지면 봇을 중단시킵니다.
        (계속 안 팔려서 손해가 확정될 것 같은 상황 방지)
        """
        if not self.is_leg_risk_active:
            return

        # 시장가(Bid)가 내 마지노선보다 15% 이상 낮아지면 비상 상황으로 판단
        danger_zone = self.min_recovery_price * 0.85
        if current_best_bid < danger_zone:
            self.is_halted = True
            logger.error("🚨 MARKET_PRICE_CRASHED_BELOW_RECOVERY_LIMIT", 
                         market_bid=current_best_bid, 
                         target=self.min_recovery_price)

    def validate_execution_price(self, expected_price: float, actual_price: float) -> bool:
        """실제 체결가가 내가 낸 주문가보다 너무 낮으면 멈춥니다 (슬리피지 방어)"""
        if actual_price < (expected_price - self.settings.max_allowed_slippage):
            logger.error("🚨 SLIPPAGE_TOO_HIGH", expected=expected_price, actual=actual_price)
            self.is_halted = True
            return False
        return True

    def reset_halt(self):
        """수동 재개 및 리스크 상태 초기화"""
        self.is_halted = False
        self.is_leg_risk_active = False
        self.min_recovery_price = 0.0
        logger.info("🔄 RISK_MANAGER_RESET_SUCCESS")


