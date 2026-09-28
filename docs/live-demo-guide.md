# Live Demo Runbook

## Before the session

1. Start Docker and confirm the daemon responds.
2. From the project root, run `docker compose up -d --build`.
3. Check `docker compose ps`; wait until the database is healthy and the API, worker, Redis and frontend are running.
4. Check `curl http://localhost:8000/health` returns `{"status":"ok"}` and open `http://localhost:5173`.
5. Enter the local API key in the dashboard. Keep `.env` and the key off screen.
6. Use a public, reachable repository you have already evaluated successfully. Repository builds need outbound internet and can take several minutes; Compose is configured for one evaluation at a time on a 4 GB machine.

## Demonstration flow

1. Show the dashboard and its submission options.
2. Submit the prepared public repository and select its project type.
3. Follow the queued task to completion. A project build/test failure is still a valid evaluation result, but use a known-good sample if you want to demonstrate a successful run.
4. Open the result and explain the engineering maturity score, category breakdown, static findings, dynamic checks and improvement feedback.
5. Open **View Report** and show the downloadable Markdown scorecard, including test outcome and available output.
6. Show the leaderboard and platform metrics. Open `/public` to show the scorecard that does not require an API key.
7. If time permits, open `http://localhost:8000/docs` and show the documented submission and result endpoints.

## If the live submission cannot run

- Keep the dashboard and API health check as the live system demonstration.
- Open a previously completed evaluation from history and walk through its actual results; identify it as a saved run.
- Do not present a failed clone/build as a platform crash: the platform should record a submission verdict and report.
- The Ezitech portal callback and Kubernetes deployment are documented integration/deployment paths, not live-tested demo steps.

## After the session

Do not run `docker compose down -v`; it deletes the persisted database volume. If the stack should be stopped, use `docker compose stop`.
