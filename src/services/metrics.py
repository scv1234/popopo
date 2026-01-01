from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, start_http_server

orders_placed_counter = Counter(
    "pm_mm_orders_placed_total", "Total orders placed", ["side", "outcome"]
)
orders_filled_counter = Counter(
    "pm_mm_orders_filled_total", "Total orders filled", ["side", "outcome"]
)
orders_cancelled_counter = Counter(
    "pm_mm_orders_cancelled_total", "Total orders cancelled"
)
# 인벤토리 및 노출도 게이지 (Share 기반 추가)
inventory_gauge = Gauge(
    "pm_mm_inventory", "Current inventory positions in Shares", ["type"]
)
# [수정] USD 노출도와 함께 수량(Share) 노출도 지표 추가
exposure_shares_gauge = Gauge("pm_mm_exposure_shares", "Current net exposure in Shares (YES - NO)")
exposure_gauge = Gauge("pm_mm_exposure_usd", "Current net exposure in USD")

# [추가] Circuit Breaker 작동 상태 (0: 정상, 1: 중단)
halt_status_gauge = Gauge("pm_mm_halt_status", "System halt status (1 if halted)")

spread_gauge = Gauge("pm_mm_spread_bps", "Current spread in basis points")
profit_gauge = Gauge("pm_mm_profit_usd", "Cumulative profit in USD")
quote_latency_histogram = Histogram(
    "pm_mm_quote_latency_ms",
    "Quote generation and placement latency in milliseconds",
    buckets=[10, 50, 100, 250, 500, 1000],
)


def start_metrics_server(host: str, port: int) -> None:
    start_http_server(port, addr=host)


def record_order_placed(side: str, outcome: str) -> None:
    orders_placed_counter.labels(side=side, outcome=outcome).inc()


def record_order_filled(side: str, outcome: str) -> None:
    orders_filled_counter.labels(side=side, outcome=outcome).inc()


def record_order_cancelled() -> None:
    orders_cancelled_counter.inc()


def record_inventory(inventory_type: str, value: float) -> None:
    inventory_gauge.labels(type=inventory_type).set(value)

# [추가] 수량 기반 노출도 기록 함수
def record_exposure_shares(shares: float) -> None:
    exposure_shares_gauge.set(shares)

# [추가] 시스템 중단 상태 기록 함수
def record_halt_status(is_halted: bool) -> None:
    halt_status_gauge.set(1.0 if is_halted else 0.0)


def record_exposure(exposure_usd: float) -> None:
    exposure_gauge.set(exposure_usd)


def record_spread(spread_bps: float) -> None:
    spread_gauge.set(spread_bps)


def record_profit(profit_usd: float) -> None:
    profit_gauge.set(profit_usd)


def record_quote_latency(latency_ms: float) -> None:
    quote_latency_histogram.observe(latency_ms)

