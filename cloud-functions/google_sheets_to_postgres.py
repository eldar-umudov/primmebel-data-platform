import os
import json
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from sqlalchemy import create_engine, text


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_postgres_engine():
    user = os.environ["NEON_USER"]
    password = os.environ["NEON_PASSWORD"]
    host = os.environ["NEON_HOST"]
    port = os.environ.get("NEON_PORT", "5432")
    database = os.environ["NEON_DATABASE"]
    sslmode = os.environ.get("NEON_SSLMODE", "require")

    database_url = (
        f"postgresql+psycopg2://{user}:{password}"
        f"@{host}:{port}/{database}?sslmode={sslmode}"
    )

    return create_engine(database_url)


def handler(event, context):
    service_account_info = json.loads(
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    )

    creds = Credentials.from_service_account_info(
        service_account_info,
        scopes=SCOPES
    )

    gc = gspread.authorize(creds)

    sheet_name = os.environ.get("GOOGLE_SHEET_NAME", "sales_clean")
    sheet = gc.open(sheet_name)
    worksheet = sheet.sheet1

    data = worksheet.get_all_records()
    df = pd.DataFrame(data)

    df = df.replace("", pd.NA)

    text_cols = [
        "product_name", "category", "client_name", "phone", "supplier",
        "payment_type", "status", "address", "district", "comment"
    ]

    for col in text_cols:
        if col in df.columns:
            df[col] = df[col].astype("string").str.strip()

    if "order_date" in df.columns:
        df["order_date"] = pd.to_datetime(df["order_date"], errors="coerce")

    num_cols = [
        "sale_price", "cost_price", "profit",
        "margin", "delivery_price", "quantity"
    ]

    for col in num_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    engine = get_postgres_engine()

    df.to_sql(
        "sales_clean",
        con=engine,
        if_exists="replace",
        index=False
    )

    sql_steps = [
        "DROP TABLE IF EXISTS bi_fact_sales",
        "DROP TABLE IF EXISTS bi_dim_date",
        "DROP TABLE IF EXISTS bi_dim_product",
        "DROP TABLE IF EXISTS bi_dim_client",

        "ALTER TABLE sales_clean DROP COLUMN IF EXISTS sale_id",

        """
        ALTER TABLE sales_clean
        ADD COLUMN sale_id SERIAL PRIMARY KEY
        """,

        """
        CREATE TABLE bi_dim_date (
            date_id SERIAL PRIMARY KEY,
            order_date DATE,
            sale_year INT,
            month_number INT,
            sale_year_month VARCHAR(20)
        )
        """,

        """
        INSERT INTO bi_dim_date (order_date, sale_year, month_number, sale_year_month)
        SELECT DISTINCT
            order_date::date,
            EXTRACT(YEAR FROM order_date)::int,
            EXTRACT(MONTH FROM order_date)::int,
            TO_CHAR(order_date, 'YYYY-MM')
        FROM sales_clean
        WHERE order_date IS NOT NULL
        """,

        """
        CREATE TABLE bi_dim_product (
            product_id SERIAL PRIMARY KEY,
            product_name VARCHAR(255),
            category VARCHAR(100),
            supplier VARCHAR(100)
        )
        """,

        """
        INSERT INTO bi_dim_product (product_name, category, supplier)
        SELECT DISTINCT
            product_name,
            category,
            supplier
        FROM sales_clean
        WHERE product_name IS NOT NULL
        """,

        """
        CREATE TABLE bi_dim_client (
            client_id SERIAL PRIMARY KEY,
            client_name VARCHAR(255),
            phone VARCHAR(50)
        )
        """,

        """
        INSERT INTO bi_dim_client (client_name, phone)
        SELECT DISTINCT
            client_name,
            phone
        FROM sales_clean
        WHERE client_name IS NOT NULL
           OR phone IS NOT NULL
        """,

        """
        CREATE TABLE bi_fact_sales AS
        SELECT
            sc.sale_id,
            dd.date_id,
            dp.product_id,
            dc.client_id,
            sc.sale_price,
            sc.cost_price,
            sc.profit,
            sc.margin,
            sc.delivery_price,
            sc.quantity,
            sc.payment_type,
            sc.status
        FROM sales_clean sc
        LEFT JOIN bi_dim_date dd
            ON sc.order_date::date = dd.order_date
        LEFT JOIN bi_dim_product dp
            ON sc.product_name IS NOT DISTINCT FROM dp.product_name
           AND sc.category IS NOT DISTINCT FROM dp.category
           AND sc.supplier IS NOT DISTINCT FROM dp.supplier
        LEFT JOIN bi_dim_client dc
            ON sc.client_name IS NOT DISTINCT FROM dc.client_name
           AND sc.phone IS NOT DISTINCT FROM dc.phone
        """
    ]

    with engine.begin() as conn:
        for step in sql_steps:
            conn.execute(text(step))

    return {
        "statusCode": 200,
        "body": f"OK. Loaded {len(df)} rows from Google Sheets to Neon PostgreSQL."
