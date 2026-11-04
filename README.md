# Dot Bili Web cloud collector

Updated: 2026-11-05 (Asia/Tokyo, UTC+09:00).

Read-only network client for the existing Dot Bili Web Site. It runs on a cloud web service, not the user's computer. It does not run a model or call a paid model API.

The Site signs each request with HMAC-SHA256 and a short-lived timestamp. Set the same random, at least 32-character `BILI_RELAY_SECRET` in the collector environment and in the Site's encrypted runtime secrets. Set the collector's HTTPS origin in the Site's `BILI_RELAY_URL`. Never commit secrets to the repository.

Only the allowlisted Bilibili metadata, subtitle index, playback, comments, QR login, and media sources are accepted. The service follows no redirects. It validates public DNS addresses, limits response sizes and concurrency, uses request-scoped clients, forwards account cookies only to the Bilibili API / video webpage, and suppresses URL and credential logging. Login cookies remain encrypted in the Site's fixed R2 account record. All devices authorized with the Site's access key share that account.

Run with `pip install -r requirements.txt` and `python server.py`, or build the Dockerfile. `PORT` defaults to 7860; Render supplies its own port. `GET /health` returns configuration status without exposing any key. `POST /fetch` requires the Site's signature.

Render's Free service sleeps when idle and may take about a minute to wake. Free usage has limits; select only the Free plan and do not enable paid upgrades. An accessible cloud service does not prove Bilibili permits its network: validate live metadata, playback, and QR generation before declaring the Site repaired. HTTP 412 / 429 and account verification requirements must remain visible, with no repeated automated probing.

Local checks: `python -m unittest test_server.py`. These verify authentication, source restrictions, header sanitation and cookie boundaries. They do not replace cloud end-to-end acceptance.

2026-11-05 (Asia/Tokyo, UTC+09:00): `/frames` uses the same HMAC authentication and downloads only validated video CDN sources to temporary cloud storage. FFmpeg samples at exactly 1 fps, emits 640×360 tiles in 5×5 JPEG sheets, and streams progress / manifest / sheets as NDJSON. Only one decoder runs at a time on the Free instance. Limits: two hours, 768 MiB input, 256 MiB output, ten minutes of decoder time. Temporary files are removed on the cloud service after streaming; the Site stores durable images in R2. Sparse platform storyboards are not used as a substitute when extraction fails.
