# PHC Triage — backend + dashboard

Implements the solution doc: a synthetic-DHIS2 simulator, three risk models
(expected-report gap, silence-type classifier, calibrated trouble forecast),
priority ranking, and a dashboard (officer list / facility page / state
overview) with the Confirmed / Cleared / Just late feedback loop.

Everything here is **synthetic data**, clearly labelled. No DHIS2 connection
exists in this build — by design, per the doc's "we read, we never write"
rule for DHIS2.

## Run it

```bash
cd phc_triage/backend
pip install -r requirements.txt
python main.py        # first boot trains the models (~1 min), then caches to data/
```

Open http://localhost:8010/ — the backend also serves the dashboard
(`phc_triage/dashboard/`) as static files, so there's nothing else to start.

To force a clean retrain (new simulated world): delete `backend/data/*.joblib`
and `validation.json`, or POST to `/api/admin/retrain`.

## Layout

- `backend/simulator.py` — builds the fake state: facilities, 24 months of
  hidden truth (stock, staffing, fridges, closures), and the messy DHIS2-style
  reports that truth produces.
- `backend/features.py` — turns a world into a leak-free feature table
  (only past months ever feed a given month's features) plus the hidden
  labels used only for training/grading.
- `backend/train.py` — trains the three models and the validation numbers
  (precision@k vs the "silent 2 months" baseline, calibration, a stress test
  on a different fake world, rural vs urban fairness).
- `backend/scoring.py` — turns trained models + a world into the ranked,
  explained risk table the dashboard reads (priority = trouble chance x
  people affected, SHAP-grouped reasons, plain-language sentence, suggested
  action).
- `backend/main.py` — the FastAPI app: officer list, facility detail, state
  overview, feedback loop, CSV/printable export, weekly brief text.
- `dashboard/` — the three views from section 7 of the doc, plain HTML/CSS/JS,
  no build step.

## What's real vs mocked here

Built: simulator, all three models, ranking, officer list, facility page,
state overview, Confirmed/Cleared/Just late + mark visited + override, CSV
export, printable list, weekly brief (template), validation numbers.

Not built (per the doc, mock-up only): WhatsApp delivery, the facility
in-charge view, live SMS confirmation. `/api/weekly_brief` returns the exact
text a WhatsApp message would carry — wiring an actual WhatsApp Business
account to send it is the only missing piece for that path.
