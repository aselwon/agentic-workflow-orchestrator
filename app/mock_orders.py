from fastapi import FastAPI, HTTPException

app = FastAPI(title="OpsAgent mock order service")
ORDERS = {
    "ORD-1001": {"order_id": "ORD-1001", "status": "delayed", "eta": "within 2 business days"},
    "ORD-1002": {"order_id": "ORD-1002", "status": "shipped", "eta": "next business day"},
}


@app.get("/health")
def health():
    return {"status": "ok", "mock": True}


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    if order_id not in ORDERS:
        raise HTTPException(404, "Order not found")
    return ORDERS[order_id]
