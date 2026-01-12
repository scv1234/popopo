# src/api_server.py
import asyncio
import sys
import sqlite3
import json
from contextlib import asynccontextmanager
from pathlib import Path

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from web3 import Web3

from src.main import MarketMakerBot
from src.config import get_settings
from src.logging_config import configure_logging

# --- 1. 초기 설정 및 봇 인스턴스 ---
settings = get_settings()
configure_logging(settings.log_level)
bot = MarketMakerBot(settings)

# USDC (Polygon) 설정
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
ERC20_ABI = [{"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"}]

class SemiAutoOrderRequest(BaseModel):
    market_id: str
    amount: float
    yes_token_id: str
    no_token_id: str

# --- 2. Lifecycle 관리 ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 서버 시작 시 봇 루프 실행
    bot_task = asyncio.create_task(bot.run())
    yield
    # 서버 종료 시 정리
    await bot.cleanup()
    bot_task.cancel()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 3. API 엔드포인트 ---

@app.get("/honey-pots")
async def get_honey_pots():
    try:
        conn = sqlite3.connect('bot_data.db')
        cursor = conn.cursor()
        cursor.execute('SELECT data FROM honeypots')
        rows = cursor.fetchall()
        conn.close()
        return [json.loads(row[0]) for row in rows]
    except: return []

@app.get("/status")
async def get_status():
    """봇의 실시간 상태 및 포지션 반환"""
    mid_price = 0.5
    if bot.current_market_id:
        yes_book = bot.orderbooks.get(bot.yes_token_id, {})
        bids = yes_book.get("bids", [])
        asks = yes_book.get("asks", [])
        if bids and asks:
            try:
                mid_price = (bot._extract_price(bids[0]) + bot._extract_price(asks[0])) / 2.0
            except: pass

    # InventoryManager에서 현재 포지션 수량을 가져옴
    return {
        "is_halted": bot.risk_manager.is_halted, 
        "market": {
            "market_id": bot.current_market_id, 
            "mid_price": round(mid_price, 4)
        },
        "positions": {
            "yes": round(bot.inventory_manager.inventory.yes_position, 2),
            "no": round(bot.inventory_manager.inventory.no_position, 2),
            "net_exposure": round(bot.inventory_manager.inventory.net_exposure_shares, 2)
        }
    }

@app.get("/wallet")
async def get_wallet():
    """지갑의 USDC 잔고 조회"""
    try:
        w3 = Web3(Web3.HTTPProvider(settings.rpc_url))
        addr = Web3.to_checksum_address(settings.public_address)
        usdc_contract = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_ABI)
        usdc = usdc_contract.functions.balanceOf(addr).call() / 1e6
        return {"usdc_balance": round(usdc, 2)}
    except: return {"usdc_balance": 0}

@app.get("/open-orders")
async def get_open_orders():
    """봇의 메모리(open_orders)를 대시보드 형식에 맞춰 반환"""
    formatted = {}
    for oid, o in bot.open_orders.items():
        mid = o.get("market")
        outcome = o.get("outcome", "YES")
        if mid not in formatted: 
            formatted[mid] = {"YES": [], "NO": []}
        
        formatted[mid][outcome].append({
            "order_id": oid, 
            "side": o.get("side"),
            "price": float(o.get("price")), 
            "size": float(o.get("size"))
        })
    return formatted

@app.post("/place-semi-auto-order")
async def place_order(req: SemiAutoOrderRequest):
    """지갑 잔고 기반 Sell Farming 전략 실행 (Split은 외부에서 수행)"""
    # [수정] TypeError 해결: signature에 맞춰 market_id와 amount만 전달
    success = await bot.execute_optimizer_order(req.market_id, req.amount)
    
    if not success: 
        raise HTTPException(status_code=500, detail="Farming strategy start failed. Check your token balances.")
    return {"status": "success"}

@app.post("/cancel-order/{order_id}")
async def cancel_order(order_id: str):
    """[수정] 거래소 취소 + 봇 메모리 정리 통합 호출"""
    success = await bot.cancel_single_order(order_id)
    if not success:
        raise HTTPException(status_code=500, detail="Cancel failed")
    return {"status": "success"}

@app.post("/batch-cancel-manual")
async def batch_cancel():
    """[수정] 모든 수동 주문 일괄 취소 및 메모리 정리"""
    await bot.batch_cancel_manual_orders()
    return {"status": "success"}

@app.get("/logs")
async def get_logs():
    """최신 로그 30줄 반환"""
    try:
        if Path("bot.log").exists():
            with open("bot.log", "r", encoding="utf-8") as f:
                return f.readlines()[-30:]
        return []
    except: return []

# --- 4. 정적 파일 서빙 (대시보드) ---
WEB_DIR = Path("web")
@app.get("/")
async def serve_index():
    return FileResponse(WEB_DIR / "index.html")

if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR)), name="static")