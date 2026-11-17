CREATE TABLE IF NOT EXISTS laptops (
    offer_no          text PRIMARY KEY,
    brand             text NOT NULL,
    model             text NOT NULL,
    cpu               text,
    ram_gb            integer CHECK (ram_gb > 0),
    ram_is_min        boolean NOT NULL DEFAULT false,
    storage_gb        integer CHECK (storage_gb > 0),
    storage_is_min    boolean NOT NULL DEFAULT false,
    screen_inch       numeric(4,1) CHECK (screen_inch > 0),
    screen_is_min     boolean NOT NULL DEFAULT false,
    resolution        text,
    gpu               text,
    os                text,
    country           text NOT NULL,
    price_uzs         bigint NOT NULL CHECK (price_uzs > 0),
    stock_qty         integer NOT NULL CHECK (stock_qty >= 0),
    warranty_months   integer CHECK (warranty_months > 0),
    delivery_days     integer CHECK (delivery_days > 0),
    offer_valid_until date NOT NULL,
    description       text NOT NULL,
    source_url        text NOT NULL
);

CREATE TABLE IF NOT EXISTS shop_policies (
    policy_id text PRIMARY KEY,
    topic     text NOT NULL,
    rule_text text NOT NULL
);