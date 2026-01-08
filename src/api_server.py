# src/api_server.py
import asyncio
import sys
import sqlite3
import json
from contextlib import asynccontextmanager

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from src.main import MarketMakerBot
from src.config import get_settings
from src.logging_config import configure_logging # [추가]
from src.polymarket.honeypot_service import HoneypotService  # [연동]
from pydantic import BaseModel
from web3 import Web3

settings = get_settings()
configure_logging(settings.log_level)
bot = MarketMakerBot(settings)
honeypot_service = HoneypotService(settings)

# USDC (Polygon) 컨트랙트 주소 및 최소 ABI
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"}
]

async def run_honeypot_scanner():
    """배경 작업: 10분마다 폴리마켓을 스캔하여 DB를 갱신합니다."""
    while True:
        try:
            print("🔍 [Scanner] 주기적 시장 스캔 시작...")
            await honeypot_service.scan()
            print("✅ [Scanner] 시장 스캔 및 DB 갱신 완료.")
        except Exception as e:
            print(f"❌ [Scanner] 스캔 중 오류 발생: {e}")
        await asyncio.sleep(600) # 10분 대기

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 서버 시작 시 봇 루프 실행
    bot_task = asyncio.create_task(bot.run())
    yield
    # 서버 종료 시 봇 정리
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

class OrderRequest(BaseModel):
    market_id: str
    amount: float
    yes_token_id: str
    no_token_id: str

def load_honeypots_from_db():
    """HoneypotService가 저장한 SQLite DB에서 데이터를 로드합니다."""
    try:
        conn = sqlite3.connect('bot_data.db')
        cursor = conn.cursor()
        cursor.execute('SELECT data FROM honeypots')
        rows = cursor.fetchall()
        conn.close()
        return [json.loads(row[0]) for row in rows]
    except Exception as e:
        print(f"❌ DB 조회 실패: {e}")
        return []

def calculate_actual_mid(token_id, fallback_price=0.5):
    """실시간 오더북 리스트에서 최우선 호가를 찾아 중간가를 계산합니다."""
    if not token_id: return fallback_price
    
    # 1. 봇의 메모리에 있는 실시간 오더북 확인
    book = bot.orderbooks.get(token_id, {})
    
    # L2 Book 데이터 구조(리스트)에서 가격 추출
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    
    if bids and asks:
        try:
            # bids[0][0] 또는 bids[0]['price'] 형태 대응
            best_bid = float(bids[0][0] if isinstance(bids[0], list) else bids[0].get('price', 0))
            best_ask = float(asks[0][0] if isinstance(asks[0], list) else asks[0].get('price', 0))
            
            if best_bid > 0 and best_ask > 0:
                return round((best_bid + best_ask) / 2.0, 3)
        except Exception:
            pass

    # 2. 실시간 데이터가 없으면 DB에 저장된 스캔 당시 가격 사용
    try:
        conn = sqlite3.connect('bot_data.db')
        cursor = conn.cursor()
        cursor.execute("SELECT data FROM honeypots WHERE id = (SELECT id FROM honeypots WHERE data LIKE ? LIMIT 1)", (f'%{token_id}%',))
        row = cursor.fetchone()
        conn.close()
        
        if row:
            m_data = json.loads(row[0])
            if m_data.get("yes_token_id") == token_id:
                return m_data.get("mid_yes", fallback_price)
            return m_data.get("mid_no", fallback_price)
    except:
        pass

    return fallback_price

@app.get("/honey-pots")
async def get_honey_pots():
    """DB 데이터와 실시간 봇 데이터를 병합하여 반환합니다."""
    db_markets = load_honeypots_from_db()
    if not db_markets:
        return []

    results = []
    for m in db_markets:
        y_id = m.get("yes_token_id")
        n_id = m.get("no_token_id")
        
        # 기본값은 DB에 저장된 당시의 mid 값을 사용 (0.5 방지)
        mid_yes = m.get("mid_yes", 0.5)
        mid_no = m.get("mid_no", 0.5)

        # 봇이 실시간 데이터를 가지고 있는지 확인
        for tid, current_mid in [(y_id, "mid_yes"), (n_id, "mid_no")]:
            book = bot.orderbooks.get(tid, {})
            bids = book.get("bids", [])
            asks = book.get("asks", [])

            if bids and asks:
                try:
                    # 폴리마켓 데이터 구조: [[가격, 수량], [가격, 수량], ...]
                    # 첫 번째 요소의 0번 인덱스가 가격입니다.
                    best_bid = float(bids[0][0] if isinstance(bids[0], list) else bids[0].get('price', 0))
                    best_ask = float(asks[0][0] if isinstance(asks[0], list) else asks[0].get('price', 0))
                    
                    if best_bid > 0 and best_ask > 0:
                        if current_mid == "mid_yes": mid_yes = round((best_bid + best_ask) / 2.0, 3)
                        else: mid_no = round((best_bid + best_ask) / 2.0, 3)
                except (IndexError, TypeError, ValueError):
                    continue

        results.append({
            "market_id": m.get("market_id"),
            "title": m.get("title"),
            "slug": m.get("slug"),
            "mid_yes": mid_yes,
            "mid_no": mid_no,
            "score": m.get("score"),
            "total_depth": m.get("total_depth"),
            "reward": m.get("reward"),
            "min_size": m.get("min_size", 20),
            "spread_cents": m.get("spread_cents", 3),
            "yes_token_id": y_id,
            "no_token_id": n_id
        })
    return results

@app.get("/status")
async def get_status():
    """봇의 작동 상태와 인벤토리 정보만 반환합니다."""
    yes_token = getattr(bot, 'yes_token_id', None)
    yes_book = bot.orderbooks.get(yes_token, {}) if yes_token else {}
    best_bid = float(yes_book.get("best_bid", 0))
    best_ask = float(yes_book.get("best_ask", 1))
    mid_price = bot.quote_engine.calculate_mid_price(best_bid, best_ask)
    current_spread = getattr(bot, 'spread_cents', 3)

    return {
        "is_halted": bot.risk_manager.is_halted,
        "is_locked": bot.state_lock.locked(),
        "inventory": {
            "yes": bot.inventory_manager.inventory.yes_position,
            "no": bot.inventory_manager.inventory.no_position,
            "net_shares": bot.inventory_manager.inventory.net_exposure_shares,
            "skew": bot.inventory_manager.inventory.get_skew()
        },
        "market": {
            "market_id": bot.current_market_id, 
            "mid_price": round(mid_price, 4),
            "spread_cents": current_spread
        }
    }

# 추가로 /wallet 엔드포인트에서도 매틱을 제거하여 깔끔하게 만듭니다.
@app.get("/wallet")
async def get_wallet():
    """RPC를 통해 실제 지갑의 USDC 잔고를 조회합니다."""
    try:
        w3 = Web3(Web3.HTTPProvider(settings.rpc_url))
        # 봇이 사용하는 공개 주소 또는 프록시 주소 설정
        address = Web3.to_checksum_address(settings.public_address)
        
        usdc_contract = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_ABI)
        raw_balance = usdc_contract.functions.balanceOf(address).call()
        decimals = usdc_contract.functions.decimals().call()
        usdc_balance = raw_balance / (10 ** decimals)

        return {
            "usdc_balance": float(usdc_balance)
        }
    except Exception as e:
        return {"usdc_balance": 0.0, "error": str(e)}

@app.post("/place-semi-auto-order")
async def place_semi_auto_order(req: OrderRequest):
    """대시보드에서 누른 '유동성 공급' 버튼을 처리합니다."""
    if bot.risk_manager.is_halted:
        raise HTTPException(status_code=400, detail="시스템이 중단되었습니다. 먼저 리셋 버튼을 눌러주세요.")
    
    success = await bot.execute_manual_safety_order(
        req.market_id, 
        req.amount, 
        req.yes_token_id, 
        req.no_token_id
    )
    if not success:
        raise HTTPException(status_code=500, detail="주문 전송에 실패했습니다. 로그를 확인하세요.")
    
    return {"status": "success"}

# [추가] 봇 리셋 엔드포인트
@app.post("/reset-bot")
async def reset_bot():
    """서킷 브레이커로 중단된 봇을 수동으로 재개합니다."""
    if bot.risk_manager.is_halted:
        bot.risk_manager.reset_halt()
        return {"status": "success", "message": "봇이 정상 상태로 재설정되었습니다."}
    
    return {"status": "ignored", "message": "봇이 이미 정상 작동 중입니다."}

@app.get("/wallet")
async def get_wallet():
    """RPC를 통해 실제 지갑의 USDC 및 MATIC 잔고를 조회합니다."""
    try:
        w3 = Web3(Web3.HTTPProvider(settings.rpc_url))
        address = Web3.to_checksum_address(settings.public_address)
        
        native_balance = w3.eth.get_balance(address)
        matic_balance = w3.from_wei(native_balance, 'ether')
        
        usdc_contract = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_ABI)
        raw_balance = usdc_contract.functions.balanceOf(address).call()
        decimals = usdc_contract.functions.decimals().call()
        usdc_balance = raw_balance / (10 ** decimals)

        return {
            "address": settings.public_address,
            "usdc_balance": round(usdc_balance, 2),
            "matic_balance": round(float(matic_balance), 4),
            "native_token": "Polygon MATIC"
        }
    except Exception as e:
        print(f"❌ 잔고 조회 실패: {e}")
        return {
            "address": settings.public_address,
            "usdc_balance": 0.0,
            "matic_balance": 0.0,
            "error": str(e)
        }

@app.get("/open-orders")
async def get_open_orders():
    """현재 거래소 활성 주문 리스트를 마켓별, 결과별(YES/NO)로 그룹화하여 반환"""
    grouped = {}
    
    for order_id, details in bot.open_orders.items():
        market_id = details.get("market", "Unknown")
        outcome = details.get("outcome", "YES") # 'YES' 또는 'NO'
        
        # 마켓별 초기화
        if market_id not in grouped:
            grouped[market_id] = {"YES": [], "NO": []}
            
        # 해당 마켓의 YES 또는 NO 리스트에 주문 추가
        grouped[market_id][outcome].append({
            "order_id": order_id,
            "side": details.get("side"),
            "price": float(details.get("price")),
            "size": float(details.get("size"))
        })
    
    return grouped

@app.post("/batch-cancel-manual")
async def batch_cancel_manual():
    success = await bot.batch_cancel_manual_orders()
    return {"status": "success" if success else "failed"}    

@app.post("/cancel-order/{order_id}")
async def cancel_order(order_id: str):
    """특정 주문 ID를 사용하여 개별 취소"""
    success = await bot.cancel_single_order(order_id)
    if not success:
        raise HTTPException(status_code=404, detail="주문을 찾을 수 없거나 취소에 실패했습니다.")
    return {"status": "success"}

@app.get("/recommend-allocation")
async def recommend_allocation(total_budget: float = 1000.0):
    # 1. 현재 꿀통 데이터 가져오기
    pots = await get_honey_pots()  # 비동기 호출 수정
    if not pots: return {"error": "No market data available"}

    # 2. 시뮬레이션 (Greedy 알고리즘)
    # $1씩 가장 효율이 좋은 곳에 배분하는 방식
    allocations = {p['market_id']: 0.0 for p in pots}
    step = 5.0 # 5달러 단위로 배분 시뮬레이션
    current_budget = total_budget

    while current_budget >= step:
        best_market = None
        max_gain = -1
        
        for p in pots:
            mid = p['market_id']
            r = p['reward']
            d = p['total_depth']
            x = allocations[mid]
            
            # 현재 수익 vs 5달러 더 넣었을 때 수익 차이 계산
            current_rev = r * (x / (d + x)) if (d + x) > 0 else 0
            next_rev = r * ((x + step) / (d + x + step))
            gain = next_rev - current_rev
            
            if gain > max_gain:
                max_gain = gain
                best_market = mid
        
        if best_market:
            allocations[best_market] += step
            current_budget -= step
        else: break

    # 3. 결과 정리 (수익 예상치 포함)
    result = []
    for p in pots:
        amt = allocations[p['market_id']]
        if amt > 0:
            est_profit = p['reward'] * (amt / (p['total_depth'] + amt))
            result.append({
                "title": p['title'],
                "recommend_usd": amt,
                "est_daily_profit": round(est_profit, 2),
                "roi_pct": round((est_profit / amt) * 100, 2)
            })
            
    return sorted(result, key=lambda x: x['recommend_usd'], reverse=True)

@app.get("/logs")
async def get_logs():
    """최근 로그 20줄"""
    try:
        with open("bot.log", "r", encoding="utf-8") as f:
            lines = f.readlines()
            return lines[-20:]
    except:
        return ["로그 파일을 찾을 수 없습니다."]