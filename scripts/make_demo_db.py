"""Create the sample database used by the tests:  python scripts/make_demo_db.py PATH"""
import os
import random
import sqlite3
import sys
import zlib
import struct


def tiny_png(r: int, g: int, b: int, size: int = 8) -> bytes:
    raw = b"".join(b"\x00" + bytes([r, g, b]) * size for _ in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def main(path: str) -> None:
    if os.path.exists(path):
        os.remove(path)
    rnd = random.Random(42)
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE,
            country TEXT DEFAULT 'IN',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            meta TEXT
        );
        CREATE TABLE products (
            sku TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            price REAL CHECK(price >= 0),
            stock INTEGER NOT NULL DEFAULT 0,
            image BLOB
        ) WITHOUT ROWID;
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
            sku TEXT REFERENCES products(sku),
            quantity INTEGER NOT NULL DEFAULT 1,
            ordered_at TEXT,
            total REAL GENERATED ALWAYS AS (quantity * 1.0) VIRTUAL
        );
        CREATE INDEX idx_orders_customer ON orders(customer_id);
        CREATE INDEX idx_orders_date ON orders(ordered_at DESC);
        CREATE VIEW order_summary AS
            SELECT c.name AS customer, COUNT(o.id) AS orders, SUM(o.quantity) AS items
            FROM customers c LEFT JOIN orders o ON o.customer_id = c.id GROUP BY c.id;
        CREATE TRIGGER trg_stock AFTER INSERT ON orders BEGIN
            UPDATE products SET stock = stock - NEW.quantity WHERE sku = NEW.sku;
        END;
    """)
    first = ["Aarav", "Diya", "Kabir", "Meera", "Rohan", "Ananya", "Vihaan", "Isha", "Arjun", "Sara", "Liam", "Emma"]
    last = ["Sharma", "Patel", "Iyer", "Khan", "Singh", "Das", "Smith", "Garcia", "Müller", "Rossi"]
    countries = ["IN", "US", "DE", "IT", "GB", "JP", "BR"]
    for i in range(1, 501):
        n = f"{rnd.choice(first)} {rnd.choice(last)}"
        meta = None if i % 7 else '{"vip": true, "tags": ["early", "beta"], "score": %d}' % rnd.randint(1, 100)
        con.execute("INSERT INTO customers(name, email, country, created_at, meta) VALUES (?,?,?,?,?)",
                    (n, f"user{i}@example.com", rnd.choice(countries), f"2024-{rnd.randint(1, 12):02d}-{rnd.randint(1, 28):02d}", meta))
    colors = [(239, 68, 68), (59, 130, 246), (16, 185, 129), (234, 179, 8), (168, 85, 247)]
    for i in range(1, 41):
        con.execute("INSERT INTO products VALUES (?,?,?,?,?)",
                    (f"SKU-{i:04d}", f"Product {i}", round(rnd.uniform(1, 500), 2), rnd.randint(4000, 9000),
                     tiny_png(*colors[i % len(colors)]) if i % 3 == 0 else None))
    con.executemany("INSERT INTO orders(customer_id, sku, quantity, ordered_at) VALUES (?,?,?,?)",
                    [(rnd.randint(1, 500), f"SKU-{rnd.randint(1, 40):04d}", rnd.randint(1, 5),
                      f"2025-{rnd.randint(1, 12):02d}-{rnd.randint(1, 28):02d}") for _ in range(20000)])
    con.commit()
    con.close()
    print(f"Created {path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python scripts/make_demo_db.py PATH")
    main(sys.argv[1])
