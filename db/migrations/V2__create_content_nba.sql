-- NBA highlight shorts (workflow n8n nba_reel_pipeline, kênh NBA Game Turn On).
--
-- * clips: danh mục các clip Storyteller đã được chọn (giữ metadata như lúc nhận được để
--   còn kiểm lại quyết định chọn clip về sau).
-- * reels: một dòng sản xuất cho mỗi clip (UNIQUE clip_id = một clip không bao giờ làm hai lần;
--   dòng 'failed' được lượt chạy sau làm lại).
-- * reel_publications: một dòng cho mỗi (reel, kênh) với id bài trên nền tảng và giờ đăng đã đặt
--   (video được đặt lịch, không public ngay).
-- Đích đăng dùng chung bảng content.channels (topic = 'nba').
--
-- media_key (voice/music/video) là đường dẫn TƯƠNG ĐỐI trong thư mục media local
-- (MEDIA_DIR, mặc định ./data/media trên host, /data/media trong container), vd.
-- nba/<reel_id>/reel.mp4. DB chỉ lưu key, không lưu bytes.

CREATE SCHEMA IF NOT EXISTS content_nba;

CREATE TABLE IF NOT EXISTS content_nba.clips (
    clip_id       TEXT PRIMARY KEY,           -- Storyteller clip id
    description   TEXT NOT NULL,
    mp4_url       TEXT NOT NULL,
    thumbnail_url TEXT,
    duration      NUMERIC(6,2),
    like_count    INTEGER,
    share_count   INTEGER,
    players       TEXT[] NOT NULL DEFAULT '{}',
    teams         TEXT[] NOT NULL DEFAULT '{}',
    categories    TEXT[] NOT NULL DEFAULT '{}',
    source_score  NUMERIC(10,3),
    created_at    TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS content_nba.reels (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clip_id          TEXT NOT NULL UNIQUE REFERENCES content_nba.clips(clip_id) ON DELETE CASCADE,
    run_date         DATE NOT NULL,
    rank             SMALLINT NOT NULL,
    status           VARCHAR(30) NOT NULL DEFAULT 'selected'
                     CHECK (status IN ('selected','scripted','voiced','music_ready','rendering',
                                       'video_ready','scheduled','published','failed')),
    score            JSONB,       -- {total, heuristic, llm, llm_reason, ...}
    script           JSONB,       -- {title, description, hashtags, tags, hook, commentary, cta, music}
    voice            JSONB,       -- {segments: [{id, key, duration, words}]}
    music_key        TEXT,        -- nba/<reel_id>/music.mp3
    video_key        TEXT,        -- nba/<reel_id>/reel.mp4
    duration_seconds NUMERIC(6,2),
    publish_at       TIMESTAMP WITH TIME ZONE,   -- slot đã lên kế hoạch
    error_message    TEXT,
    created_at       TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at       TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_nba_reels_status   ON content_nba.reels(status);
CREATE INDEX IF NOT EXISTS idx_nba_reels_run_date ON content_nba.reels(run_date DESC);

CREATE OR REPLACE TRIGGER trg_nba_reels_updated_at
    BEFORE UPDATE ON content_nba.reels
    FOR EACH ROW EXECUTE FUNCTION content.update_updated_at();

CREATE TABLE IF NOT EXISTS content_nba.reel_publications (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reel_id          UUID NOT NULL REFERENCES content_nba.reels(id) ON DELETE CASCADE,
    channel_id       UUID NOT NULL REFERENCES content.channels(id) ON DELETE RESTRICT,
    status           VARCHAR(20) NOT NULL CHECK (status IN ('scheduled','published','failed','skipped')),
    platform_post_id TEXT,
    url              TEXT,
    scheduled_for    TIMESTAMP WITH TIME ZONE,
    error_message    TEXT,
    created_at       TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at       TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE (reel_id, channel_id)
);

CREATE OR REPLACE TRIGGER trg_nba_reel_publications_updated_at
    BEFORE UPDATE ON content_nba.reel_publications
    FOR EACH ROW EXECUTE FUNCTION content.update_updated_at();

-- ─── Clip id không được làm lại ────────────────────────────────────────────
-- Mọi clip đã có reel chưa thất bại. Reel 'failed', và reel đứng im ở một bước sản xuất
-- quá 12 giờ (lượt chạy bị crash), được phép làm lại.
CREATE OR REPLACE FUNCTION content_nba.is_retryable(p_status TEXT, p_updated_at TIMESTAMPTZ) RETURNS BOOLEAN
LANGUAGE sql IMMUTABLE AS $$
    SELECT p_status = 'failed'
        OR (p_status IN ('selected','scripted','voiced','music_ready','rendering','video_ready')
            AND p_updated_at < NOW() - INTERVAL '12 hours');
$$;

CREATE OR REPLACE FUNCTION content_nba.used_clip_ids() RETURNS TEXT[]
LANGUAGE sql STABLE AS $$
    SELECT COALESCE(array_agg(clip_id), '{}') FROM content_nba.reels
     WHERE NOT content_nba.is_retryable(status, updated_at);
$$;

-- ─── Lưu các clip một lượt chạy đã chọn ────────────────────────────────────
-- p_selected: [{clip_id, description, mp4_url, thumbnail_url, duration, like_count, share_count,
--               players[], teams[], categories[], source_score, rank, score}]
-- Tạo một reel 'selected' cho mỗi clip (reel được phép làm lại thì reset). Clip đã có reel còn
-- sống thì bỏ qua, nên hai lượt chạy chồng nhau không thể làm cùng một clip.
-- Trả về [{reel_id, clip_id, rank}] của những reel lượt chạy này sở hữu.
CREATE OR REPLACE FUNCTION content_nba.save_selection(
    p_run_date DATE,
    p_selected JSONB
) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_item   JSONB;
    v_reel   UUID;
    v_result JSONB := '[]'::jsonb;
BEGIN
    IF jsonb_typeof(p_selected) <> 'array' THEN
        RAISE EXCEPTION 'p_selected must be an array';
    END IF;

    FOR v_item IN SELECT value FROM jsonb_array_elements(p_selected) LOOP
        INSERT INTO content_nba.clips
            (clip_id, description, mp4_url, thumbnail_url, duration, like_count, share_count,
             players, teams, categories, source_score)
        VALUES (
            v_item->>'clip_id', v_item->>'description', v_item->>'mp4_url', v_item->>'thumbnail_url',
            (v_item->>'duration')::numeric, (v_item->>'like_count')::int, (v_item->>'share_count')::int,
            COALESCE(ARRAY(SELECT jsonb_array_elements_text(v_item->'players')), '{}'),
            COALESCE(ARRAY(SELECT jsonb_array_elements_text(v_item->'teams')), '{}'),
            COALESCE(ARRAY(SELECT jsonb_array_elements_text(v_item->'categories')), '{}'),
            (v_item->>'source_score')::numeric
        )
        ON CONFLICT (clip_id) DO UPDATE
            SET mp4_url = EXCLUDED.mp4_url, thumbnail_url = EXCLUDED.thumbnail_url,
                like_count = EXCLUDED.like_count, share_count = EXCLUDED.share_count;

        v_reel := NULL;
        INSERT INTO content_nba.reels (clip_id, run_date, rank, score)
        VALUES (v_item->>'clip_id', p_run_date, (v_item->>'rank')::smallint, v_item->'score')
        ON CONFLICT (clip_id) DO UPDATE
            SET run_date = EXCLUDED.run_date, rank = EXCLUDED.rank, score = EXCLUDED.score,
                status = 'selected', script = NULL, voice = NULL, music_key = NULL, video_key = NULL,
                duration_seconds = NULL, publish_at = NULL, error_message = NULL
            WHERE content_nba.is_retryable(content_nba.reels.status, content_nba.reels.updated_at)
        RETURNING id INTO v_reel;

        IF v_reel IS NOT NULL THEN
            v_result := v_result || jsonb_build_array(jsonb_build_object(
                'reel_id', v_reel, 'clip_id', v_item->>'clip_id', 'rank', (v_item->>'rank')::int));
        END IF;
    END LOOP;

    RETURN v_result;
END;
$$;

-- ─── Tiến trình của một reel ───────────────────────────────────────────────
-- p_data (mọi khoá đều tuỳ chọn): script, voice, music_key, video_key, duration, publish_at.
CREATE OR REPLACE FUNCTION content_nba.set_reel(
    p_id     UUID,
    p_status TEXT,
    p_data   JSONB DEFAULT '{}'::jsonb
) RETURNS JSONB
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE content_nba.reels
       SET status           = p_status,
           script           = COALESCE(p_data->'script', script),
           voice            = COALESCE(p_data->'voice', voice),
           music_key        = COALESCE(p_data->>'music_key', music_key),
           video_key        = COALESCE(p_data->>'video_key', video_key),
           duration_seconds = COALESCE((p_data->>'duration')::numeric, duration_seconds),
           publish_at       = COALESCE((p_data->>'publish_at')::timestamptz, publish_at),
           error_message    = CASE WHEN p_status = 'failed' THEN LEFT(p_data->>'error', 4000) ELSE NULL END
     WHERE id = p_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown reel %', p_id;
    END IF;
    RETURN jsonb_build_object('reel_id', p_id, 'status', p_status);
END;
$$;

-- ─── Kết quả đăng một reel ─────────────────────────────────────────────────
-- p_results: [{channel, status: 'scheduled'|'published'|'failed'|'skipped', post_id, url,
--              scheduled_for, error}]
-- Reel thành 'scheduled'/'published' ngay khi một kênh thành công (làm lại sẽ đăng trùng ở kênh đó;
-- lỗi của kênh khác nằm ở error_message / reel_publications), 'failed' khi không kênh nào thành công
-- mà có kênh lỗi (clip được lượt sau làm lại); mọi kênh skipped thì giữ 'video_ready'.
CREATE OR REPLACE FUNCTION content_nba.finish_reel(
    p_id      UUID,
    p_results JSONB
) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_res     JSONB;
    v_chan    UUID;
    v_errors  TEXT[] := '{}';
    v_ok      INTEGER := 0;
    v_pub     INTEGER := 0;
BEGIN
    IF jsonb_typeof(p_results) <> 'array' OR jsonb_array_length(p_results) = 0 THEN
        RAISE EXCEPTION 'p_results must be a non-empty array';
    END IF;

    FOR v_res IN SELECT value FROM jsonb_array_elements(p_results) LOOP
        SELECT id INTO v_chan FROM content.channels WHERE code = v_res->>'channel';
        IF NOT FOUND THEN
            RAISE EXCEPTION 'unknown channel code "%" in publish results', v_res->>'channel';
        END IF;

        INSERT INTO content_nba.reel_publications
            (reel_id, channel_id, status, platform_post_id, url, scheduled_for, error_message)
        VALUES (p_id, v_chan, v_res->>'status', v_res->>'post_id', v_res->>'url',
                (v_res->>'scheduled_for')::timestamptz, LEFT(v_res->>'error', 4000))
        ON CONFLICT (reel_id, channel_id) DO UPDATE
            SET status = EXCLUDED.status, platform_post_id = EXCLUDED.platform_post_id, url = EXCLUDED.url,
                scheduled_for = EXCLUDED.scheduled_for, error_message = EXCLUDED.error_message;

        IF v_res->>'status' IN ('scheduled', 'published') THEN
            v_ok := v_ok + 1;
            IF v_res->>'status' = 'published' THEN v_pub := v_pub + 1; END IF;
        ELSIF v_res->>'status' = 'failed' THEN
            v_errors := v_errors || format('%s: %s', v_res->>'channel', COALESCE(NULLIF(v_res->>'error', ''), 'publish failed'));
        END IF;
    END LOOP;

    UPDATE content_nba.reels
       SET status = CASE WHEN v_ok = 0 AND cardinality(v_errors) > 0 THEN 'failed'
                         WHEN v_ok = 0 THEN 'video_ready'      -- mọi kênh đều skipped
                         WHEN v_pub = v_ok THEN 'published' ELSE 'scheduled' END,
           error_message = CASE WHEN cardinality(v_errors) > 0 THEN LEFT(array_to_string(v_errors, E'\n'), 4000) END
     WHERE id = p_id;

    RETURN jsonb_build_object('reel_id', p_id, 'ok', cardinality(v_errors) = 0, 'errors', to_jsonb(v_errors));
END;
$$;

-- Xem nhanh reel nào đã đặt lịch / lỗi.
CREATE OR REPLACE VIEW content_nba.reel_overview AS
SELECT r.run_date, r.rank, r.status, c.description, c.players, r.publish_at, r.video_key, r.error_message,
       (SELECT jsonb_agg(jsonb_build_object('channel', ch.code, 'status', p.status, 'url', p.url,
                                            'scheduled_for', p.scheduled_for))
          FROM content_nba.reel_publications p
          JOIN content.channels ch ON ch.id = p.channel_id
         WHERE p.reel_id = r.id) AS publications
FROM content_nba.reels r
JOIN content_nba.clips c ON c.clip_id = r.clip_id
ORDER BY r.run_date DESC, r.rank;
