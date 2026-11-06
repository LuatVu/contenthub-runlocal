-- Nền tảng dùng chung cho mọi pipeline nội dung chạy local (docker compose).
--
-- * extension `vector` (pgvector): dành cho các pipeline cần embedding (crawl tin tức…).
--   Luồng NBA chưa dùng, nhưng tạo sẵn để không phải sửa migration sau.
-- * schema `content`: thông tin DÙNG CHUNG giữa các pipeline.
--   - channels: mỗi dòng là một đích đăng (1 account trên 1 nền tảng: Facebook Page,
--     YouTube channel, …). Bảng giữ page/channel id và **tên** biến môi trường chứa token
--     (credential_key), không bao giờ giữ token. Workflow n8n chỉ tham chiếu kênh qua `code`.
--   - bind_channels(): mỗi (kênh, định dạng) chỉ thuộc một workflow n8n; workflow copy dùng
--     nhầm kênh sẽ lỗi thay vì đăng sai chỗ.
-- * Dữ liệu riêng của từng pipeline nằm ở schema riêng: content_nba (V2), …

CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS content;

-- ─── Trigger updated_at dùng chung ─────────────────────────────────────────
CREATE OR REPLACE FUNCTION content.update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ─── Kênh đăng (publishing targets) ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS content.channels (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code              VARCHAR(60) NOT NULL UNIQUE
                      CHECK (code ~ '^[a-z0-9][a-z0-9_-]{1,59}$'),   -- vd. nba-yt
    topic             VARCHAR(50) NOT NULL,                          -- vd. nba
    platform          VARCHAR(30) NOT NULL
                      CHECK (platform IN ('facebook','youtube','tiktok','instagram','threads')),
    external_id       TEXT NOT NULL,         -- Page id / YouTube channel id / IG user id …
    handle            TEXT,                  -- tên hiển thị cho người vận hành
    credential_key    TEXT NOT NULL          -- TÊN biến môi trường trong .env, không phải token
                      CHECK (credential_key ~ '^[A-Z][A-Z0-9_]*$'),
    formats           TEXT[] NOT NULL,
    platform_settings JSONB NOT NULL DEFAULT '{}'::jsonb,  -- tuỳ chọn đăng theo nền tảng
    content_profile   JSONB NOT NULL DEFAULT '{}'::jsonb,  -- ghi đè tham số của workflow
    workflow_bindings JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {"reel": {id, name, bound_at}, …}
    enabled           BOOLEAN NOT NULL DEFAULT TRUE,
    notes             TEXT,
    created_at        TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at        TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE (platform, external_id),
    -- Định dạng mỗi nền tảng hỗ trợ (post = ảnh + chữ, reel = video dọc ngắn).
    CONSTRAINT channels_formats_supported CHECK (
        cardinality(formats) > 0
        AND formats <@ (CASE platform
                            WHEN 'facebook'  THEN ARRAY['post','reel']
                            WHEN 'instagram' THEN ARRAY['post','reel']
                            WHEN 'tiktok'    THEN ARRAY['post','reel']
                            WHEN 'youtube'   THEN ARRAY['reel']
                            WHEN 'threads'   THEN ARRAY['post']
                        END)::text[]
    ),
    CONSTRAINT channels_json_objects CHECK (
        jsonb_typeof(platform_settings) = 'object'
        AND jsonb_typeof(content_profile) = 'object'
        AND jsonb_typeof(workflow_bindings) = 'object'
    )
);

CREATE INDEX IF NOT EXISTS idx_channels_topic ON content.channels(topic, platform);

CREATE OR REPLACE TRIGGER trg_channels_updated_at
    BEFORE UPDATE ON content.channels
    FOR EACH ROW EXECUTE FUNCTION content.update_updated_at();

-- ─── Gắn kênh với workflow ─────────────────────────────────────────────────
-- Kiểm tra các kênh một lượt chạy nhắm tới và gắn mỗi (kênh, định dạng) với workflow n8n
-- gọi nó ở lần chạy đầu. Lỗi khi: code không tồn tại, kênh bị tắt, kênh không nhận định dạng,
-- các kênh khác chủ đề, hoặc (kênh, định dạng) đã thuộc workflow khác.
-- Trả về {topic, format, channels: [{code, platform, external_id, handle, credential_key,
-- platform_settings}], profile} — profile trộn content_profile của các kênh (kênh đứng trước thắng).
CREATE OR REPLACE FUNCTION content.bind_channels(
    p_codes         TEXT[],
    p_format        TEXT,
    p_workflow_id   TEXT,
    p_workflow_name TEXT DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_code     TEXT;
    v_ch       content.channels%ROWTYPE;
    v_owner    JSONB;
    v_topic    TEXT;
    v_channels JSONB := '[]'::jsonb;
    v_profile  JSONB := '{}'::jsonb;
BEGIN
    IF p_format NOT IN ('post', 'reel') THEN
        RAISE EXCEPTION 'unknown format %', p_format;
    END IF;
    IF COALESCE(cardinality(p_codes), 0) = 0 THEN
        RAISE EXCEPTION 'channel_codes must not be empty';
    END IF;
    IF (SELECT COUNT(DISTINCT c) FROM unnest(p_codes) AS c) <> cardinality(p_codes) THEN
        RAISE EXCEPTION 'channel_codes contains duplicates: %', p_codes;
    END IF;
    IF COALESCE(btrim(p_workflow_id), '') = '' THEN
        RAISE EXCEPTION 'workflow id is required to bind channels';
    END IF;

    FOREACH v_code IN ARRAY p_codes LOOP
        SELECT * INTO v_ch FROM content.channels WHERE code = v_code FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'unknown channel code "%" (see content.channels)', v_code;
        END IF;
        IF NOT v_ch.enabled THEN
            RAISE EXCEPTION 'channel "%" is disabled', v_code;
        END IF;
        IF NOT (p_format = ANY (v_ch.formats)) THEN
            RAISE EXCEPTION 'channel "%" (%) does not accept format "%" (formats: %)',
                v_code, v_ch.platform, p_format, v_ch.formats;
        END IF;
        IF v_topic IS NULL THEN
            v_topic := v_ch.topic;
        ELSIF v_topic <> v_ch.topic THEN
            RAISE EXCEPTION 'channels of one workflow must share a topic: "%" is %, expected %',
                v_code, v_ch.topic, v_topic;
        END IF;

        v_owner := v_ch.workflow_bindings -> p_format;
        IF v_owner IS NULL OR COALESCE(v_owner->>'id', '') = '' THEN
            UPDATE content.channels
               SET workflow_bindings = workflow_bindings || jsonb_build_object(p_format, jsonb_build_object(
                       'id', p_workflow_id, 'name', p_workflow_name, 'bound_at', NOW()))
             WHERE id = v_ch.id;
        ELSIF v_owner->>'id' <> p_workflow_id THEN
            RAISE EXCEPTION 'channel "%" format "%" belongs to n8n workflow % (%). One workflow per channel and format; '
                            'to move it run: UPDATE content.channels SET workflow_bindings = '
                            'workflow_bindings - ''%'' WHERE code = ''%'';',
                v_code, p_format, v_owner->>'id', COALESCE(v_owner->>'name', '?'), p_format, v_code;
        ELSIF p_workflow_name IS NOT NULL AND v_owner->>'name' IS DISTINCT FROM p_workflow_name THEN
            UPDATE content.channels
               SET workflow_bindings = jsonb_set(workflow_bindings, ARRAY[p_format, 'name'], to_jsonb(p_workflow_name))
             WHERE id = v_ch.id;
        END IF;

        v_channels := v_channels || jsonb_build_array(jsonb_build_object(
            'code', v_ch.code,
            'platform', v_ch.platform,
            'external_id', v_ch.external_id,
            'handle', v_ch.handle,
            'credential_key', v_ch.credential_key,
            'platform_settings', v_ch.platform_settings
        ));
        v_profile := v_ch.content_profile || v_profile;
    END LOOP;

    RETURN jsonb_build_object('topic', v_topic, 'format', p_format,
                              'channels', v_channels, 'profile', v_profile);
END;
$$;

-- Workflow nào đang giữ kênh nào (dùng để gỡ binding khi đổi workflow).
CREATE OR REPLACE VIEW content.channel_bindings AS
SELECT c.code, c.topic, c.platform, c.handle, c.enabled, c.formats,
       b.key AS format, b.value->>'id' AS workflow_id, b.value->>'name' AS workflow_name,
       (b.value->>'bound_at')::timestamptz AS bound_at
FROM content.channels c
LEFT JOIN LATERAL jsonb_each(c.workflow_bindings) b ON TRUE
ORDER BY c.code, b.key;
