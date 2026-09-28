CREATE INDEX listings_platform_external_idx ON listings(platform,external_id);
CREATE INDEX reviews_listing_idx ON reviews(listing_id,finished_at DESC);
