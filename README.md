# Idon-Lafia: PHC Triage

**YoungDataPro Datathon 2026 · Health Track**

Idon-Lafia tells LGA health officers in Nigeria **which primary health centres to check first this week, and why**. It reads the monthly reports facilities already send into DHIS2 and treats late and missing reports as warning signs. It forecasts which facilities are likely to run short of medicines, vaccines or staff in the next 1–2 months, and ranks them by how many people would be affected. Urgent facilities reach the officer by **WhatsApp and a phone call**.

> All outcome data is **synthetic**, simulated on real GRID3 facility locations, and labelled "Sample data" throughout.

## The problem

Nigeria has about **31,000 primary health centres**, most people's first contact with the health system. Facilities are meant to report medicine, vaccine and staffing data upward (facility → LGA → state → DHIS2), but reports arrive **late, incomplete or not at all**. So a medicine can run out with nobody above the facility knowing until a patient arrives and there is none.

- **Shortages are common.** The NBS 2023 National Health Facility Survey (3,330 facilities sampled) found only **34.3% of essential drugs** in stock and in date in primary facilities, from about 54% in Delta down to about 14% in Kebbi.
- **Officers can't check everyone.** One LGA officer looks after roughly **40 facilities**, our estimate from about 31,800 PHCs across 774 LGAs, and can visit only a few each week.

**The question Idon-Lafia answers:** *"Which facilities should I check first this week, and why?"*

## Start here

| | |
|---|---|
| 🎬 **Demo video** | [idon-lafiya.web.app/demo](https://idon-lafiya.web.app/demo) |
| 📄 **Project summary** (2 pages) | [`project-SUMMARY.pdf`](project-SUMMARY.pdf) |
| 🌐 **Live dashboard** | [idon-lafiya.web.app](https://idon-lafiya.web.app) (needs the backend running, see [Run it](#run-it)) |
| 📊 **Validation results** | [`phc_triage/reports/validation.md`](phc_triage/reports/validation.md) |

## Results at a glance (synthetic data)

| | Model | "Silent 2+ months" rule | Random |
|---|---|---|---|
| Right in the top 5 per LGA | **82%** | 49% | 31% |
| LGAs never seen in training | **86%** | 52% | – |

The model is fair across rural and urban facilities (83% vs 82%) and holds about 80% in all 8 stress-test worlds. Brier 0.157, AUC 0.786.

## What's in the repo

```
.
├── README.md                       ← you are here
├── project-SUMMARY.pdf             2-page summary: problem, approach, features
├── idon-lafia.mp4                  demo screen recording
├── start_tunnel.ps1                public tunnel + redeploy dashboard (Windows)
├── firebase.json, .firebaserc      Firebase Hosting config for the dashboard
└── phc_triage/
    ├── README.md                   technical details and design decisions
    ├── inputs/                     real inputs: GRID3 facility points, NMIS calibration
    ├── configs/                    one YAML per simulated world (baseline + 8 stress tests)
    ├── simulator/                  builds the synthetic world (hidden truth + DHIS2-style reports)
    ├── data/                       simulated data the app serves (baseline world) + world summaries
    ├── ml/                         features, the three models, evaluation, feedback loop
    ├── models/bundle.joblib        trained models
    ├── reports/                    validation results (markdown + JSON)
    ├── backend/                    FastAPI API
    │   ├── main.py                 all endpoints + WhatsApp webhook
    │   ├── spec_pipeline.py        scores facilities into the ranked risk table
    │   ├── scoring.py              reasons, suggested actions, LGA cluster alerts
    │   ├── alerts.py               automatic urgent alerts (who, when, log)
    │   ├── whatsapp.py             WhatsApp Business Cloud API sender
    │   └── calls.py                BimpeAI voice-agent phone calls
    ├── dashboard/                  web app: officer list, facility page, state overview
    ├── run.py, generate_all.py     regenerate one or all simulated worlds
    └── preview_sample_SYNTHETIC.xlsx   small sample, visible and hidden data side by side
```

## How it works

1. **Data:** simulated monthly reports for 1,537 real facility locations in Oyo, Kano and Gombe (37 LGAs, 48 months). See [`phc_triage/simulator/`](phc_triage/simulator/) and [`phc_triage/data/`](phc_triage/data/).
2. **Three models** in [`phc_triage/ml/`](phc_triage/ml/):
   - **Expected report:** how much activity a facility should have reported.
   - **Silence type:** why a quiet facility is quiet.
   - **Trouble forecast:** a calibrated chance of serious trouble in the next 1–2 months.
3. **Priority** = chance of trouble × people affected (population, distance to the next facility). Each facility gets a HIGH, MEDIUM or LOW tag with a plain-language reason and a suggested action ([`backend/spec_pipeline.py`](phc_triage/backend/spec_pipeline.py)).
4. **Dashboard** ([`phc_triage/dashboard/`](phc_triage/dashboard/)):
   - **Officer list:** ranked to the officer's visit capacity.
   - **Facility page:** history chart, why it is flagged, record the check.
   - **State overview:** totals, LGAs, validation.
5. **Feedback loop:** officers record Confirmed, Cleared, Just late or Couldn't verify. Answers snooze or follow up facilities and become training labels.
6. **Alerts** ([`alerts.py`](phc_triage/backend/alerts.py), [`whatsapp.py`](phc_triage/backend/whatsapp.py), [`calls.py`](phc_triage/backend/calls.py)):
   - An officer saves their number once and gets a WhatsApp message and a phone call for every urgent facility.
   - After that, the backend checks hourly and alerts only when a facility newly becomes HIGH.

## Run it

```bash
cd phc_triage/backend
pip install -r requirements.txt
python main.py                 # API + dashboard on http://localhost:5005/
```

To make it public and redeploy the Firebase dashboard (Windows), run this in a second terminal:

```powershell
powershell -ExecutionPolicy Bypass -File start_tunnel.ps1
```

WhatsApp and phone calls read a `.env` at the repo root. It is never committed. Without it, the dashboard still works and sends report "not configured".

| Variable | Used for |
|---|---|
| `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_GRAPH_VERSION` | sending WhatsApp messages |
| `WHATSAPP_VERIFY_TOKEN`, `PUBLIC_WHATSAPP_NUMBER` | the `/whatsapp` webhook |
| `BIMPEAI_API_KEY` | BimpeAI phone calls (`IDON_CALL_LIVE=true` for live, not test, calls) |

To rebuild the data and models from scratch, see [`phc_triage/README.md`](phc_triage/README.md). The main commands are `python generate_all.py`, then `python -m ml.train`.
