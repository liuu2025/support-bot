"""Localize built-in sample customers and products without changing business data.

Only original fixture ids AND original values are updated. Amounts, order status,
refunds, and customer edits are left intact. Existing conversation text is retained.
"""

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

CHANGES = [
    ("customers", "CUST-001", "name", "Alice Johnson", "林晓"),
    ("customers", "CUST-001", "email", "alice@example.com", "customer001@example.test"),
    ("customers", "CUST-002", "name", "Bob Smith", "周宁"),
    ("customers", "CUST-002", "email", "bob@example.com", "customer002@example.test"),
    ("customers", "CUST-003", "name", "Carol Davis", "陈晨"),
    ("customers", "CUST-003", "email", "carol@example.com", "customer003@example.test"),
    ("customers", "CUST-004", "name", "David Lee", "李明"),
    ("customers", "CUST-004", "email", "david@example.com", "customer004@example.test"),
    ("customers", "CUST-005", "name", "Emma Wilson", "许悦"),
    ("customers", "CUST-005", "email", "emma@example.com", "customer005@example.test"),
    ("customers", "CUST-006", "name", "Frank Miller", "王凯"),
    ("customers", "CUST-006", "email", "frank@example.com", "customer006@example.test"),
    ("customers", "CUST-007", "name", "Grace Chen", "赵晴"),
    ("customers", "CUST-007", "email", "grace@example.com", "customer007@example.test"),
    ("customers", "CUST-008", "name", "Henry Brown", "孙浩"),
    ("customers", "CUST-008", "email", "henry@example.com", "customer008@example.test"),
    ("customers", "CUST-009", "name", "Iris Taylor", "吴桐"),
    ("customers", "CUST-009", "email", "iris@example.com", "customer009@example.test"),
    ("customers", "CUST-010", "name", "Jack Anderson", "郑宇"),
    ("customers", "CUST-010", "email", "jack@example.com", "customer010@example.test"),
    ("customers", "CUST-011", "name", "Karen White", "冯雪"),
    ("customers", "CUST-011", "email", "karen@example.com", "customer011@example.test"),
    ("customers", "CUST-012", "name", "Liam Garcia", "沈言"),
    ("customers", "CUST-012", "email", "liam@example.com", "customer012@example.test"),
    ("customers", "CUST-013", "name", "Mia Martinez", "顾念"),
    ("customers", "CUST-013", "email", "mia@example.com", "customer013@example.test"),
    ("customers", "CUST-014", "name", "Noah Rodriguez", "何川"),
    ("customers", "CUST-014", "email", "noah@example.com", "customer014@example.test"),
    ("customers", "CUST-015", "name", "Olivia Hernandez", "唐可"),
    ("customers", "CUST-015", "email", "olivia@example.com", "customer015@example.test"),
    ("orders", "ORD-1001", "product_name", "Wireless Headphones", "无线降噪耳机"),
    ("orders", "ORD-1002", "product_name", "Clearance Graphic T-Shirt", "清仓印花短袖"),
    ("orders", "ORD-1003", "product_name", '55" 4K OLED TV', "55英寸高清电视"),
    ("orders", "ORD-1004", "product_name", "Bluetooth Speaker", "便携蓝牙音箱"),
    ("orders", "ORD-1005", "product_name", "Trail Running Shoes", "轻量越野跑鞋"),
    ("orders", "ORD-1006", "product_name", "Drip Coffee Maker", "家用滴滤咖啡机"),
    ("orders", "ORD-1007", "product_name", "Leather Designer Handbag", "真皮通勤手提包"),
    ("orders", "ORD-1008", "product_name", "Silicone Phone Case", "硅胶手机壳"),
    ("orders", "ORD-1009", "product_name", "Final Sale Winter Jacket", "清仓保暖外套"),
    ("orders", "ORD-1010", "product_name", "Aluminum Laptop Stand", "铝合金笔记本支架"),
    ("orders", "ORD-1011", "product_name", "Gaming Console", "家用游戏主机"),
    ("orders", "ORD-1012", "product_name", "Extra Wireless Controller", "无线游戏手柄"),
    ("orders", "ORD-1013", "product_name", "Diamond Stud Earrings", "简约钻石耳钉"),
    ("orders", "ORD-1014", "product_name", "Premium Yoga Mat", "防滑瑜伽垫"),
    ("orders", "ORD-1015", "product_name", "Smart Watch Series 7", "智能运动手表"),
    ("orders", "ORD-1016", "product_name", "Clearance Watch Band", "清仓手表表带"),
    ("orders", "ORD-1017", "product_name", "Ergonomic Office Chair", "人体工学办公椅"),
    ("orders", "ORD-1018", "product_name", "Electric Standing Desk", "电动升降书桌"),
    ("orders", "ORD-1019", "product_name", "Espresso Machine", "家用意式咖啡机"),
    ("orders", "ORD-1020", "product_name", "Handheld Milk Frother", "手持电动打奶器"),
]


def _apply(reverse=False):
    connection = op.get_bind()
    for table, identifier, field, old, new in CHANGES:
        before, after = (new, old) if reverse else (old, new)
        connection.execute(
            sa.text(
                f"UPDATE {table} SET {field} = :after WHERE id = :identifier AND {field} = :before"
            ),
            {"after": after, "identifier": identifier, "before": before},
        )


def upgrade():
    _apply()


def downgrade():
    _apply(reverse=True)
