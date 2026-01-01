from __future__ import annotations

from typing import Any
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = "development"
    log_level: str = "INFO"

    # --- 1. Polymarket API & Auth ---
    polymarket_api_url: str = Field(default="https://clob.polymarket.com")
    polymarket_ws_url: str = Field(default="wss://clob-ws.polymarket.com")
    private_key: str = Field(description="Ethereum private key for signing orders")
    public_address: str = Field(description="Ethereum public address")
    rpc_url: str = Field(default="https://polygon-rpc.com")
    
    # --- 2. Market Configuration & Discovery (Honey Pot) ---
    market_id: str = Field(default="", description="현재 트레이딩 중인 마켓 ID")
    market_discovery_enabled: bool = Field(default=True, description="꿀통 마켓 자동 탐색 활성화")
    min_daily_reward_usd: float = Field(default=50.0, description="최소 일일 보상액 필터")
    max_volatility_threshold: float = Field(default=0.02, description="허용 최대 가격 변동폭 (2센트)")
    spread_weight: float = Field(default=2.0, description="스프레드 크기 가중치")
    min_size: float = Field(default=0.0, description="마켓별 최소 리워드 수량(Shares)")

    # --- 3. Quoting & Execution ---
    default_size: float = Field(default=100.0, description="기본 주문 수량 (Shares)")
    quote_refresh_rate_ms: int = Field(default=1000, description="쿼트 갱신 주기")
    order_lifetime_ms: int = Field(default=3000, description="주문 유효 시간")
    cancel_replace_interval_ms: int = Field(default=500, description="취소/교체 주기")
    batch_cancellations: bool = Field(default=True, description="일괄 취소 사용 여부")
    
    # --- 4. Inventory & 3-Stage Defense (핵심 리스크 관리) ---
    # [수량 기반 인벤토리]
    inventory_skew_limit: float = Field(default=0.3, description="일반적인 인벤토리 불균형 한도")
    max_exposure_usd: float = Field(default=1000.0, description="최대 USD 노출 한도")
    min_exposure_usd: float = Field(default=-1000.0, description="최소 USD 노출 한도")
    target_inventory_balance: float = Field(default=0.0, description="목표 인벤토리 밸런스")

    # [방어 시스템 파라미터]
    # 2단 방어: Circuit Breaker 임계치 (예상가와 체결가 차이)
    max_allowed_slippage: float = Field(default=0.01, description="허용 최대 슬리피지 (Circuit Breaker)")
    # 3단 방어: Hard-Limit 비상 탈출 기준 (Skew 70% 이상 시)
    emergency_skew_limit: float = Field(default=0.7, description="비상 강제 청산 기준선")

    # --- 5. Auto-Redeem & Metrics ---
    auto_redeem_enabled: bool = Field(default=True)
    redeem_threshold_usd: float = Field(default=1.0)
    metrics_host: str = "0.0.0.0"
    metrics_port: int = 9305


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings