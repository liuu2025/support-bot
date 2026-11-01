"""An isolated, fixed-identity local workspace. Never available in production."""

import uuid
from datetime import timedelta
from decimal import Decimal

from fastapi import APIRouter, HTTPException

from app.auth import mint_token
from app.config import get_settings
from app.db import session as db
from app.db.models import Customer, Order, utcnow
from app.demo import order_out

router = APIRouter(prefix="/api/local", tags=["本地演示"])


@router.post("/session")
async def create_local_session():
    settings = get_settings()
    if settings.env != "dev" or settings.auth_mode != "dev":
        raise HTTPException(404, "Not Found")
    cid = f"DM-{uuid.uuid4().hex[:16]}"
    now = utcnow()
    threshold = Decimal(str(settings.escalation_threshold))
    normal = min(Decimal("129.99"), max(Decimal("0.01"), threshold / 2)).quantize(Decimal("0.01"))
    rows = [
        ("A", "无线降噪耳机", normal, False),
        ("B", "清仓连帽卫衣", Decimal("59.00"), True),
        ("C", "轻薄笔记本电脑", max(Decimal("1299.00"), threshold + 100), False),
    ]
    customer = Customer(id=cid, name="林晓", email=f"{cid.lower()}@demo.zhixu.test")
    orders = [
        Order(
            id=f"{cid}-{suffix}",
            customer_id=cid,
            product_name=product,
            category="general",
            amount=amount,
            status="delivered",
            order_date=now - timedelta(days=8),
            delivered_date=now - timedelta(days=max(0, min(6, settings.return_window_days))),
            is_final_sale=final_sale,
            refunded=False,
        )
        for suffix, product, amount, final_sale in rows
    ]
    async with db.SessionLocal() as session:
        session.add(customer)
        session.add_all(orders)
        await session.commit()
    return {
        "customer": {"id": cid, "name": customer.name},
        "orders": [order_out(order).model_dump(mode="json") for order in orders],
        "token": mint_token(cid, ttl_s=settings.dev_token_ttl_s),
        "expires_in": settings.dev_token_ttl_s,
    }
