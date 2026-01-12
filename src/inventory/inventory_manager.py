# src/inventory/inventory_manager.py

from __future__ import annotations
from dataclasses import dataclass
from typing import Any
import structlog

logger = structlog.get_logger(__name__)

@dataclass
class Inventory:
    yes_position: float = 0.0
    no_position: float = 0.0
    net_exposure_shares: float = 0.0

    def update(self, yes_delta: float, no_delta: float):
        self.yes_position += yes_delta
        self.no_position += no_delta
        self.net_exposure_shares = self.yes_position - self.no_position

    def get_skew(self) -> float:
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
        self.inventory = Inventory()
        logger.info("inventory_manager_reset_complete")

    def update_inventory(self, yes_delta: float, no_delta: float):
        self.inventory.update(yes_delta, no_delta)

    # [수정] 외부(웹사이트)에서 수행된 Split 결과 잔고를 동기화하는 기능 추가
    def sync_inventory(self, yes_balance: float, no_balance: float):
        """온체인에서 감지된 토큰 잔고를 봇의 내부 상태에 동기화합니다."""
        self.inventory.yes_position = yes_balance
        self.inventory.no_position = no_balance
        self.inventory.net_exposure_shares = yes_balance - no_balance
        
        logger.info("🔄 Inventory Synced from On-chain Balance", 
                    yes=self.inventory.yes_position, 
                    no=self.inventory.no_position)

    # 기존 record_minting은 sync_inventory로 대체 가능하므로 유지하거나 삭제 가능
    def record_minting(self, amount_shares: float):
        self.inventory.yes_position += amount_shares
        self.inventory.no_position += amount_shares
        self.inventory.net_exposure_shares = self.inventory.yes_position - self.inventory.no_position

    def can_quote_yes(self, size_shares: float) -> bool:
        potential_exposure = self.inventory.net_exposure_shares + size_shares
        return potential_exposure <= self.max_exposure_shares

    def can_quote_no(self, size_shares: float) -> bool:
        potential_exposure = self.inventory.net_exposure_shares - size_shares
        return potential_exposure >= self.min_exposure_shares

    def get_quote_size_yes(self, base_size_shares: float) -> float:
        if self.inventory.net_exposure_shares > self.target_balance:
            return base_size_shares * 0.5
        return base_size_shares

    def get_quote_size_no(self, base_size_shares: float) -> float:
        if self.inventory.net_exposure_shares < self.target_balance:
            return base_size_shares * 0.5
        return base_size_shares

    def should_rebalance(self, skew_limit: float = 0.3) -> bool:
        return not self.inventory.is_balanced(skew_limit)