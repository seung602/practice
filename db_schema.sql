-- Reference schema. The application creates/migrates this automatically.
-- SQLite database: beauty_catalog.db

SELECT name
FROM sqlite_master
WHERE type='table'
ORDER BY name;
