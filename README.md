# Last Z Intel v5 Cloud

A FastAPI-based prototype dashboard for Server 66 player intel.

## Cloud deploy (Render)
1. Upload this folder to a GitHub repository.
2. In Render, choose New > Blueprint and connect the repository.
3. Render reads `render.yaml`; deploy the web service.
4. Open the generated public HTTPS URL.

**Free plan caveats:** Free web services can sleep after inactivity; Render's free filesystem is ephemeral, so the SQLite DB may be lost on restarts/deploys. For durable data, configure a managed PostgreSQL database and migrate from SQLite. Free Render Postgres currently expires after 30 days. See Render docs.

## Local Windows
Run `run_windows.bat` on Windows (not Android). Visit http://127.0.0.1:8000.

## Source configuration
Set environment variable `SOURCE_URL` to the permitted source page or JSON endpoint. The current parser is diagnostic and generic; site-specific source/API parsing may be needed. Do not bypass access restrictions or rate limits.
