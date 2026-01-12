from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class Inventory:
    yes_position: float = 0.0
    no_position: float = 0.0
    net_exposure_shares: float = 0.0  # USD 가치가 아닌 수량(Share) 차이로 변경

    def update(self, yes_delta: float, no_delta: float):
        """
        체결된 수량만큼 인벤토리를 업데이트합니다.
        """
        self.yes_position += yes_delta
        self.no_position += no_delta
        
        # 수량 기반의 노출도 계산 (YES 수량 - NO 수량)
        # 이 값이 0에 가까울수록 진정한 델타 뉴트럴 상태입니다.
        self.net_exposure_shares = self.yes_position - self.no_position

    def get_skew(self) -> float:
        """
        인벤토리 쏠림 현상(Skew)을 수량 기준으로 계산합니다.
        """
        total_shares = abs(self.yes_position) + abs(self.no_position)
        if total_shares == 0:
            return 0.0
        return abs(self.net_exposure_shares) / total_shares

    def is_balanced(self, max_skew: float = 0.3) -> bool:
        return self.get_skew() <= max_skew


class InventoryManager:
    def __init__(self, max_exposure_usd: float, min_exposure_usd: float, target_balance: float = 0.0):
        self.max_exposure_usd = max_exposure_usd
        self.min_exposure_usd = min_exposure_usd
        self.target_balance = target_balance
        self.inventory = Inventory()

        self.max_exposure_shares = max_exposure_usd * 2.0 
        self.min_exposure_shares = min_exposure_usd * 2.0

    def reset(self):
        """새로운 마켓으로 전환 시 인벤토리 상태를 완전히 초기화합니다."""
        self.inventory = Inventory()
        logger.info("inventory_manager_reset_complete")

    def update_inventory(self, yes_delta: float, no_delta: float):
        self.inventory.update(yes_delta, no_delta)
        logger.debug(
            "inventory_updated_by_shares",
            yes_position=self.inventory.yes_position,
            no_position=self.inventory.no_position,
            net_exposure_shares=self.inventory.net_exposure_shares,
            skew=self.inventory.get_skew(),
        )

    def record_minting(self, amount_shares: float):
        """
        민팅(Split) 완료 후 Yes와 No의 수량을 동시에 업데이트합니다.
        이때 net_exposure_shares는 0(중립)이 유지되어야 합니다.
        """
        self.inventory.yes_position += amount_shares
        self.inventory.no_position += amount_shares
        
        # 1:1 상태이므로 노출도는 변하지 않음 (Yes 수량 - No 수량)
        self.inventory.net_exposure_shares = self.inventory.yes_position - self.inventory.no_position
        
        logger.info("Inventory Synced after Minting", 
                    yes=self.inventory.yes_position, 
                    no=self.inventory.no_position,
                    exposure=self.inventory.net_exposure_shares)

    def can_quote_yes(self, size_shares: float) -> bool:
        """새로운 YES 주문을 넣었을 때 수량 한도를 넘지 않는지 확인합니다."""
        potential_exposure = self.inventory.net_exposure_shares + size_shares
        return potential_exposure <= self.max_exposure_shares

    def can_quote_no(self, size_shares: float) -> bool:
        """새로운 NO 주문을 넣었을 때 수량 한도를 넘지 않는지 확인합니다."""
        potential_exposure = self.inventory.net_exposure_shares - size_shares
        return potential_exposure >= self.min_exposure_shares

    def get_quote_size_yes(self, base_size_shares: float) -> float:
        """인벤토리 균형을 위해 YES 주문 수량을 조절합니다."""
        # 이미 YES가 NO보다 많다면 주문 크기를 줄임
        if self.inventory.net_exposure_shares > self.target_balance:
            return base_size_shares * 0.5
        return base_size_shares

    def get_quote_size_no(self, base_size_shares: float) -> float:
        """인벤토리 균형을 위해 NO 주문 수량을 조절합니다."""
        # 이미 NO가 YES보다 많다면 주문 크기를 줄임
        if self.inventory.net_exposure_shares < self.target_balance:
            return base_size_shares * 0.5
        return base_size_shares

    def should_rebalance(self, skew_limit: float = 0.3) -> bool:

        return not self.inventory.is_balanced(skew_limit)