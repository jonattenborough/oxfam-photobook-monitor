ALTER TABLE reviews ADD COLUMN origin_key TEXT;
CREATE UNIQUE INDEX reviews_origin_key_idx ON reviews(origin_key) WHERE origin_key IS NOT NULL;
