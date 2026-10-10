# Deploying the ComNote dashboard to Firebase Hosting

The dashboard (public portal + officer console) is plain HTML/CSS/JS — no build
step. Firebase hosts the pages; they call the ComNote backend (the FastAPI app
that also runs the WhatsApp webhook) for data.

```
com-note.web.app  ──(fetch, Bearer token)──▶  your backend  ──▶  MongoDB
  /            public portal                    /api/public/*
  /track/<ref> one complaint                    /api/* (officer, token required)
  /admin       officer console                  /whatsapp (Meta webhook)
```

## One-time setup

1. **Firebase project** — create `com-note` in the
   [Firebase console](https://console.firebase.google.com) (already named in
   `.firebaserc`), then `firebase login` on this machine.
2. **Point the pages at the backend** — in `static/js/config.js` set
   `API_URL` to the backend's public HTTPS address.
3. **Backend `.env`** — make sure these are set, then restart it:
   ```
   PUBLIC_BASE_URL=https://com-note.web.app     # tracking links sent on WhatsApp
   DASHBOARD_USER=...                            # officer login
   DASHBOARD_PASSWORD=...
   SESSION_SECRET=...                            # signs login tokens and media links
   ```
   `https://com-note.web.app` and `https://com-note.firebaseapp.com` are
   allowed to call the API automatically. Add a custom domain with
   `DASHBOARD_ALLOWED_ORIGINS=https://comnote.gov.ng`.

## Deploy

```powershell
cd c:\Users\USER\Music\comguard\whatsapp_chatbot\dashboard
firebase use com-note
firebase deploy --only hosting
```

Re-run the last command after any change to the pages. The same files are still
served by the backend itself at `/`, `/admin` and `/track/<ref>`, so Firebase
is optional — handy for local testing with `python app.py`.

## Check it worked

- `https://com-note.web.app` shows the stats and map (if it says it can't reach
  the server, `API_URL` is wrong or the backend is down).
- `https://com-note.web.app/admin` → sign in → tickets load, photos display.
- A CORS error in the browser console means the site's address is not in the
  backend's allowed origins — set `FIREBASE_PROJECT` or
  `DASHBOARD_ALLOWED_ORIGINS` and restart the backend.
