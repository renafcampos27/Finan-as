"""Source-owned storage for explicit subscription charge reviews."""

from __future__ import annotations

from typing import Any


def install_subscription_radar_schema(database: Any) -> None:
    database.run_schema_script(
        """
        CREATE TABLE IF NOT EXISTS subscription_price_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscription_id INTEGER,
            transaction_id INTEGER,
            evidence_key TEXT NOT NULL UNIQUE
                CHECK(LENGTH(evidence_key)=64 AND evidence_key NOT GLOB '*[^0-9a-f]*'),
            evidence_json TEXT NOT NULL
                CHECK(JSON_VALID(evidence_json) AND JSON_TYPE(evidence_json)='object'),
            classification TEXT NOT NULL DEFAULT 'unclassified'
                CHECK(classification IN ('unclassified','price','plan','matching')),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','accepted','rejected')),
            confirmed_period TEXT NOT NULL DEFAULT ''
                CHECK(confirmed_period IN ('','Mensal','Trimestral','Semestral','Anual')),
            confirmed_currency TEXT NOT NULL DEFAULT ''
                CHECK(confirmed_currency='' OR
                    (LENGTH(confirmed_currency)=3 AND confirmed_currency NOT GLOB '*[^A-Z]*')),
            match_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(match_confirmed IN (0,1)),
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK(status<>'accepted' OR classification<>'unclassified'),
            CHECK(status<>'accepted' OR classification<>'price' OR (
                match_confirmed=1 AND confirmed_currency='EUR'
                AND confirmed_period IN ('Mensal','Trimestral','Semestral','Anual')
                AND confirmed_period=COALESCE(JSON_EXTRACT(evidence_json,'$.subscription.period'),'')
            )),
            FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE SET NULL,
            FOREIGN KEY(transaction_id) REFERENCES transactions(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_subscription_price_reviews_state
            ON subscription_price_reviews(status,subscription_id,id);
        """
    )
