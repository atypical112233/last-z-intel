# Last Z Intel v6 Cloud

State-selectable player intelligence dashboard.


## Deploy
Render Blueprint from `render.yaml`.

## State selection
Enter a State/server number from 1 to 1000. The fetch URL is `https://last-z.us/rankings?playerServers=STATE`.

## Limitations
The source is a cached cross-server ranking snapshot and may only expose indexed ranking players, not a complete state roster. The parser has been made more source-aware but cannot be live-tested from this build environment. Empty extraction is now reported as `empty`, not `success`.
SQLite on Render Free uses ephemeral storage unless durable storage is configured.
