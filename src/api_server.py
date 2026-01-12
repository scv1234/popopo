import asyncio
import sys
import sqlite3
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, HTTPException, Body
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
ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"}
]

# --- 2. Pydantic 모델 (API 요청 바디) ---
class MintRequest(BaseModel):
    amount: float

class SemiAutoOrderRequest(BaseModel):
    market_id: str
    amount: float
    yes_token_id: str
    no_token_id: str

# --- 3. Lifecycle 관리 (서버 시작/종료) ---
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
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 4. 데이터베이스 헬퍼 ---
def load_honeypots_from_db():
    try:
        conn = sqlite3.connect('bot_data.db')
        cursor = conn.cursor()
        cursor.execute('SELECT data FROM honeypots')
        rows = cursor.fetchall()
        conn.close()
        return [json.loads(row[0]) for row in rows]
    except Exception:
        return []

# --- 5. API 엔드포인트 ---

@app.get("/honey-pots")
async def get_honey_pots():
    """DB에 저장된 수익성 높은 마켓 목록 반환"""
    return load_honeypots_from_db()

@app.get("/status")
async def get_status():
    """봇의 실시간 상태 및 인벤토리 정보"""
    mid_price = 0.5
    
    def extract_price(item):
        if isinstance(item, list): return float(item[0])
        return float(item.get('price', 0))

    if bot.current_market_id:
        yes_book = bot.orderbooks.get(bot.yes_token_id, {})
        bids, asks = yes_book.get("bids", []), yes_book.get("asks", [])
        if bids and asks:
            try:
                mid_price = (extract_price(bids[0]) + extract_price(asks[0])) / 2.0
            except: pass

    return {
        "is_halted": bot.risk_manager.is_halted,
        "inventory": {
            "yes": bot.inventory_manager.inventory.yes_position,
            "no": bot.inventory_manager.inventory.no_position,
            "skew": bot.inventory_manager.inventory.get_skew()
        },
        "market": {
            "market_id": bot.current_market_id,
            "mid_price": round(mid_price, 4)
        }
    }

@app.get("/wallet")
async def get_wallet():
    """지갑 잔고 조회 (MATIC & USDC)"""
    try:
        w3 = Web3(Web3.HTTPProvider(settings.rpc_url))
        addr = Web3.to_checksum_address(settings.public_address)
        matic = float(w3.from_wei(w3.eth.get_balance(addr), 'ether'))
        usdc_contract = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_ABI)
        usdc = usdc_contract.functions.balanceOf(addr).call() / 1e6
        return {"usdc_balance": round(usdc, 2), "matic_balance": round(matic, 4)}
    except Exception as e:
        return {"error": str(e)}

@app.get("/open-orders")
async def get_open_orders():
    """
    현재 봇이 관리 중인 활성 주문 조회.
    대시보드(index.html) 형식에 맞춰 데이터를 재구성하여 반환합니다.
    """
    # 봇의 메모리에 저장된 주문 목록 가져오기
    raw_orders = bot.open_orders 
    
    # 대시보드가 기대하는 구조: { "market_id": { "YES": [...], "NO": [...] } }
    formatted_orders = {}

    for order_id, order in raw_orders.items():
        market_id = order.get("market")
        outcome = order.get("outcome", "YES") # YES 또는 NO
        
        if market_id not in formatted_orders:
            formatted_orders[market_id] = {"YES": [], "NO": []}
        
        # 대시보드 테이블 표시를 위한 데이터 정리
        formatted_orders[market_id][outcome].append({
            "order_id": order_id,
            "side": order.get("side"),
            "price": float(order.get("price")),
            "size": float(order.get("size"))
        })

    return formatted_orders

@app.post("/place-semi-auto-order")
async def place_semi_auto_order(req: SemiAutoOrderRequest):
    """대시보드 Optimizer가 요청한 자동 주문 실행"""
    success = await bot.execute_optimizer_order(
        req.market_id, req.amount, req.yes_token_id, req.no_token_id
    )
    if not success:
        raise HTTPException(status_code=500, detail="Order execution failed")
    return {"status": "success"}

@app.post("/cancel-order/{order_id}")
async def cancel_order(order_id: str):
    """특정 주문 취소"""
    success = await bot.executor.cancel_order(order_id)
    return {"status": "success" if success else "failed"}

@app.post("/batch-cancel-manual")
async def batch_cancel():
    """모든 활성 주문 일괄 취소"""
    await bot.executor.cancel_all_orders()
    return {"status": "success"}

@app.get("/logs")
async def get_logs():
    """최신 로그 반환"""
    try:
        if Path("bot.log").exists():
            with open("bot.log", "r", encoding="utf-8") as f:
                return f.readlines()[-50:]
        return []
    except:
        return ["Log access error"]

# --- 6. 대시보드 서빙 (정적 파일) ---
WEB_DIR = Path("web")

@app.get("/")
async def serve_dashboard():
    """루트 접속 시 web/index.html 반환"""
    index_path = WEB_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return {"error": "web/index.html not found"}

if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR)), name="static")