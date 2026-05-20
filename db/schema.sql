CREATE TABLE urls (
  id           BIGSERIAL PRIMARY KEY,
  short_code   TEXT UNIQUE NOT NULL,
  original_url TEXT NOT NULL,
  webhook_url  TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE clicks (
  id         BIGSERIAL PRIMARY KEY,
  url_id     BIGINT NOT NULL REFERENCES urls(id) ON DELETE CASCADE,
  clicked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ip         INET,
  user_agent TEXT,
  country    TEXT
);

CREATE INDEX clicks_url_id_idx ON clicks (url_id);
