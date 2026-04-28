# HireMatrix

Project split for separate backend and frontend deployment.

## Backend: Render

This repo includes `render.yaml`, so the easiest path is Render Blueprint deployment.

1. Push this repo to GitHub.
2. In Render, choose **New > Blueprint**.
3. Select this repo and apply the blueprint.
4. Add the secret value for `GEMINI_API_KEY`.
5. After deployment, open `/api/health` on the Render URL.

Manual settings if you create a Web Service instead:

Root directory: `backend`

Build command:

```bash
pip install -r requirements.txt && python -m spacy download en_core_web_sm
```

Start command:

```bash
gunicorn app:app
```

Set `PYTHON_VERSION` to `3.11.11`. Set `GEMINI_API_KEY` and `SECRET_KEY` in Render. If the frontend is hosted on Vercel, set `FRONTEND_ORIGIN` to the Vercel URL.

## Frontend: Vercel

Vercel root directory: `frontend`

Build command:

```bash
npm run build
```

Output directory:

```bash
dist
```

Set this Vercel environment variable:

```bash
HIREMATRIX_API_BASE=https://your-render-service.onrender.com
```

The frontend build writes that value to `dist/config.js`, and the browser uses it for all `/api`, `/sample`, and `/reports` calls.

After Vercel gives you a URL, update Render's `FRONTEND_ORIGIN` environment variable to that URL.
