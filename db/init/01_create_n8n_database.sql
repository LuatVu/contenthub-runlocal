-- Chạy MỘT LẦN khi volume Postgres được khởi tạo (docker-entrypoint-initdb.d).
-- n8n lưu workflow/execution/credential của nó ở database riêng để không lẫn với dữ liệu nội dung.
SELECT 'CREATE DATABASE n8n'
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'n8n')\gexec
